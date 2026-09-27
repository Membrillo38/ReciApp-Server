from __future__ import annotations

import base64
import json
import time
from dataclasses import dataclass
from pathlib import Path
from threading import Lock
from uuid import UUID

import httpx
import jwt

from app.config import settings

_RETRYABLE_REASONS = {"ExpiredProviderToken", "TooManyRequests", "InternalServerError", "ServiceUnavailable"}
_KNOWN_REASONS = _RETRYABLE_REASONS | {
    "BadDeviceToken",
    "DeviceTokenNotForTopic",
    "InvalidProviderToken",
    "MissingProviderToken",
    "PayloadEmpty",
    "PayloadTooLarge",
    "TopicDisallowed",
    "Unregistered",
}
_LOCALIZATIONS = json.loads(
    Path(__file__).with_name("apns_localizations.json").read_text(encoding="utf-8")
)


@dataclass(frozen=True)
class APNsResult:
    status: str  # sent | retry | invalid_device | failed
    error_code: str | None = None
    retry_after_seconds: int = 0


class APNsClient:
    def __init__(self) -> None:
        self._client: httpx.Client | None = None
        self._provider_token: str | None = None
        self._provider_token_created_at = 0
        self._lock = Lock()

    @property
    def enabled(self) -> bool:
        configured = bool(
            settings.apns_enabled
            and len(settings.apns_team_id) == 10
            and len(settings.apns_key_id) == 10
            and (settings.apns_auth_key or settings.apns_auth_key_b64)
            and settings.apns_topic
            and settings.apns_topic == settings.apple_bundle_id
        )
        if not configured:
            return False
        try:
            self._token()
        except Exception:
            return False
        return True

    def _token(self) -> str:
        now = int(time.time())
        with self._lock:
            if self._provider_token and now - self._provider_token_created_at < 50 * 60:
                return self._provider_token
            private_key = settings.apns_auth_key
            if settings.apns_auth_key_b64:
                private_key = base64.b64decode(settings.apns_auth_key_b64, validate=True).decode("utf-8")
            private_key = private_key.replace("\\n", "\n")
            token = jwt.encode(
                {"iss": settings.apns_team_id, "iat": now},
                private_key,
                algorithm="ES256",
                headers={"kid": settings.apns_key_id},
            )
            self._provider_token = token
            self._provider_token_created_at = now
            return token

    def send(
        self,
        *,
        token: str,
        environment: str,
        job_id: UUID,
        recipe_id: UUID,
        language_code: str = "en-US",
    ) -> APNsResult:
        if not self.enabled:
            return APNsResult("retry", "configuration_missing")
        if environment not in {"sandbox", "production"}:
            return APNsResult("failed", "invalid_environment")
        try:
            authorization = self._token()
        except Exception:
            return APNsResult("failed", "provider_auth_invalid")

        host = "api.sandbox.push.apple.com" if environment == "sandbox" else "api.push.apple.com"
        alert = _LOCALIZATIONS.get(language_code, _LOCALIZATIONS["en-US"])
        payload = {
            "aps": {
                "alert": alert,
                "sound": "default",
                "thread-id": "reciapp.import.completed",
            },
            "job_id": str(job_id),
            "recipe_id": str(recipe_id),
        }
        headers = {
            "authorization": f"bearer {authorization}",
            "apns-topic": settings.apns_topic,
            "apns-push-type": "alert",
            "apns-priority": "10",
            "apns-collapse-id": f"reciapp-import-{job_id}",
            "content-type": "application/json",
        }
        try:
            with self._lock:
                if self._client is None:
                    self._client = httpx.Client(http2=True, timeout=httpx.Timeout(10.0, connect=5.0))
                client = self._client
            response = client.post(
                f"https://{host}/3/device/{token}",
                headers=headers,
                content=json.dumps(payload, separators=(",", ":")),
            )
        except httpx.RequestError:
            return APNsResult("retry", "network_error")

        if response.status_code == 200:
            return APNsResult("sent")
        try:
            reason = str(response.json().get("reason") or "")
        except (ValueError, AttributeError):
            reason = ""
        error_code = reason if reason in _KNOWN_REASONS else f"http_{response.status_code}"
        if response.status_code == 410 or reason == "Unregistered":
            return APNsResult("invalid_device", error_code)
        if response.status_code == 429 or response.status_code >= 500 or reason in _RETRYABLE_REASONS:
            if reason == "ExpiredProviderToken":
                with self._lock:
                    self._provider_token = None
            try:
                retry_after = int(response.headers.get("retry-after", "0"))
            except ValueError:
                retry_after = 0
            return APNsResult("retry", error_code, max(0, min(retry_after, 3600)))
        return APNsResult("failed", error_code)


apns_client = APNsClient()
