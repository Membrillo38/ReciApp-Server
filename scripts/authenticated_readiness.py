#!/usr/bin/env python3
"""Read-only post-deploy acceptance runner. Never logs tokens or response bodies."""

from __future__ import annotations

import argparse
import json
import os
import statistics
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from urllib.parse import unquote, urlparse


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_NO_REDIRECT_OPENER = urllib.request.build_opener(_NoRedirectHandler()).open


@dataclass(frozen=True)
class Probe:
    path: str
    status: int
    latency_ms: float
    request_id: str | None


def _request(base_url: str, path: str, *, token: str | None, opener=_NO_REDIRECT_OPENER) -> Probe:
    headers = {"User-Agent": "reciapp-readiness/1.0", "Accept": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(f"{base_url.rstrip('/')}{path}", headers=headers, method="GET")
    started = time.perf_counter()
    try:
        with opener(request, timeout=20) as response:
            raw = response.read(1_000_001)
            if len(raw) > 1_000_000:
                raise RuntimeError("response_too_large")
            json.loads(raw)
            status = int(response.status)
            request_id = response.headers.get("X-Request-ID") or response.headers.get("X-Correlation-ID")
    except Exception as exc:
        raise RuntimeError(f"probe_failed path={path} error_type={type(exc).__name__}") from None
    return Probe(path, status, (time.perf_counter() - started) * 1000, request_id)


def run_acceptance(
    base_url: str,
    token: str,
    cycles: int,
    *,
    expected_host: str,
    opener=_NO_REDIRECT_OPENER,
) -> dict:
    parsed = urlparse(base_url)
    decoded_path = unquote(parsed.path)
    if (
        parsed.scheme != "https"
        or not parsed.netloc
        or parsed.username
        or parsed.password
        or "\\" in decoded_path
        or "//" in decoded_path
        or any(segment in {".", ".."} for segment in decoded_path.split("/"))
        or parsed.query
        or parsed.fragment
        or parsed.port not in {None, 443}
    ):
        raise ValueError("API base URL must be a credential-free HTTPS root")
    if not expected_host or parsed.hostname != expected_host.lower().rstrip("."):
        raise ValueError("API base URL host does not match RECIAPP_EXPECTED_API_HOST")
    if not token or any(char.isspace() for char in token):
        raise ValueError("RECIAPP_ACCESS_TOKEN is missing or invalid")
    if cycles != 100:
        raise ValueError("Acceptance requires exactly 100 library/profile cycles")

    probes = [
        _request(base_url, "/health", token=None, opener=opener),
        _request(base_url, "/ready", token=None, opener=opener),
    ]
    for _ in range(cycles):
        probes.append(_request(base_url, "/v1/me/recipes", token=token, opener=opener))
        probes.append(_request(base_url, "/v1/me", token=token, opener=opener))
    if any(probe.status != 200 for probe in probes):
        raise RuntimeError("acceptance_failed non_200_response")
    library = [probe.latency_ms for probe in probes if probe.path == "/v1/me/recipes"]
    profile = [probe.latency_ms for probe in probes if probe.path == "/v1/me"]
    request_ids = [probe.request_id for probe in probes if probe.request_id]
    library_p95 = statistics.quantiles(library, n=100, method="inclusive")[94]
    profile_p95 = statistics.quantiles(profile, n=100, method="inclusive")[94]
    if len(request_ids) != len(probes):
        raise RuntimeError("acceptance_failed missing_request_id")
    if library_p95 > 800 or profile_p95 > 800:
        raise RuntimeError("acceptance_failed hot_read_p95_exceeded")
    return {
        "status": "ok",
        "cycles": cycles,
        "requests": len(probes),
        "request_ids_present": len(request_ids),
        "request_ids_unique": len(set(request_ids)),
        "request_ids_missing": len(probes) - len(request_ids),
        "library_latency_p95_ms": round(library_p95, 2),
        "profile_latency_p95_ms": round(profile_p95, 2),
        "hot_latency_max_ms": round(max([*library, *profile]), 2),
        "target_hot_read_p95_ms": 800,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--cycles", type=int, default=100)
    args = parser.parse_args()
    token = os.environ.get("RECIAPP_ACCESS_TOKEN", "")
    expected_host = os.environ.get("RECIAPP_EXPECTED_API_HOST", "")
    result = run_acceptance(
        args.base_url,
        token,
        args.cycles,
        expected_host=expected_host,
    )
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
