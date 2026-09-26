from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager

import json
import logging
import time
from datetime import datetime, timezone
from uuid import UUID

import httpx
import jwt
from fastapi import BackgroundTasks, Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from starlette.middleware.gzip import GZipMiddleware
from starlette.responses import Response

from app.apple_auth import (
    decrypt_apple_refresh_token,
    encrypt_apple_refresh_token,
    exchange_apple_authorization_code,
    revoke_apple_refresh_token,
    verify_apple_identity_token,
)
from app.cache import reset_cache
from app.auth import (
    ACCOUNT_UNAVAILABLE,
    AuthUser,
    account_error,
    current_user,
    current_user_allow_closed,
    require_api_key,
)
from app.auth_tokens import create_access_token, create_refresh_token, revoke_refresh_token, rotate_refresh_token
from app.apple_notifications import process_signed_notification
from app.config import settings
from app.dashboard_routes import router as dashboard_router
from app.dashboard_stats import log_request
from app.db import db_context, db_context_for_request, execute_returning, fetch_all, get_pool, probe_postgres, reset_db
from app.models import (
    AdminUserCreate,
    AdminUserPatch,
    AuthAppleRequest,
    AuthLogoutRequest,
    AuthRefreshRequest,
    AuthTokenResponse,
    AuthUserResponse,
    ExtractJobResponse,
    ExtractRequest,
    HealthResponse,
    JobResponse,
    JobStatus,
    ListResponse,
    MeResponse,
    OkResponse,
    QueuedJobItem,
    QueuedJobsResponse,
    RecipeListResponse,
    RecipePublic,
)
from app.pipeline import run_extract_job, run_translation_job
from app.quota import assert_can_extract, get_quota, record_usage
from app.job_guard import claim as claim_job, release as release_job, try_claim as try_claim_job
from app.job_errors import STALE_JOB, job_error_code, localize_job_error
from app.localization import normalize_language
from app.observability import AUTH_PATHS, auth_error, init_sentry
from app.security import (
    audit_security_event,
    allow_rate_limit,
    check_not_banned,
    dashboard_publicly_blocked,
    is_scanner_probe,
    new_correlation_id,
    pseudonymous_ip,
    request_ip,
    require_rate_limit,
    validate_public_url,
)
from app.spend import reserve_spend, settle_spend
from app.store import (
    claim_next_pending_extract_for_user,
    count_user_open_extract_jobs,
    create_job,
    delete_account_data,
    delete_user_recipe,
    get_job,
    get_active_job,
    get_job_by_delivery_id,
    grant_job_access,
    get_recipe,
    get_recipe_by_norm,
    JobAccessUnavailable,
    list_jobs,
    list_profiles,
    list_recipes,
    list_user_open_extract_jobs,
    list_user_recipe_summaries,
    recipe_public_from_row,
    save_user_recipe,
    delete_apple_refresh_token,
    get_apple_refresh_token_ciphertext,
    upsert_apple_refresh_token,
    update_job,
    user_can_access_job,
    user_owns_recipe,
)
from app.superwall import apply_superwall_event
from app.url_norm import normalize_url
from app.translation_cache import localized_recipe_row

init_sentry()

@asynccontextmanager
async def lifespan(_app: FastAPI):
    settings.validate_database()
    get_pool()
    try:
        yield
    finally:
        reset_cache()
        reset_db()


_PRODUCTION = settings.environment.lower() in {"production", "prod"}
app = FastAPI(
    title="ReciApp API",
    version="1.3.0",
    lifespan=lifespan,
    docs_url=None if _PRODUCTION else "/docs",
    redoc_url=None if _PRODUCTION else "/redoc",
    openapi_url=None if _PRODUCTION else "/openapi.json",
)
logger = logging.getLogger(__name__)
app.add_middleware(GZipMiddleware, minimum_size=500, compresslevel=4)
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origin_list,
    allow_methods=["GET", "POST", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type", "X-API-Key", "X-Request-ID"],
    expose_headers=["X-Correlation-ID"],
)
app.include_router(dashboard_router)

_STALE_JOB_ERROR = STALE_JOB


def _as_uuid(value: object) -> UUID:
    """psycopg may return UUID already; UUID(uuid) crashes on .replace."""
    return value if isinstance(value, UUID) else UUID(str(value))


def _bearer_user_id(request: Request) -> str | None:
    header = request.headers.get("authorization", "")
    scheme, _, token = header.partition(" ")
    if scheme.lower() != "bearer" or not token or not settings.auth_jwt_secret:
        return None
    try:
        claims = jwt.decode(
            token,
            settings.auth_jwt_secret,
            algorithms=["HS256"],
            issuer=settings.auth_jwt_issuer,
            audience=settings.auth_jwt_audience,
        )
        return str(UUID(str(claims.get("sub") or "")))
    except Exception:
        return None


def _apply_security_headers(request: Request, response: Response, correlation_id: str) -> Response:
    response.headers["X-Correlation-ID"] = correlation_id
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
    response.headers["Cross-Origin-Resource-Policy"] = "same-site"
    response.headers["Cross-Origin-Opener-Policy"] = "same-origin"
    if request.url.path.startswith("/v1/") or request.url.path.startswith("/dashboard"):
        response.headers["Cache-Control"] = "no-store"
    forwarded_proto = (request.headers.get("x-forwarded-proto") or "").split(",", 1)[0].strip().lower()
    if _PRODUCTION or request.url.scheme == "https" or forwarded_proto == "https":
        response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
    if request.url.path.startswith("/dashboard"):
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
            "base-uri 'none'; frame-ancestors 'none'; form-action 'self'"
        )
    return response


