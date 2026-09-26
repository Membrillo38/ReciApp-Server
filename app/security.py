from __future__ import annotations

import hashlib
import hmac
import ipaddress
import secrets
import socket
import threading
import time
from collections import defaultdict, deque
from urllib.parse import urlparse
from urllib.request import HTTPRedirectHandler, Request as URLRequest, build_opener

from fastapi import HTTPException, Request

from app.cache import redis_rate_allow
from app.config import settings
from app.db import execute


_BLOCKED_HOSTS = {"localhost", "metadata.google.internal"}
_ALLOWED_URL_SCHEMES = {"http": 80, "https": 443}
_MAX_REDIRECTS = 5
_DEFAULT_MAX_BYTES = 25 * 1024 * 1024
_BAN_WINDOW_SECONDS = 5 * 60
_BAN_THRESHOLD = 3
_BAN_DURATIONS_SECONDS = (5 * 60, 60 * 60, 24 * 60 * 60)


def _trusted_proxy_networks() -> list[ipaddress._BaseNetwork]:
    networks: list[ipaddress._BaseNetwork] = []
    for raw in settings.trusted_proxy_ips.split(","):
        item = raw.strip()
        if not item or item == "---":
            continue
        try:
            networks.append(ipaddress.ip_network(item, strict=False))
        except ValueError:
            continue
    return networks


def _is_trusted_proxy(value: str | None) -> bool:
    if not value:
        return False
    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        return False
    return any(address in network for network in _trusted_proxy_networks())


def request_ip(request: Request) -> str:
    """Return client IP, trusting XFF only from configured proxy IPs."""
    client_host = request.client.host if request.client else ""
    forwarded = request.headers.get("x-forwarded-for", "")
    candidate = client_host
    if forwarded and _is_trusted_proxy(client_host):
        candidate = forwarded.split(",", 1)[0].strip()
    try:
        return str(ipaddress.ip_address(candidate))
    except ValueError:
        return "unknown"


def request_host(request: Request) -> str:
    raw = (
        request.headers.get("x-forwarded-host")
        or request.headers.get("host")
        or ""
    )
    return raw.split(",", 1)[0].strip().split(":", 1)[0].lower()


def is_public_dashboard_host(request: Request) -> bool:
    """True when request hits the public internet hostname (sslip / public IP)."""
    host = request_host(request)
    if not host:
        return True
    if host.endswith(".sslip.io") or host.endswith(".nip.io"):
        return True
    if host == "51.255.43.100":
        return True
    return False


def is_tailscale_or_local_ip(value: str | None) -> bool:
    try:
        address = ipaddress.ip_address(value or "")
    except ValueError:
        return False
    if address.is_loopback or address.is_private:
        return True
    # Tailscale CGNAT
    return address in ipaddress.ip_network("100.64.0.0/10")


def dashboard_publicly_blocked(request: Request) -> bool:
    """Block /dashboard on public Host. Allow Tailscale/cpanel proxy Hosts."""
    if not request.url.path.startswith("/dashboard"):
        return False
    if is_public_dashboard_host(request):
        return True
    # Extra belt: if somehow Host is private but client is random internet, still ok
    # because public Host already blocked. Proxy binds Tailscale only.
    return False


def pseudonymous_ip(value: str | None) -> str | None:
    if not value:
        return None
    # Stable enough for abuse analysis, but never stores the raw address.
    digest = hashlib.sha256(value.encode("utf-8")).hexdigest()
    return f"sha256:{digest[:32]}"


def audit_security_event(
    *,
    event: str,
    request: Request | None = None,
    user_id: str | None = None,
    metadata: dict | None = None,
) -> None:
    """Best-effort audit. Never include credentials or request bodies."""
    if settings.maintenance_mode:
        return
    try:
        execute(
            """
            insert into security_events (event, user_id, ip, metadata)
            values (%s, %s, %s, %s)
            """,
            (event[:80], user_id, pseudonymous_ip(request_ip(request)) if request else None, metadata or {}),
        )
    except Exception:
        pass


