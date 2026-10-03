import pytest
from pathlib import Path


def test_superwall_is_configured_and_identified_with_app_user(ios_root: Path):
    service = (ios_root / "ReciApp/Services/SubscriptionService.swift").read_text(encoding="utf-8")
    auth = (ios_root / "ReciApp/Services/AuthService.swift").read_text(encoding="utf-8")
    assert "apiKey: AppConfig.superwallPublicKey" in service
    assert "Superwall.shared.identify(userId: value)" in service
    assert 'attributes["user_id"] = identifiedUserID' in service
    assert "Superwall.shared.setUserAttributes(attributes)" in service
    assert "func restorePurchases" in service
    assert "SubscriptionService.shared.identify" in auth


def test_signed_storekit_restore_is_wired_from_ios_to_server(ios_root: Path):
    source = Path("app/main.py").read_text(encoding="utf-8")
    api = (ios_root / "ReciApp/Services/APIClient.swift").read_text(encoding="utf-8")
    service = (ios_root / "ReciApp/Services/SubscriptionService.swift").read_text(encoding="utf-8")
    assert '@app.post("/v1/me/subscription/restore"' in source
    assert 'request("v1/me/subscription/restore", method: "POST"' in api
    assert "Transaction.currentEntitlements" in service
    assert "result.jwsRepresentation" in service


def test_superwall_webhook_rejects_unsigned_payloads_in_production_path():
    source = Path("app/main.py").read_text(encoding="utf-8")
    assert 'if not settings.superwall_webhook_secret:' in source
    assert 'raise HTTPException(status_code=503, detail="Webhook verification unavailable")' in source
    assert 'raise HTTPException(status_code=400, detail="Invalid application id")' in source
    assert "apply_superwall_event(payload, event_id=svix_id)" in source


def test_server_extracts_app_user_id_not_hosted_auth():
    source = Path("app/superwall.py").read_text(encoding="utf-8")
    assert "def extract_app_user_id" in source
    assert "no_user_id" in source
    assert "no_supabase_user_id" not in source