def _auth_error_payload(request: Request, code: str, message: str) -> dict[str, object]:
    return {
        "code": code,
        "message": message,
        # Keep the auth envelope and standard FastAPI detail shape so both
        # AuthService and APIClient can classify transient failures.
        "detail": {"code": code, "message": message},
        "request_id": getattr(request.state, "request_id", "unknown"),
        "correlation_id": getattr(request.state, "correlation_id", "unknown"),
    }


def _http_error_code(status: int, detail: object) -> tuple[str, str]:
    if isinstance(detail, dict) and detail.get("code") and detail.get("message"):
        return str(detail["code"]), str(detail["message"])
    return {
        401: "AUTH_UNAUTHORIZED", 403: "AUTH_FORBIDDEN", 422: "AUTH_VALIDATION_FAILED",
        500: "INTERNAL_SERVER_ERROR", 503: "AUTH_UNAVAILABLE",
    }.get(status, "AUTH_REQUEST_FAILED"), str(detail or "Authentication request failed")


@app.exception_handler(HTTPException)
async def auth_http_exception(request: Request, exc: HTTPException) -> JSONResponse:
    if not request.url.path.startswith(AUTH_PATHS) and request.url.path != "/v1/me":
        return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail}, headers=exc.headers)
    code, message = _http_error_code(exc.status_code, exc.detail)
    level = "warning" if exc.status_code in {401, 403, 422} else "error"
    auth_error(request=request, code=code, phase="http_error", status=exc.status_code, level=level, message=message)
    return JSONResponse(
        status_code=exc.status_code,
        content=_auth_error_payload(request, code, message),
        headers=exc.headers,
    )


@app.exception_handler(RequestValidationError)
async def auth_validation_exception(request: Request, exc: RequestValidationError) -> JSONResponse:
    if not request.url.path.startswith(AUTH_PATHS) and request.url.path != "/v1/me":
        return JSONResponse(status_code=422, content={"detail": exc.errors()})
    auth_error(request=request, code="AUTH_VALIDATION_FAILED", phase="request_validation", status=422, level="warning", message="Authentication request validation failed")
    return JSONResponse(status_code=422, content=_auth_error_payload(request, "AUTH_VALIDATION_FAILED", "Authentication request validation failed"))


@app.exception_handler(Exception)
async def unexpected_exception(request: Request, exc: Exception) -> JSONResponse:
    if request.url.path.startswith(AUTH_PATHS) or request.url.path == "/v1/me":
        auth_error(request=request, code="INTERNAL_SERVER_ERROR", phase="unexpected", status=500, exc=exc)
        return JSONResponse(status_code=500, content=_auth_error_payload(request, "INTERNAL_SERVER_ERROR", "Internal server error"))
    raise exc


@app.exception_handler(httpx.HTTPStatusError)
@app.exception_handler(httpx.RequestError)
async def upstream_request_error(request: Request, exc: httpx.RequestError | httpx.HTTPStatusError) -> JSONResponse:
    """Turn transient upstream disconnects into an iOS-retryable response."""
    if request.url.path.startswith(AUTH_PATHS):
        auth_error(request=request, code="AUTH_UPSTREAM_TIMEOUT" if isinstance(exc, httpx.TimeoutException) else "AUTH_UPSTREAM_ERROR", phase="network", status=503, exc=exc)
    logger.warning(
        "upstream request failed path=%s error_type=%s correlation_id=%s",
        request.url.path,
        type(exc).__name__,
        getattr(request.state, "correlation_id", "unknown"),
    )
    if request.url.path.startswith(AUTH_PATHS):
        content = _auth_error_payload(request, "AUTH_UPSTREAM_TIMEOUT" if isinstance(exc, httpx.TimeoutException) else "AUTH_UPSTREAM_ERROR", "Authentication temporarily unavailable")
    else:
        content = {"detail": "Backend temporarily unavailable. Retry."}
    return JSONResponse(status_code=503, content=content, headers={"Retry-After": "1"})


def _job_is_stale(row: dict) -> bool:
    if row.get("status") not in {JobStatus.pending.value, JobStatus.processing.value}:
        return False
    raw = row.get("updated_at") or row.get("created_at")
    if not raw:
        return False
    try:
        timestamp = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError:
        return False
    if timestamp.tzinfo is None:
        timestamp = timestamp.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - timestamp).total_seconds() > settings.job_ttl_seconds


def _attach_recipe_or_retry(user_id: UUID, recipe_id: UUID) -> None:
    try:
        save_user_recipe(user_id, recipe_id)
    except Exception as exc:
        logger.warning("recipe user-link write failed error_type=%s", type(exc).__name__)
        raise HTTPException(
            status_code=503,
            detail={
                "code": "RECIPE_ATTACH_UNAVAILABLE",
                "message": "Recipe temporarily unavailable. Retry.",
            },
            headers={"Retry-After": "1"},
        ) from exc


def _expire_stale_job(row: dict) -> bool:
    if settings.maintenance_mode or not _job_is_stale(row):
        return False
    job_id = UUID(str(row["id"]))
    update_job(
        job_id,
        status=JobStatus.failed.value,
        progress=0,
        lease_until=None,
        error=_STALE_JOB_ERROR,
    )
    settle_spend(job_id=job_id, actual_cents=0, status="failed")
    return True


def _share_job_or_http(row: dict, user: AuthUser) -> None:
    """Grant access only when a caller joins another user's active job."""
    if str(row.get("user_id") or "") == str(user.id):
        return
    try:
        grant_job_access(job_id=UUID(str(row["id"])), user_id=user.id)
    except JobAccessUnavailable as exc:
        raise HTTPException(
            status_code=503,
            detail={"code": "JOB_ACCESS_UNAVAILABLE", "message": "Job access temporarily unavailable."},
        ) from exc


