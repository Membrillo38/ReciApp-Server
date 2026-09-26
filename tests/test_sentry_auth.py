from __future__ import annotations

import asyncio
from types import SimpleNamespace
from uuid import uuid4

import httpx
from fastapi import HTTPException
from starlette.requests import Request

from app import main
from app.auth_tokens import rotate_refresh_token
from app.models import AuthAppleRequest
from app.observability import _scrub_event


def _request(path="/v1/auth/apple", request_id="ios-test-12345678"):
    request = Request({"type": "http", "method": "POST", "path": path, "headers": [(b"x-request-id", request_id.encode())]})
    request.state.request_id = request_id
    request.state.correlation_id = "corr-test-12345678"
    return request


def test_login_correct(monkeypatch):
    monkeypatch.setattr(main.settings, "auth_jwt_secret", "test-secret")
    user_id = uuid4()
    row = {"id": user_id, "email": "a@example.com", "display_name": None, "is_pro": False, "pro_expires_at": None}
    monkeypatch.setattr(main, "verify_apple_identity_token", lambda *a, **k: SimpleNamespace(apple_sub="sub", email="a@example.com"))
    monkeypatch.setattr(main, "_upsert_apple_profile", lambda *a, **k: row)
    monkeypatch.setattr(main, "_persist_apple_authorization_code", lambda *a, **k: None)
    monkeypatch.setattr(main, "create_access_token", lambda *a, **k: "access")
    monkeypatch.setattr(main, "create_refresh_token", lambda *a, **k: "refresh")
    result = main.auth_apple(_request(), AuthAppleRequest(identity_token="opaque", nonce="nonce"))
    assert result.access_token == "access"


def test_apple_token_invalid_and_nonce_invalid(monkeypatch):
    monkeypatch.setattr(main.settings, "auth_jwt_secret", "test-secret")
    monkeypatch.setattr(main, "verify_apple_identity_token", lambda *a, **k: (_ for _ in ()).throw(ValueError("Invalid Apple nonce")))
    with __import__("pytest").raises(HTTPException) as caught:
        main.auth_apple(_request(), AuthAppleRequest(identity_token="opaque", nonce="nonce"))
    assert caught.value.status_code == 401
    assert caught.value.detail["code"] == "APPLE_NONCE_INVALID"


def test_refresh_expired(monkeypatch):
    monkeypatch.setattr(main.settings, "auth_jwt_secret", "test-secret")
    monkeypatch.setattr(main, "rotate_refresh_token", lambda _token, _request_id=None: None)
    with __import__("pytest").raises(HTTPException) as caught:
        main.auth_refresh(
            _request("/v1/auth/refresh"),
            type("Body", (), {"refresh_token": "opaque", "request_id": None})(),
        )
    assert caught.value.detail["code"] == "REFRESH_TOKEN_EXPIRED"


def test_response_401_and_500_are_consistent():
    request = _request()
    response = asyncio.run(main.auth_http_exception(request, HTTPException(401, {"code": "AUTH_BAD", "message": "bad"})))
    assert response.status_code == 401
    assert response.body.decode().find('"request_id":"ios-test-12345678"') >= 0
    response = asyncio.run(main.unexpected_exception(request, RuntimeError("boom")))
    assert response.status_code == 500
    assert b"INTERNAL_SERVER_ERROR" in response.body


def test_timeout_handler_returns_503_and_ids():
    request = _request()
    response = asyncio.run(main.upstream_request_error(request, httpx.ReadTimeout("timeout")))
    assert response.status_code == 503
    assert response.headers.get("Retry-After") == "1"


def test_request_and_correlation_ids_and_secret_scrub():
    event = {"request": {"data": {"refreshToken": "secret"}, "headers": {"authorization": "Bearer secret", "cookie": "x"}}, "contexts": {"body": "secret"}}
    cleaned = _scrub_event(event, {})
    assert "data" not in cleaned["request"]
    assert "authorization" not in cleaned["request"]["headers"]
    assert cleaned["contexts"]["body"] == "[Filtered]"
