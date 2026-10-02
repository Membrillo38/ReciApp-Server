from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

import app.apple_notifications as apple_notifications
from app.models import SubscriptionRestoreRequest


def _transaction(user_id, *, expires_at=None, revocation_date=None, product_id="reciapp_an_4499_t3"):
    now = datetime.now(timezone.utc)
    return {
        "bundleId": "com.membri.reciapp",
        "environment": "Production",
        "productId": product_id,
        "transactionId": "1000123456789",
        "appAccountToken": str(user_id),
        "purchaseDate": int((now - timedelta(days=2)).timestamp() * 1000),
        "expiresDate": int((expires_at or now + timedelta(days=28)).timestamp() * 1000),
        "revocationDate": revocation_date,
    }


def _configure(monkeypatch, transaction, profile):
    monkeypatch.setattr(
        apple_notifications,
        "settings",
        SimpleNamespace(
            maintenance_mode=False,
            apple_root_ca_pem="configured-root",
            apple_bundle_id="com.membri.reciapp",
            apple_environment="Production",
        ),
    )
    monkeypatch.setattr(apple_notifications, "verify_jws", lambda _: transaction)
    monkeypatch.setattr(apple_notifications, "fetch_one", lambda *_: profile)


def test_restore_applies_verified_active_transaction_to_authenticated_user(monkeypatch):
    user_id = uuid4()
    transaction = _transaction(user_id)
    profile = {"is_pro": True, "pro_expires_at": datetime.now(timezone.utc) + timedelta(days=28)}
    _configure(monkeypatch, transaction, profile)
    applied = []
    monkeypatch.setattr(
        apple_notifications,
        "_apply_notification_to_profile",
        lambda *args: applied.append(args) or "updated",
    )

    result = apple_notifications.restore_subscription_for_user(user_id, ["header.payload.signature"])

    assert result["restored"] is True
    assert result["is_pro"] is True
    assert applied[0][0] == user_id
    assert applied[0][1] == "apple-restore:1000123456789"
    # Reconciliation time must be newer than the transaction's original
    # purchase date so it can recover from a stale server-side event.
    assert applied[0][2] > datetime.fromtimestamp(transaction["purchaseDate"] / 1000, tz=timezone.utc)


def test_restore_accepts_legacy_active_subscription_product(monkeypatch):
    user_id = uuid4()
    transaction = _transaction(user_id, product_id="reciapp_wk")
    profile = {"is_pro": True, "pro_expires_at": datetime.now(timezone.utc) + timedelta(days=5)}
    _configure(monkeypatch, transaction, profile)
    applied = []
    monkeypatch.setattr(
        apple_notifications,
        "_apply_notification_to_profile",
        lambda *args: applied.append(args) or "updated",
    )

    result = apple_notifications.restore_subscription_for_user(user_id, ["header.payload.signature"])

    assert result["is_pro"] is True
    assert applied[0][1] == "apple-restore:1000123456789"


@pytest.mark.parametrize(
    "transaction_changes",
    [
        {"expires_at": datetime.now(timezone.utc) - timedelta(seconds=1)},
        {"revocation_date": 1},
    ],
)
def test_restore_does_not_grant_expired_or_revoked_transaction(monkeypatch, transaction_changes):
    user_id = uuid4()
    transaction = _transaction(user_id, **transaction_changes)
    _configure(monkeypatch, transaction, {"is_pro": False, "pro_expires_at": None})
    applied = []
    monkeypatch.setattr(apple_notifications, "_apply_notification_to_profile", lambda *args: applied.append(args))

    result = apple_notifications.restore_subscription_for_user(user_id, ["header.payload.signature"])

    assert result["restored"] is False
    assert applied == []


def test_restore_rejects_transaction_for_different_account(monkeypatch):
    transaction = _transaction(uuid4())
    _configure(monkeypatch, transaction, {"is_pro": False, "pro_expires_at": None})

    with pytest.raises(HTTPException) as error:
        apple_notifications.restore_subscription_for_user(uuid4(), ["header.payload.signature"])

    assert error.value.status_code == 403


def test_restore_request_accepts_all_supported_products_and_rejects_extra_items():
    supported_product_count = len(apple_notifications._RECIAPP_SUBSCRIPTION_PRODUCTS)
    signed_transactions = [f"{index:04d}{'a' * 16_376}.b.c" for index in range(supported_product_count)]

    request = SubscriptionRestoreRequest(signed_transactions=signed_transactions)

    assert len(request.signed_transactions) == supported_product_count == 11
    assert len(request.model_dump_json().encode("utf-8")) < 262_144
    with pytest.raises(ValidationError):
        SubscriptionRestoreRequest(signed_transactions=signed_transactions + ["extra.payload.signature"])