def _ensure_translation_job(
    *,
    request: Request | None = None,
    user: AuthUser,
    recipe_id: UUID,
    source_url_raw: str,
    source_url_norm: str,
    language_code: str,
    client_delivery_id: UUID | None = None,
    background: BackgroundTasks,
) -> dict:
    require_writes_enabled()
    language_code = normalize_language(language_code)
    active = get_active_job(
        source_url_norm=source_url_norm,
        language_code=language_code,
        job_kind="translation",
        recipe_id=recipe_id,
    )
    if active:
        if _expire_stale_job(active):
            active = None
        else:
            _share_job_or_http(active, user)
            return active

    assert_can_extract(user, cache_hit=False, request=request)
    local_claimed = not settings.worker_enabled
    if local_claimed:
        claim_job(user.id)
    try:
        job = create_job(
            user_id=user.id,
            source_url_raw=source_url_raw,
            source_url_norm=source_url_norm,
            language_code=language_code,
            job_kind="translation",
            status=JobStatus.pending.value,
            cache_hit=False,
            recipe_id=recipe_id,
            client_delivery_id=client_delivery_id,
        )
        if job.get("_idempotent_replay"):
            if (
                job.get("source_url_norm") != source_url_norm
                or job.get("language_code") != language_code
            ):
                raise HTTPException(
                    status_code=409,
                    detail={
                        "code": "IDEMPOTENCY_KEY_REUSED",
                        "message": "This delivery identifier was already used for another import.",
                    },
                )
            if local_claimed:
                release_job(user.id)
            return job
        job_id = _as_uuid(job["id"])
        reserve_spend(user_id=user.id, job_id=job_id)
    except Exception:
        # Recovery reads/writes can fail too; release capacity before them.
        if local_claimed:
            release_job(user.id)
        if "job_id" in locals():
            update_job(job_id, status=JobStatus.failed.value, error="Usage protection unavailable")
            raise
        active = get_active_job(
            source_url_norm=source_url_norm,
            language_code=language_code,
            job_kind="translation",
            recipe_id=recipe_id,
        )
        if active:
            _share_job_or_http(active, user)
            return active
        raise
    if not settings.worker_enabled:
        background.add_task(run_translation_job, job_id, user.id, recipe_id, language_code)
    return job


@app.middleware("http")
async def request_metrics(request: Request, call_next):
    start = time.perf_counter()
    client_request_id = request.headers.get("x-request-id", "")
    request_id = (
        client_request_id
        if 8 <= len(client_request_id) <= 64
        and all(character.isalnum() or character == "-" for character in client_request_id)
        else new_correlation_id()
    )
    correlation_id = new_correlation_id()
    request.state.request_id = request_id
    request.state.correlation_id = correlation_id
    content_length = request.headers.get("content-length")
    is_probe = request.url.path in {"/health", "/ready"}
    # Public sslip Host must not expose ops UI — Tailscale/cpanel proxy only.
    if dashboard_publicly_blocked(request):
        return _apply_security_headers(
            request,
            JSONResponse(status_code=404, content={"detail": "Not Found"}),
            correlation_id,
        )
    if is_scanner_probe(request.url.path):
        client_ip = request_ip(request)
        if allow_rate_limit(f"scanner-log:{client_ip}", limit=30, window_seconds=3600):
            audit_security_event(
                event="scanner_probe",
                request=request,
                metadata={"path": request.url.path[:200], "ip": client_ip},
            )
        return _apply_security_headers(
            request,
            JSONResponse(status_code=403, content={"detail": "Forbidden"}),
            correlation_id,
        )
    response = None
    user_id = _bearer_user_id(request)
    actor, rls_user_id = db_context_for_request(request.url.path, user_id)
    with db_context(actor=actor, user_id=rls_user_id):
        return await _finish_request_metrics(
            request, call_next, start, correlation_id, content_length, is_probe, user_id
        )


async def _finish_request_metrics(request, call_next, start, correlation_id, content_length, is_probe, user_id):
    response = None
    if not is_probe and request.url.path.startswith("/v1/"):
        try:
            check_not_banned(request, user_id=user_id)
            require_rate_limit(
                request,
                key=f"ip:{request_ip(request)}",
                limit=settings.rate_limit_per_ip_per_minute,
                window_seconds=60,
                event="general_ip_rate_limited",
            )
            if user_id:
                require_rate_limit(
                    request,
                    key=f"user:{user_id}",
                    limit=settings.rate_limit_per_user_per_minute,
                    window_seconds=60,
                    event="general_user_rate_limited",
                    audit_user_id=user_id,
                )
        except HTTPException as exc:
            response = JSONResponse(
                status_code=exc.status_code,
                content={"detail": exc.detail},
                headers=exc.headers,
            )
    if response is not None:
        pass
    elif settings.maintenance_mode and not is_probe and request.method not in {"GET", "HEAD", "OPTIONS"}:
        response = JSONResponse(
            status_code=503, content={"detail": "Maintenance in progress. Retry."},
            headers={"Retry-After": "30"},
        )
    elif content_length and content_length.isdigit() and int(content_length) > settings.max_request_body_bytes:
        response = JSONResponse(status_code=413, content={"detail": "Request body too large"})
    else:
        response = await call_next(request)
    duration_ms = int((time.perf_counter() - start) * 1000)
    if not is_probe and not settings.maintenance_mode:
        try:
            # Keep the synchronous metrics insert off the event loop.
            from starlette.concurrency import run_in_threadpool
            await run_in_threadpool(
                log_request,
                method=request.method,
                path=request.url.path,
                status_code=response.status_code,
                duration_ms=duration_ms,
                user_id=None,
                ip=pseudonymous_ip(request_ip(request)),
                correlation_id=correlation_id,
            )
        except Exception:
            pass
    logger.info(
        "request completed endpoint=%s method=%s status=%s server_code=%s request_id=%s correlation_id=%s duration_ms=%s",
        request.url.path,
        request.method,
        response.status_code,
        _http_error_code(response.status_code, "")[0] if response.status_code >= 400 else "",
        getattr(request.state, "request_id", "unknown"),
        correlation_id,
        duration_ms,
    )
    return _apply_security_headers(request, response, correlation_id)