class SlidingWindowLimiter:
    def __init__(self, limit: int, window_seconds: int) -> None:
        self.limit = max(1, limit)
        self.window_seconds = max(1, window_seconds)
        self._lock = threading.Lock()
        self._hits: dict[str, deque[float]] = defaultdict(deque)

    def allow(self, key: str) -> bool:
        now = time.monotonic()
        with self._lock:
            bucket = self._hits[key]
            cutoff = now - self.window_seconds
            while bucket and bucket[0] <= cutoff:
                bucket.popleft()
            if len(bucket) >= self.limit:
                return False
            bucket.append(now)
            return True


_rate_limiters: dict[tuple[int, int], SlidingWindowLimiter] = {}
_rate_limiters_lock = threading.Lock()


def allow_rate_limit(key: str, *, limit: int, window_seconds: int) -> bool:
    limit = max(1, limit)
    window_seconds = max(1, window_seconds)
    cached = redis_rate_allow(key, limit=limit, window_seconds=window_seconds)
    if cached is not None:
        return cached
    cache_key = (limit, window_seconds)
    with _rate_limiters_lock:
        limiter = _rate_limiters.get(cache_key)
        if limiter is None:
            limiter = SlidingWindowLimiter(limit, window_seconds)
            _rate_limiters[cache_key] = limiter
    return limiter.allow(key)


class _BanState:
    def __init__(self) -> None:
        self.violations: deque[float] = deque()
        self.banned_until = 0.0
        self.level = 0


_ban_lock = threading.Lock()
_ban_states: dict[str, _BanState] = defaultdict(_BanState)


def _ban_key(key: str) -> str:
    # Extract abuse bans must remain scoped to extract endpoints. If these are
    # normalized to the generic ip:/user: keys, middleware blocks library reads
    # after an import burst.
    for prefix in ("extract-ip:", "extract-user:", "extract-user-day:"):
        if key.startswith(prefix):
            return key
    for marker, prefix in (("-ip:", "ip:"), ("ip:", "ip:"), ("-user:", "user:"), ("user:", "user:")):
        if marker in key:
            return prefix + key.split(marker, 1)[1]
    return f"key:{key}"


def _ban_retry_after(state: _BanState, now: float | None = None) -> int:
    current = time.monotonic() if now is None else now
    return max(1, int(state.banned_until - current))


def raise_if_banned(key: str) -> None:
    ban_key = _ban_key(key)
    now = time.monotonic()
    with _ban_lock:
        state = _ban_states.get(ban_key)
        if not state or state.banned_until <= now:
            return
        retry_after = _ban_retry_after(state, now)
    raise HTTPException(
        status_code=429,
        detail="Temporarily banned for repeated rate-limit violations",
        headers={"Retry-After": str(retry_after)},
    )


def record_rate_limit_violation(key: str) -> int | None:
    ban_key = _ban_key(key)
    now = time.monotonic()
    with _ban_lock:
        state = _ban_states[ban_key]
        if state.banned_until > now:
            return _ban_retry_after(state, now)
        cutoff = now - _BAN_WINDOW_SECONDS
        while state.violations and state.violations[0] <= cutoff:
            state.violations.popleft()
        state.violations.append(now)
        if len(state.violations) < _BAN_THRESHOLD:
            return None
        duration = _BAN_DURATIONS_SECONDS[min(state.level, len(_BAN_DURATIONS_SECONDS) - 1)]
        state.level += 1
        state.violations.clear()
        state.banned_until = now + duration
        return duration


def check_not_banned(request: Request, *, user_id: str | None = None) -> None:
    raise_if_banned(f"ip:{request_ip(request)}")
    if user_id:
        raise_if_banned(f"user:{user_id}")


def require_rate_limit(
    request: Request,
    *,
    key: str,
    limit: int,
    window_seconds: int,
    event: str,
    retry_after_seconds: int = 60,
    audit_user_id: str | None = None,
) -> None:
    raise_if_banned(key)
    if allow_rate_limit(key, limit=limit, window_seconds=window_seconds):
        return
    ban_retry_after = record_rate_limit_violation(key)
    audit_security_event(
        event=event,
        request=request,
        user_id=audit_user_id,
        metadata={
            "limit": limit,
            "window_seconds": window_seconds,
            "correlation_id": getattr(request.state, "correlation_id", None),
        },
    )
    raise HTTPException(
        status_code=429,
        detail="Temporarily banned for repeated rate-limit violations" if ban_retry_after else "Too many requests",
        headers={"Retry-After": str(max(1, ban_retry_after or retry_after_seconds))},
    )


