import json
from pathlib import Path
from uuid import uuid4
from types import SimpleNamespace

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec

from app.apns import APNsClient, _LOCALIZATIONS
from app.config import settings
from app.localization import SUPPORTED_LANGUAGE_CODES
from app.auth import AuthUser
from app import main
from app.models import PushDeviceRequest


class FakeResponse:
    def __init__(self, status_code: int, *, reason: str = "", headers: dict | None = None):
        self.status_code = status_code
        self.headers = headers or {}
        self._reason = reason

    def json(self):
        return {"reason": self._reason}


class FakeHTTPClient:
    def __init__(self, response: FakeResponse):
        self.response = response
        self.call = None

    def post(self, url: str, **kwargs):
        self.call = (url, kwargs)
        return self.response


def _configure_apns(monkeypatch):
    monkeypatch.setattr(settings, "apns_enabled", True)
    monkeypatch.setattr(settings, "apns_team_id", "TEAMID1234")
    monkeypatch.setattr(settings, "apns_key_id", "KEYID12345")
    monkeypatch.setattr(settings, "apns_auth_key", "test-key")
    monkeypatch.setattr(settings, "apns_topic", "com.membri.reciapp")


def test_provider_token_uses_es256_team_and_key_id(monkeypatch):
    private_key = ec.generate_private_key(ec.SECP256R1())
    private_pem = private_key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()
    _configure_apns(monkeypatch)
    monkeypatch.setattr(settings, "apns_auth_key", private_pem)

    token = APNsClient()._token()

    claims = jwt.decode(token, private_key.public_key(), algorithms=["ES256"])
    assert claims["iss"] == "TEAMID1234"
    assert isinstance(claims["iat"], int)
    assert jwt.get_unverified_header(token)["kid"] == "KEYID12345"


def test_send_uses_selected_app_language_and_sandbox_endpoint(monkeypatch):
    _configure_apns(monkeypatch)
    client = APNsClient()
    fake = FakeHTTPClient(FakeResponse(200))
    client._client = fake
    client._token = lambda: "provider-token"
    job_id, recipe_id = uuid4(), uuid4()

    result = client.send(
        token="a" * 64,
        environment="sandbox",
        job_id=job_id,
        recipe_id=recipe_id,
        language_code="es-ES",
    )

    url, request = fake.call
    assert result.status == "sent"
    assert url == f"https://api.sandbox.push.apple.com/3/device/{'a' * 64}"
    assert request["headers"]["apns-push-type"] == "alert"
    assert request["headers"]["apns-collapse-id"] == f"reciapp-import-{job_id}"
    payload = json.loads(request["content"])
    assert payload["aps"]["alert"] == _LOCALIZATIONS["es-ES"]
    assert payload["aps"]["alert"]["title"] == "Tu receta está lista"
    assert str(recipe_id) in request["content"]


def test_apns_localizations_cover_every_server_language():
    assert set(_LOCALIZATIONS) == set(SUPPORTED_LANGUAGE_CODES)
    assert all(set(copy) == {"title", "body"} for copy in _LOCALIZATIONS.values())


def test_apns_disables_when_topic_does_not_match_app_bundle(monkeypatch):
    _configure_apns(monkeypatch)
    monkeypatch.setattr(settings, "apple_bundle_id", "com.example.other")
    client = APNsClient()
    client._token = lambda: "provider-token"

    assert client.enabled is False


def test_unknown_apns_language_falls_back_to_english(monkeypatch):
    _configure_apns(monkeypatch)
    client = APNsClient()
    fake = FakeHTTPClient(FakeResponse(200))
    client._client = fake
    client._token = lambda: "provider-token"

    result = client.send(
        token="e" * 64,
        environment="production",
        job_id=uuid4(),
        recipe_id=uuid4(),
        language_code="xx-XX",
    )

    assert result.status == "sent"
    assert json.loads(fake.call[1]["content"])["aps"]["alert"] == _LOCALIZATIONS["en-US"]


def test_completion_trigger_includes_translations_and_excludes_cache_hits():
    migration = Path("migrations/010_apns_recipe_completion.sql").read_text(encoding="utf-8")

    assert "new.job_kind not in ('extract', 'translation')" in migration
    assert "or new.cache_hit" in migration


def test_send_marks_unregistered_tokens_for_removal(monkeypatch):
    _configure_apns(monkeypatch)
    client = APNsClient()
    client._client = FakeHTTPClient(FakeResponse(410, reason="Unregistered"))
    client._token = lambda: "provider-token"

    result = client.send(token="b" * 64, environment="production", job_id=uuid4(), recipe_id=uuid4())

    assert result.status == "invalid_device"
    assert result.error_code == "Unregistered"


def test_send_respects_apns_retry_after(monkeypatch):
    _configure_apns(monkeypatch)
    client = APNsClient()
    client._client = FakeHTTPClient(FakeResponse(429, reason="TooManyRequests", headers={"retry-after": "77"}))
    client._token = lambda: "provider-token"

    result = client.send(token="c" * 64, environment="production", job_id=uuid4(), recipe_id=uuid4())

    assert result.status == "retry"
    assert result.retry_after_seconds == 77


def test_registration_falls_back_when_durable_worker_is_not_healthy(monkeypatch):
    deleted = []
    monkeypatch.setattr(main, "apns_client", SimpleNamespace(enabled=True))
    monkeypatch.setattr(main, "recipe_worker_is_healthy", lambda: False)
    monkeypatch.setattr(main, "upsert_push_device", lambda **_kwargs: pytest.fail("must not register device"))
    monkeypatch.setattr(main, "delete_push_device", lambda **kwargs: deleted.append(kwargs))
    user = AuthUser(id=uuid4(), email=None, display_name=None, is_pro=False, pro_expires_at=None)
    token = "d" * 64

    response = main.register_push_device(
        PushDeviceRequest(token=token, environment="production", language="es-ES"),
        user,
    )

    assert response.registered is False
    assert response.push_enabled is False
    assert deleted == [{"user_id": user.id, "token": token}]