@app.get("/health", response_model=HealthResponse)
def health() -> HealthResponse:
    return HealthResponse()


@app.get("/ready")
async def ready() -> JSONResponse:
    start = time.perf_counter()
    error = None
    try:
        async with asyncio.timeout(settings.readiness_timeout_seconds):
            await probe_postgres()
    except Exception as exc:
        error = type(exc).__name__
    return JSONResponse(
        status_code=503 if error else 200,
        content={
            "status": "unavailable" if error else "ready",
            "latency_ms": round((time.perf_counter() - start) * 1000),
            "maintenance": settings.maintenance_mode,
            "environment": settings.environment,
            **({"error": error} if error else {}),
        },
        headers={"Cache-Control": "no-store", **({"Retry-After": "1"} if error else {})},
    )


def require_writes_enabled() -> None:
    if settings.maintenance_mode:
        raise HTTPException(
            status_code=503, detail="Maintenance in progress. Retry.",
            headers={"Retry-After": "30"},
        )


def _require_auth_secret() -> None:
    if not settings.auth_jwt_secret:
        raise HTTPException(status_code=503, detail="AUTH_JWT_SECRET not configured")


def _auth_user_response(row: dict) -> AuthUserResponse:
    return AuthUserResponse(
        id=UUID(str(row["id"])),
        email=row.get("email"),
        display_name=row.get("display_name"),
        is_pro=bool(row.get("is_pro")),
        pro_expires_at=str(row["pro_expires_at"]) if row.get("pro_expires_at") else None,
    )