def _validate_public_address(raw_address: str) -> None:
    try:
        address = ipaddress.ip_address(raw_address)
    except ValueError as exc:
        raise ValueError("Invalid URL address") from exc
    if (
        address.is_private
        or address.is_loopback
        or address.is_link_local
        or address.is_reserved
        or address.is_multicast
        or address.is_unspecified
    ):
        raise ValueError("Private or metadata addresses are not accepted")


def validate_public_url(url: str, *, allowed_hosts: set[str] | None = None) -> None:
    """Reject ambiguous, local and metadata URLs before fetching."""
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower().rstrip(".")
    scheme = parsed.scheme.lower()
    if scheme not in _ALLOWED_URL_SCHEMES or not host or parsed.username or parsed.password:
        raise ValueError("Only public HTTP(S) URLs are accepted")
    if host in _BLOCKED_HOSTS:
        raise ValueError("Private or metadata addresses are not accepted")
    try:
        port = parsed.port
    except ValueError as exc:
        raise ValueError("Invalid URL port") from exc
    expected_port = _ALLOWED_URL_SCHEMES[scheme]
    if port not in (None, expected_port):
        raise ValueError("Non-standard ports are not accepted")
    if allowed_hosts and not any(host == item or host.endswith(f".{item}") for item in allowed_hosts):
        raise ValueError("Unsupported source host")
    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        _validate_public_address(str(literal))
    addresses: list[str]
    try:
        addresses = [item[4][0] for item in socket.getaddrinfo(host, port or expected_port, type=socket.SOCK_STREAM)]
    except socket.gaierror as exc:
        raise ValueError("URL host could not be resolved") from exc
    for raw_address in set(addresses):
        _validate_public_address(raw_address)


class _SafeRedirectHandler(HTTPRedirectHandler):
    def __init__(self) -> None:
        self.redirect_count = 0

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        self.redirect_count += 1
        if self.redirect_count > _MAX_REDIRECTS:
            raise ValueError("Too many redirects")
        validate_public_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _safe_opener():
    return build_opener(_SafeRedirectHandler())


def safe_urlopen(request: URLRequest, *, timeout: int):
    validate_public_url(request.full_url)
    return _safe_opener().open(request, timeout=timeout)


def safe_urlopen_limited(request: URLRequest, *, timeout: int, max_bytes: int = _DEFAULT_MAX_BYTES) -> bytes:
    limit = max(1, int(max_bytes))
    with safe_urlopen(request, timeout=timeout) as response:
        data = response.read(limit + 1)
    if len(data) > limit:
        raise ValueError("Download exceeded the size bound")
    return data


def safe_compare(left: str | None, right: str | None) -> bool:
    if not left or not right:
        return False
    return hmac.compare_digest(left.encode("utf-8"), right.encode("utf-8"))


def new_correlation_id() -> str:
    return secrets.token_urlsafe(12)


_SCANNER_EXACT = frozenset(
    {
        "/backup.zip",
        "/backup.tar.gz",
        "/backup.sql",
        "/database.sql",
        "/dump.sql",
        "/docker-compose.yml",
        "/phpinfo.php",
        "/config.php",
        "/server.key",
        "/secrets.json",
        "/xmlrpc.php",
        "/wp-config.php",
        "/wp-login.php",
        "/.npmrc",
        "/.bash_history",
    }
)
_SCANNER_PREFIXES = (
    "/.env",
    "/.git",
    "/.ssh",
    "/.svn",
    "/.vscode",
    "/.ds_store",
    "/wp-admin",
    "/wp-content",
    "/wp-includes",
    "/xmlrpc.php",
    "/actuator",
    "/storage/logs",
    "/server.key",
    "/phpinfo",
    "/vendor/phpunit",
    "/telescope",
    "/debug/default",
)


def is_scanner_probe(path: str) -> bool:
    """Cheap path check for mass scanners. Never matches /v1 or /health."""
    raw = (path or "").split("?", 1)[0].lower()
    if raw in {"/health", "/ready", "/ping"} or raw.startswith("/v1/") or raw.startswith("/dashboard"):
        return False
    if raw.startswith("/.well-known/"):
        return False
    if raw in _SCANNER_EXACT:
        return True
    return any(raw == prefix or raw.startswith(prefix + "/") or raw.startswith(prefix + ".") for prefix in _SCANNER_PREFIXES)
