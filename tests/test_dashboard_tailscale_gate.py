from __future__ import annotations

from fastapi import Request
from starlette.datastructures import Headers

from app.security import dashboard_publicly_blocked, is_public_dashboard_host


def _req(path: str, host: str, forwarded_host: str | None = None) -> Request:
    headers = {"host": host}
    if forwarded_host:
        headers["x-forwarded-host"] = forwarded_host
    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "headers": Headers(headers).raw,
        "client": ("100.123.33.15", 12345),
        "server": ("100.123.33.15", 8090),
    }
    return Request(scope)


def test_public_sslip_dashboard_blocked():
    req = _req("/dashboard", "51-255-43-100.sslip.io")
    assert is_public_dashboard_host(req)
    assert dashboard_publicly_blocked(req)


def test_public_sslip_via_forwarded_host_blocked():
    req = _req("/dashboard/ips", "10.0.1.11:8000", forwarded_host="51-255-43-100.sslip.io")
    assert dashboard_publicly_blocked(req)


def test_public_acasillas_api_host_dashboard_blocked():
    req = _req("/dashboard", "api.acasillas.com")
    assert is_public_dashboard_host(req)
    assert dashboard_publicly_blocked(req)


def test_public_acasillas_api_forwarded_host_dashboard_blocked():
    req = _req("/dashboard/ips", "10.0.1.11:8000", forwarded_host="api.acasillas.com")
    assert dashboard_publicly_blocked(req)


def test_tailscale_host_dashboard_allowed():
    req = _req("/dashboard", "100.123.33.15:8090")
    assert not is_public_dashboard_host(req)
    assert not dashboard_publicly_blocked(req)


def test_api_paths_not_affected():
    req = _req("/v1/recipes", "51-255-43-100.sslip.io")
    assert not dashboard_publicly_blocked(req)