@app.post("/v1/webhooks/superwall")
async def superwall_webhook(
    request: Request,
    svix_id: str | None = Header(default=None, alias="svix-id"),
    svix_timestamp: str | None = Header(default=None, alias="svix-timestamp"),
    svix_signature: str | None = Header(default=None, alias="svix-signature"),
) -> JSONResponse:
    raw = await request.body()
    if len(raw) > settings.max_request_body_bytes:
        raise HTTPException(status_code=413, detail="Webhook payload too large")
    if not settings.superwall_webhook_secret:
        raise HTTPException(status_code=503, detail="Webhook verification unavailable")
    try:
        from svix.webhooks import Webhook

        wh = Webhook(settings.superwall_webhook_secret)
        payload = wh.verify(
            raw,
            {
                "svix-id": svix_id or "",
                "svix-timestamp": svix_timestamp or "",
                "svix-signature": svix_signature or "",
            },
        )
    except Exception as exc:
        raise HTTPException(status_code=400, detail="Invalid webhook signature") from exc

    if isinstance(payload, (bytes, str)):
        try:
            payload = json.loads(payload)
        except json.JSONDecodeError as exc:
            raise HTTPException(status_code=400, detail="Invalid webhook payload") from exc
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="Invalid webhook payload")

    app_id = payload.get("applicationId")
    if app_id is not None:
        try:
            app_id = int(app_id)
        except (TypeError, ValueError) as exc:
            raise HTTPException(status_code=400, detail="Invalid application id") from exc
        if app_id != settings.superwall_application_id:
            return JSONResponse({"ok": True, "skipped": "wrong_application"})

    try:
        result = apply_superwall_event(payload, event_id=svix_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Webhook event id required") from exc
    return JSONResponse({"ok": True, **result})


@app.post("/v1/webhooks/apple")
async def apple_webhook(request: Request) -> JSONResponse:
    raw = await request.body()
    if len(raw) > settings.max_request_body_bytes:
        raise HTTPException(status_code=413, detail="Apple notification payload too large")
    try:
        body = json.loads(raw.decode("utf-8"))
        signed_payload = body.get("signedPayload") if isinstance(body, dict) else None
        if not isinstance(signed_payload, str):
            raise ValueError("signedPayload required")
        result = process_signed_notification(signed_payload)
    except HTTPException:
        raise
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Invalid signed Apple notification") from exc
    except Exception as exc:
        raise HTTPException(status_code=503, detail="Apple notification verification unavailable") from exc
    return JSONResponse(result)


def _upsert_apple_profile(apple, display_name: str | None) -> dict | None:
    """Create or reactivate the profile for this Apple subject (same id keeps free quota)."""
    return execute_returning(
        """
        insert into profiles (email, apple_sub, display_name)
        values (%s, %s, %s)
        on conflict (apple_sub) do update
           set email = coalesce(excluded.email, profiles.email),
               display_name = coalesce(excluded.display_name, profiles.display_name),
               deleted_at = null
        returning id, email, display_name, is_pro, pro_expires_at, deleted_at
        """,
        (apple.email, apple.apple_sub, display_name),
    )


def _persist_apple_authorization_code(user_id: UUID, authorization_code: str | None, apple_sub: str) -> None:
    code = (authorization_code or "").strip()
    if not code:
        return
    refresh = exchange_apple_authorization_code(code, expected_sub=apple_sub)
    if not refresh:
        return
    try:
        upsert_apple_refresh_token(user_id, encrypt_apple_refresh_token(refresh))
    except Exception as exc:
        logger.warning("apple refresh token persist failed error_type=%s", type(exc).__name__)


def revoke_stored_apple_authorization(user_id: UUID) -> None:
    try:
        ciphertext = get_apple_refresh_token_ciphertext(user_id)
    except Exception as exc:
        logger.warning("apple refresh token load failed error_type=%s", type(exc).__name__)
        return
    if not ciphertext:
        return
    try:
        refresh = decrypt_apple_refresh_token(ciphertext)
    except Exception as exc:
        logger.warning("apple refresh token decrypt failed error_type=%s", type(exc).__name__)
        try:
            delete_apple_refresh_token(user_id)
        except Exception:
            pass
        return
    if not revoke_apple_refresh_token(refresh):
        return
    try:
        delete_apple_refresh_token(user_id)
    except Exception as exc:
        logger.warning("apple refresh token delete failed error_type=%s", type(exc).__name__)


def _purge_account(user_id: UUID) -> None:
    revoke_stored_apple_authorization(user_id)
    try:
        delete_account_data(user_id)
    except Exception as exc:
        logger.error("account deletion transaction failed error_type=%s", type(exc).__name__)
        raise HTTPException(
            status_code=503,
            detail={
                "code": "ACCOUNT_DELETION_UNAVAILABLE",
                "message": "Account deletion temporarily unavailable. Retry.",
            },
            headers={"Retry-After": "3"},
        ) from exc


@app.post("/v1/auth/apple", response_model=AuthTokenResponse)
def auth_apple(request: Request, body: AuthAppleRequest) -> AuthTokenResponse:
    require_writes_enabled()
    _require_auth_secret()
    try:
        apple = verify_apple_identity_token(body.identity_token, nonce=body.nonce)
    except Exception as exc:
        code = "APPLE_NONCE_INVALID" if "nonce" in str(exc).lower() else "APPLE_TOKEN_INVALID"
        auth_error(request=request, code=code, phase="identity_token", status=401, level="warning", message="Apple identity token rejected")
        raise HTTPException(status_code=401, detail={"code": code, "message": "Apple identity token rejected"}) from exc
    display_name = (body.full_name or "").strip()[:200] or None
    try:
        row = _upsert_apple_profile(apple, display_name)
    except Exception as exc:
        auth_error(request=request, code="AUTH_DATABASE_ERROR", phase="profile_upsert", status=503, exc=exc)
        logger.warning("apple profile upsert failed error_type=%s", type(exc).__name__)
        raise HTTPException(status_code=503, detail={"code": "AUTH_DATABASE_ERROR", "message": "Authentication temporarily unavailable"}, headers={"Retry-After": "1"}) from exc
    if not row:
        raise account_error(ACCOUNT_UNAVAILABLE, "Account unavailable")
    user_id = UUID(str(row["id"]))
    _persist_apple_authorization_code(user_id, body.authorization_code, apple.apple_sub)
    try:
        return AuthTokenResponse(
            access_token=create_access_token(user_id, row.get("email")),
            refresh_token=create_refresh_token(
                user_id,
                user_agent=request.headers.get("user-agent"),
                ip_hash=pseudonymous_ip(request_ip(request)),
            ),
            expires_in=settings.auth_access_token_ttl_seconds,
            user=_auth_user_response(row),
        )
    except Exception as exc:
        auth_error(request=request, code="AUTH_SESSION_ISSUE_FAILED", phase="jwt", status=503, exc=exc)
        logger.warning("apple session issue failed error_type=%s", type(exc).__name__)
        raise HTTPException(status_code=503, detail={"code": "AUTH_SESSION_ISSUE_FAILED", "message": "Authentication temporarily unavailable"}, headers={"Retry-After": "1"}) from exc


@app.post("/v1/auth/refresh", response_model=AuthTokenResponse)
def auth_refresh(request: Request, body: AuthRefreshRequest) -> AuthTokenResponse:
    _require_auth_secret()
    try:
        pair = rotate_refresh_token(body.refresh_token)
    except Exception as exc:
        auth_error(request=request, code="REFRESH_DATABASE_ERROR", phase="refresh_token", status=503, exc=exc)
        raise HTTPException(status_code=503, detail={"code": "REFRESH_DATABASE_ERROR", "message": "Authentication temporarily unavailable"}, headers={"Retry-After": "1"}) from exc
    if not pair:
        auth_error(request=request, code="REFRESH_TOKEN_EXPIRED", phase="refresh_token", status=401, level="warning", message="Refresh token rejected")
        raise HTTPException(status_code=401, detail={"code": "REFRESH_TOKEN_EXPIRED", "message": "Refresh token rejected"})
    return AuthTokenResponse(
        access_token=pair["access_token"],
        refresh_token=pair["refresh_token"],
        expires_in=settings.auth_access_token_ttl_seconds,
    )


@app.post("/v1/auth/logout", response_model=OkResponse)
def auth_logout(request: Request, body: AuthLogoutRequest) -> OkResponse:
    try:
        revoke_refresh_token(body.refresh_token)
    except Exception as exc:
        auth_error(request=request, code="LOGOUT_DATABASE_ERROR", phase="logout", status=503, exc=exc)
        raise HTTPException(status_code=503, detail={"code": "LOGOUT_DATABASE_ERROR", "message": "Logout temporarily unavailable"}) from exc
    return OkResponse()


@app.get("/v1/me", response_model=MeResponse)
def me(user: AuthUser = Depends(current_user)) -> MeResponse:
    q = get_quota(user)
    return MeResponse(
        id=user.id,
        display_name=user.display_name,
        is_pro=user.is_pro,
        pro_expires_at=user.pro_expires_at,
        free_used_this_week=q.free_used_this_week,
        free_used_this_year=q.free_used_this_week,
        free_limit=q.free_limit,
        free_remaining=q.free_remaining,
        pro_remaining_cents=q.pro_remaining_cents if user.is_pro else None,
    )


@app.delete("/v1/me", response_model=OkResponse)
def delete_me(user: AuthUser = Depends(current_user_allow_closed)) -> OkResponse:
    _purge_account(user.id)
    return OkResponse()


@app.post("/v1/extract", response_model=ExtractJobResponse)
def extract_recipe(
    request: Request,
    body: ExtractRequest,
    background: BackgroundTasks,
    user: AuthUser = Depends(current_user),
) -> ExtractJobResponse:
    url = str(body.url)
    language_code = normalize_language(body.language)
    try:
        allowed = {"youtube.com", "youtu.be", "tiktok.com", "instagram.com", "facebook.com", "fb.watch"}
        validate_public_url(url, allowed_hosts=allowed)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="Unsupported or unsafe URL") from exc
    url_norm = normalize_url(url)

    if body.client_delivery_id is not None:
        existing_delivery = get_job_by_delivery_id(user.id, body.client_delivery_id)
        if existing_delivery:
            if (
                existing_delivery.get("source_url_norm") != url_norm
                or existing_delivery.get("language_code") != language_code
            ):
                raise HTTPException(
                    status_code=409,
                    detail={
                        "code": "IDEMPOTENCY_KEY_REUSED",
                        "message": "This delivery identifier was already used for another import.",
                    },
                )
            if (
                existing_delivery.get("status") == JobStatus.completed.value
                and existing_delivery.get("cache_hit")
                and existing_delivery.get("recipe_id")
            ):
                _attach_recipe_or_retry(user.id, _as_uuid(existing_delivery["recipe_id"]))
            return ExtractJobResponse(
                job_id=_as_uuid(existing_delivery["id"]),
                status=JobStatus(existing_delivery["status"]),
                cache_hit=bool(existing_delivery.get("cache_hit")),
                progress=int(existing_delivery.get("progress") or 0),
                queued=existing_delivery["status"] in {JobStatus.pending.value, JobStatus.processing.value},
            )

    require_rate_limit(
        request,
        key=f"extract-ip:{request_ip(request)}",
        limit=settings.rate_limit_extract_per_ip_per_minute,
        window_seconds=60,
        event="extract_ip_rate_limited",
    )
    require_rate_limit(
        request,
        key=f"extract-user:{user.id}",
        limit=settings.rate_limit_extract_per_user_per_minute,
        window_seconds=60,
        event="extract_user_rate_limited",
        audit_user_id=user.id,
    )
    require_rate_limit(
        request,
        key=f"extract-user-day:{user.id}",
        limit=settings.rate_limit_extract_daily_per_user,
        window_seconds=24 * 60 * 60,
        event="extract_user_daily_rate_limited",
        retry_after_seconds=60 * 60,
        audit_user_id=user.id,
    )

    cached = get_recipe_by_norm(url_norm)
    if cached:
        recipe_id = _as_uuid(cached["id"])
        localized = localized_recipe_row(cached, language_code)
        if localized is not None:
            job = create_job(
                user_id=user.id,
                source_url_raw=url,
                source_url_norm=url_norm,
                language_code=language_code,
                status=JobStatus.completed.value,
                cache_hit=True,
                recipe_id=recipe_id,
                cost_cents=0,
                client_delivery_id=body.client_delivery_id,
            )
            if job.get("_idempotent_replay"):
                if job.get("source_url_norm") != url_norm or job.get("language_code") != language_code:
                    raise HTTPException(
                        status_code=409,
                        detail={"code": "IDEMPOTENCY_KEY_REUSED", "message": "This delivery identifier was already used for another import."},
                    )
                return ExtractJobResponse(
                    job_id=_as_uuid(job["id"]),
                    status=JobStatus(job["status"]),
                    cache_hit=bool(job.get("cache_hit")),
                    progress=int(job.get("progress") or 0),
                )
            _attach_recipe_or_retry(user.id, recipe_id)
            try:
                record_usage(
                    user_id=user.id,
                    kind="extract_hit",
                    cost_cents=0,
                    recipe_id=recipe_id,
                    job_id=_as_uuid(job["id"]),
                )
            except Exception:
                # A usage-log outage must not hide a recipe that is already
                # cached and attached to the user.
                pass
            return ExtractJobResponse(
                job_id=_as_uuid(job["id"]),
                status=JobStatus.completed,
                cache_hit=True,
                progress=100,
            )

        if not settings.openai_api_key:
            raise HTTPException(status_code=503, detail="OPENAI_API_KEY not configured")

        job = _ensure_translation_job(
            request=request,
            user=user,
            recipe_id=recipe_id,
            source_url_raw=url,
            source_url_norm=url_norm,
            language_code=language_code,
            client_delivery_id=body.client_delivery_id,
            background=background,
        )
        return ExtractJobResponse(
            job_id=_as_uuid(job["id"]),
            status=JobStatus(job["status"]),
            cache_hit=False,
            progress=int(job.get("progress") or 0),
        )

    active = get_active_job(source_url_norm=url_norm, job_kind="extract")
    if active:
        if not _expire_stale_job(active):
            _share_job_or_http(active, user)
            return ExtractJobResponse(
                job_id=_as_uuid(active["id"]),
                status=JobStatus(active["status"]),
                cache_hit=False,
                progress=int(active.get("progress") or 0),
            )

    if not settings.openai_api_key:
        raise HTTPException(status_code=503, detail="OPENAI_API_KEY not configured")

    assert_can_extract(user, cache_hit=False, request=request)

    open_count = count_user_open_extract_jobs(user.id)
    if open_count >= settings.max_pending_jobs_per_user:
        raise HTTPException(
            status_code=429,
            detail="Import queue is full. Wait for a recipe to finish.",
            headers={"Retry-After": "30"},
        )

    # Serial per user: start now only when this user has no open extract yet.
    should_start = not settings.worker_enabled and open_count == 0
    local_claimed = False
    if should_start:
        local_claimed = try_claim_job(user.id)
        should_start = local_claimed

    try:
        job = create_job(
            user_id=user.id,
            source_url_raw=url,
            source_url_norm=url_norm,
            language_code=language_code,
            job_kind="extract",
            status=JobStatus.pending.value,
            cache_hit=False,
            client_delivery_id=body.client_delivery_id,
        )
        if job.get("_idempotent_replay"):
            if job.get("source_url_norm") != url_norm or job.get("language_code") != language_code:
                raise HTTPException(
                    status_code=409,
                    detail={"code": "IDEMPOTENCY_KEY_REUSED", "message": "This delivery identifier was already used for another import."},
                )
            if local_claimed:
                release_job(user.id)
            return ExtractJobResponse(
                job_id=_as_uuid(job["id"]),
                status=JobStatus(job["status"]),
                cache_hit=bool(job.get("cache_hit")),
                progress=int(job.get("progress") or 0),
                queued=job["status"] in {JobStatus.pending.value, JobStatus.processing.value},
            )
        job_id = _as_uuid(job["id"])
        reserve_spend(user_id=user.id, job_id=job_id)
    except HTTPException as exc:
        if local_claimed:
            release_job(user.id)
        if "job_id" in locals():
            detail = exc.detail
            message = detail.get("message") if isinstance(detail, dict) else detail
            update_job(
                job_id,
                status=JobStatus.failed.value,
                error=str(message or "Usage protection unavailable")[:300],
            )
        raise
    except Exception:
        # Recovery reads/writes can fail too; release capacity before them.
        if local_claimed:
            release_job(user.id)
        if "job_id" in locals():
            update_job(job_id, status=JobStatus.failed.value, error="Usage protection unavailable")
            raise
        active = get_active_job(source_url_norm=url_norm, job_kind="extract")
        if active and not _expire_stale_job(active):
            _share_job_or_http(active, user)
            return ExtractJobResponse(
                job_id=_as_uuid(active["id"]),
                status=JobStatus(active["status"]),
                cache_hit=False,
                progress=int(active.get("progress") or 0),
            )
        raise

    queue_position = open_count + 1
    if should_start and not settings.worker_enabled:
        background.add_task(run_extract_job, job_id, user.id, url, url_norm, language_code)
        return ExtractJobResponse(
            job_id=job_id,
            status=JobStatus.pending,
            cache_hit=False,
            queued=False,
            queue_position=queue_position,
        )

    # Left pending: drain starts it after the user's current extract finishes.
    return ExtractJobResponse(
        job_id=job_id,
        status=JobStatus.pending,
        cache_hit=False,
        queued=True,
        queue_position=queue_position,
    )


@app.get("/v1/me/jobs", response_model=QueuedJobsResponse)
def list_my_jobs(user: AuthUser = Depends(current_user)) -> QueuedJobsResponse:
    rows = list_user_open_extract_jobs(user.id, limit=settings.max_pending_jobs_per_user)
    items = [
        QueuedJobItem(
            job_id=UUID(str(row["id"])),
            status=JobStatus(row["status"]),
            progress=int(row.get("progress") or 0),
            source_url=str(row.get("source_url_raw") or row.get("source_url_norm") or ""),
            queue_position=index + 1,
            created_at=str(row["created_at"]) if row.get("created_at") else None,
            job_kind=str(row.get("job_kind") or "extract"),
        )
        for index, row in enumerate(rows)
    ]
    return QueuedJobsResponse(items=items)


@app.get("/v1/jobs/{job_id}", response_model=JobResponse)
def get_job_status(
    job_id: UUID,
    background: BackgroundTasks,
    language: str = Query("en-US"),
    user: AuthUser = Depends(current_user),
) -> JobResponse:
    require_writes_enabled()  # Polling can expire, attach and translate recipes.
    row = get_job(job_id)
    if not row:
        raise HTTPException(status_code=404, detail="Job not found")
    try:
        can_access = user_can_access_job(job_id=job_id, user_id=user.id, row=row)
    except JobAccessUnavailable as exc:
        raise HTTPException(
            status_code=503,
            detail={"code": "JOB_ACCESS_UNAVAILABLE", "message": "Job access temporarily unavailable."},
        ) from exc
    if not can_access:
        raise HTTPException(status_code=403, detail="Not your job")

    if _expire_stale_job(row):
        row = dict(row)
        row.update(status=JobStatus.failed.value, progress=0, error=_STALE_JOB_ERROR)

    recipe = None
    next_job_id = None
    if row.get("recipe_id"):
        r = get_recipe(_as_uuid(row["recipe_id"]))
        if r:
            _attach_recipe_or_retry(user.id, _as_uuid(row["recipe_id"]))
            requested_language = normalize_language(language)
            row_language = normalize_language(row.get("language_code"))
            resolution_language = row_language if row.get("job_kind") == "translation" else requested_language
            localized = localized_recipe_row(r, resolution_language)
            if (
                row.get("job_kind") == "extract"
                and row.get("status") == JobStatus.completed.value
                and localized is None
                and requested_language != row_language
            ):
                translation_job = _ensure_translation_job(
                    request=request,
                    user=user,
                    recipe_id=_as_uuid(row["recipe_id"]),
                    source_url_raw=row.get("source_url_raw") or r.get("source_url_raw") or "",
                    source_url_norm=row.get("source_url_norm") or r.get("source_url_norm") or "",
                    language_code=requested_language,
                    background=background,
                )
                next_job_id = _as_uuid(translation_job["id"])
                return JobResponse(
                    job_id=next_job_id,
                    status=JobStatus(translation_job["status"]),
                    cache_hit=False,
                    recipe=None,
                    recipe_id=_as_uuid(row["recipe_id"]),
                    error=None,
                    progress=int(translation_job.get("progress") or 0),
                )
            recipe = recipe_public_from_row(localized or r)

    return JobResponse(
        job_id=_as_uuid(row["id"]),
        status=JobStatus(row["status"]),
        cache_hit=bool(row.get("cache_hit")),
        recipe=recipe,
        recipe_id=_as_uuid(row["recipe_id"]) if row.get("recipe_id") else None,
        error=localize_job_error(row.get("error"), language) if row.get("error") else None,
        error_code=job_error_code(row.get("error")),
        progress=int(row.get("progress") or 0),
        next_job_id=next_job_id,
    )


@app.get("/v1/me/recipes", response_model=RecipeListResponse)
def my_recipes(
    language: str = Query("en-US"),
    user: AuthUser = Depends(current_user),
) -> RecipeListResponse:
    return RecipeListResponse(
        items=list_user_recipe_summaries(user.id, normalize_language(language))
    )


@app.delete("/v1/me/recipes/{recipe_id}", response_model=OkResponse)
def remove_my_recipe(
    recipe_id: UUID,
    user: AuthUser = Depends(current_user),
) -> OkResponse:
    ok = delete_user_recipe(user.id, recipe_id)
    if not ok:
        raise HTTPException(status_code=404, detail="Recipe not in your list")
    return OkResponse()


@app.get("/v1/recipes/{recipe_id}", response_model=RecipePublic)
def recipe_detail(
    recipe_id: UUID,
    language: str = Query("en-US"),
    user: AuthUser = Depends(current_user),
) -> RecipePublic:
    if not user_owns_recipe(user.id, recipe_id):
        raise HTTPException(status_code=403, detail="Recipe not in your list")
    row = get_recipe(recipe_id)
    if not row:
        raise HTTPException(status_code=404, detail="Recipe not found")
    localized = localized_recipe_row(row, normalize_language(language))
    return recipe_public_from_row(localized or row)


# --- Admin ---


@app.get("/v1/admin/users", response_model=ListResponse, dependencies=[Depends(require_api_key)])
def admin_list_users(limit: int = Query(100, ge=1, le=500)) -> ListResponse:
    return ListResponse(items=list_profiles(limit=limit))


@app.post("/v1/admin/users", response_model=ListResponse, dependencies=[Depends(require_api_key)])
def admin_create_user(request: Request, body: AdminUserCreate) -> ListResponse:
    row = execute_returning(
        """
        insert into profiles (email, display_name, is_pro)
        values (%s, %s, %s)
        returning *
        """,
        (body.email, body.display_name or body.email, body.is_pro),
    )
    if not row:
        raise HTTPException(status_code=400, detail="User create failed")
    audit_security_event(event="admin_user_created", request=request, user_id=str(row["id"]), metadata={"is_pro": body.is_pro})
    return ListResponse(items=[row])


@app.patch("/v1/admin/users/{user_id}", dependencies=[Depends(require_api_key)])
def admin_patch_user(request: Request, user_id: UUID, body: AdminUserPatch):
    fields = {k: v for k, v in body.model_dump().items() if v is not None}
    if not fields:
        raise HTTPException(status_code=400, detail="No fields to update")
    assignments = ", ".join(f"{field} = %s" for field in fields)
    row = execute_returning(
        f"update profiles set {assignments} where id = %s and deleted_at is null returning *",
        (*fields.values(), user_id),
    )
    if not row:
        raise HTTPException(status_code=404, detail="User not found")
    audit_security_event(event="admin_user_changed", request=request, user_id=str(user_id), metadata={"fields": sorted(fields)})
    return row


@app.delete("/v1/admin/users/{user_id}", response_model=OkResponse, dependencies=[Depends(require_api_key)])
def admin_delete_user(request: Request, user_id: UUID) -> OkResponse:
    _purge_account(user_id)
    audit_security_event(event="admin_user_deleted", request=request, user_id=str(user_id))
    return OkResponse()


@app.get("/v1/admin/recipes", response_model=ListResponse, dependencies=[Depends(require_api_key)])
def admin_list_recipes(limit: int = Query(50, ge=1, le=500)) -> ListResponse:
    return ListResponse(items=list_recipes(limit=limit))


@app.get("/v1/admin/jobs", response_model=ListResponse, dependencies=[Depends(require_api_key)])
def admin_list_jobs(limit: int = Query(50, ge=1, le=500)) -> ListResponse:
    return ListResponse(items=list_jobs(limit=limit))


@app.get("/v1/admin/usage", response_model=ListResponse, dependencies=[Depends(require_api_key)])
def admin_usage(limit: int = Query(100, ge=1, le=1000)) -> ListResponse:
    return ListResponse(items=fetch_all("select * from usage_events order by created_at desc limit %s", (limit,)))
