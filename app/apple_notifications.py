from __future__ import annotations

import base64
import hashlib
import json
from datetime import datetime, timezone
from uuid import UUID

from fastapi import HTTPException
from psycopg.errors import UniqueViolation

from app.config import settings
from app.db import execute_returning, fetch_one

_RECIAPP_SUBSCRIPTION_PRODUCTS = frozenset({
    "reciapp_wk_699", "reciapp_wk_999", "reciapp_wk_1299",
    "reciapp_an_3499_t3", "reciapp_an_3499_t7",
    "reciapp_an_4499_t3", "reciapp_an_4499_t7",
    "reciapp_an_5999_t3", "reciapp_an_5999_t7",
    # Active subscriptions from the pre-experiment catalog remain restorable.
    "reciapp_wk", "reciapp_an_3trial",
})


def _b64(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def _verify_signature(public_key, signature: bytes, message: bytes, algorithm) -> None:
    from cryptography.hazmat.primitives import asymmetric, hashes
    from cryptography.hazmat.primitives.asymmetric import ec, padding
    from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature

    if isinstance(public_key, asymmetric.rsa.RSAPublicKey):
        public_key.verify(signature, message, padding.PKCS1v15(), algorithm)
    elif isinstance(public_key, ec.EllipticCurvePublicKey):
        # JWS ES256 encodes ECDSA as fixed-width R || S; cryptography expects DER.
        width = (public_key.curve.key_size + 7) // 8
        if len(signature) == 2 * width:
            r = int.from_bytes(signature[:width], "big")
            s = int.from_bytes(signature[width:], "big")
            signature = encode_dss_signature(r, s)
        public_key.verify(signature, message, ec.ECDSA(algorithm))
    else:
        raise ValueError("Unsupported Apple certificate key")


def verify_jws(compact: str) -> dict:
    """Verify Apple JWS signature and certificate chain; never trust plain JSON."""
    root_pem = settings.apple_root_ca_pem.replace("\\n", "\n").strip()
    if compact.count(".") != 2 or not root_pem:
        raise ValueError("Apple JWS verification is not configured")
    try:
        from cryptography import x509
        from cryptography.hazmat.primitives import hashes, serialization
    except ImportError as exc:
        raise ValueError("Apple JWS verification dependency unavailable") from exc

    encoded_header, encoded_payload, encoded_signature = compact.split(".")
    header = json.loads(_b64(encoded_header))
    payload = json.loads(_b64(encoded_payload))
    if header.get("alg") != "ES256" or not header.get("x5c"):
        raise ValueError("Unsupported Apple JWS")

    chain = [x509.load_der_x509_certificate(base64.b64decode(value)) for value in header["x5c"]]
    root = x509.load_pem_x509_certificate(root_pem.encode("utf-8"))
    if chain[-1].fingerprint(hashes.SHA256()) != root.fingerprint(hashes.SHA256()):
        # Apple may omit the root from x5c; verify the final intermediate against
        # the configured root instead of accepting an arbitrary CA.
        _verify_signature(root.public_key(), chain[-1].signature, chain[-1].tbs_certificate_bytes, chain[-1].signature_hash_algorithm)
    for child, issuer in zip(chain, chain[1:]):
        _verify_signature(issuer.public_key(), child.signature, child.tbs_certificate_bytes, child.signature_hash_algorithm)
    _verify_signature(
        chain[0].public_key(),
        _b64(encoded_signature),
        f"{encoded_header}.{encoded_payload}".encode("ascii"),
        hashes.SHA256(),
    )
    return payload


def _record_notification(notification_id: str, payload: dict) -> bool:
    existing = fetch_one("select status from apple_notification_events where event_id = %s limit 1", (notification_id,))
    if existing:
        return existing.get("status") not in {"processed", "skipped"}
    try:
        execute_returning(
            """
            insert into apple_notification_events (event_id, notification_type, signed_payload_sha256, payload, status)
            values (%s, %s, %s, %s, %s)
            returning event_id
            """,
            (
                payload["event_id"],
                payload.get("notification_type"),
                payload["signed_payload_sha256"],
                payload.get("payload") or {},
                payload.get("status") or "received",
            ),
        )
    except UniqueViolation:
        existing = fetch_one("select status from apple_notification_events where event_id = %s limit 1", (notification_id,))
        if not existing:
            raise
        # Concurrent receipt insertion is not proof of successful processing.
        return existing.get("status") not in {"processed", "skipped"}
    return True


def _mark_notification(notification_id: str, status: str) -> None:
    row = execute_returning(
        """
        update apple_notification_events
           set status = %s
         where event_id = %s and status = any(%s)
        returning status
        """,
        (status, notification_id, ["received", "failed"]),
    )
    if not row:
        current = fetch_one("select status from apple_notification_events where event_id = %s limit 1", (notification_id,))
        if current and current.get("status") in {"processed", "skipped"}:
            return
        raise RuntimeError("Apple notification status was not persisted")


def _apply_notification_to_profile(user_uuid: UUID, event_id: str, event_at: datetime, update: dict) -> str:
    current = fetch_one(
        "select subscription_event_at, subscription_event_id, deleted_at from profiles where id = %s limit 1",
        (user_uuid,),
    )
    if not current or current.get("deleted_at"):
        return "profile_unavailable"
    if current.get("subscription_event_id") == event_id:
        return "duplicate"
    if current.get("subscription_event_at"):
        current_at = datetime.fromisoformat(str(current["subscription_event_at"]).replace("Z", "+00:00"))
        if current_at >= event_at:
            return "out_of_order"
    update = {**update, "subscription_event_at": event_at.isoformat(), "subscription_event_id": event_id}
    assignments = ", ".join(f"{field} = %s" for field in update)
    if execute_returning(
        f"""
        update profiles
           set {assignments}
         where id = %s
           and deleted_at is null
           and subscription_event_at is not distinct from %s
           and subscription_event_id is not distinct from %s
        returning id
        """,
        (*update.values(), user_uuid, current.get("subscription_event_at"), current.get("subscription_event_id")),
    ):
        return "updated"
    # Re-read after a lost compare-and-set; never blindly overwrite a newer
    # Apple or Superwall event and never re-create a concurrently deleted user.
    latest = fetch_one(
        "select subscription_event_at, subscription_event_id, deleted_at from profiles where id = %s limit 1",
        (user_uuid,),
    )
    if not latest or latest.get("deleted_at"):
        return "profile_unavailable"
    if latest.get("subscription_event_id") == event_id:
        return "duplicate"
    if latest.get("subscription_event_at"):
        latest_at = datetime.fromisoformat(str(latest["subscription_event_at"]).replace("Z", "+00:00"))
        if latest_at >= event_at:
            return "out_of_order"
    raise RuntimeError("Subscription changed concurrently; retry Apple notification")


def process_signed_notification(signed_payload: str) -> dict:
    if settings.maintenance_mode:
        raise HTTPException(status_code=503, detail="Maintenance in progress. Retry.", headers={"Retry-After": "30"})
    notification = verify_jws(signed_payload)
    notification_id = str(notification.get("notificationUUID") or "")
    if not notification_id:
        raise ValueError("Apple notification id required")
    data = notification.get("data") or {}
    if data.get("bundleId") and data["bundleId"] != settings.apple_bundle_id:
        return {"ok": True, "skipped": "wrong_bundle"}
    if data.get("environment") and data["environment"] != settings.apple_environment:
        return {"ok": True, "skipped": "wrong_environment"}
    signed_date = notification.get("signedDate")
    if isinstance(signed_date, bool) or not isinstance(signed_date, (int, float)) or signed_date <= 0:
        raise ValueError("Apple notification signed date required")
    try:
        event_at = datetime.fromtimestamp(signed_date / 1000, tz=timezone.utc)
    except (ValueError, OverflowError, OSError):
        raise ValueError("Invalid Apple notification signed date") from None

    transaction = {}
    if data.get("signedTransactionInfo"):
        transaction = verify_jws(data["signedTransactionInfo"])
    user_id = transaction.get("appAccountToken")
    try:
        user_uuid = UUID(str(user_id))
    except (ValueError, TypeError):
        user_uuid = None

    event_type = str(notification.get("notificationType") or "").upper()
    expiry_ms = transaction.get("expiresDate")
    expires_at = None
    if isinstance(expiry_ms, (int, float)):
        expires_at = datetime.fromtimestamp(float(expiry_ms) / 1000, tz=timezone.utc)
    off = event_type in {"EXPIRED", "REFUND", "REVOKE"} or bool(transaction.get("revocationDate"))
    update = {"is_pro": not off, "pro_expires_at": expires_at.isoformat() if expires_at else None}
    redacted = {"notificationType": event_type, "environment": data.get("environment"), "productId": transaction.get("productId")}
    try:
        if not _record_notification(notification_id, {
            "event_id": notification_id,
            "notification_type": event_type,
            "signed_payload_sha256": hashlib.sha256(signed_payload.encode()).hexdigest(),
            "payload": redacted,
            "status": "received",
        }):
            return {"ok": True, "skipped": "duplicate", "event_id": notification_id}
        outcome = _apply_notification_to_profile(user_uuid, "apple:" + notification_id, event_at, update) if user_uuid else "no_user_id"
        _mark_notification(notification_id, "processed" if outcome in {"updated", "duplicate"} else "skipped")
    except Exception:
        try:
            _mark_notification(notification_id, "failed")
        except Exception:
            pass
        raise HTTPException(status_code=503, detail="Apple notification processing temporarily unavailable", headers={"Retry-After": "1"}) from None
    return {
        "ok": True, "event_id": notification_id, "updated": outcome == "updated",
        "is_pro": update["is_pro"] if outcome == "updated" else None,
        **({"skipped": outcome} if outcome != "updated" else {}),
    }


def restore_subscription_for_user(user_uuid: UUID, signed_transactions: list[str]) -> dict:
    """Verify StoreKit signed transactions before reconciling an authenticated restore."""
    if settings.maintenance_mode:
        raise HTTPException(status_code=503, detail="Maintenance in progress. Retry.", headers={"Retry-After": "30"})
    if not settings.apple_root_ca_pem:
        raise HTTPException(status_code=503, detail="Apple transaction verification unavailable")

    now = datetime.now(timezone.utc)
    valid: list[tuple[datetime, str, datetime]] = []
    for compact in signed_transactions:
        transaction = verify_jws(compact)
        if transaction.get("bundleId") != settings.apple_bundle_id:
            raise HTTPException(status_code=400, detail="Transaction belongs to another application")
        if transaction.get("environment") != settings.apple_environment:
            raise HTTPException(status_code=400, detail="Transaction belongs to another environment")
        product_id = transaction.get("productId")
        if not isinstance(product_id, str) or product_id not in _RECIAPP_SUBSCRIPTION_PRODUCTS:
            continue
        app_account_token = transaction.get("appAccountToken")
        if app_account_token:
            try:
                if UUID(str(app_account_token)) != user_uuid:
                    raise HTTPException(status_code=403, detail="Transaction belongs to another account")
            except (ValueError, TypeError):
                raise HTTPException(status_code=400, detail="Invalid transaction account token") from None
        transaction_id = transaction.get("transactionId")
        purchase_ms = transaction.get("purchaseDate")
        expiry_ms = transaction.get("expiresDate")
        if not isinstance(transaction_id, str) or not transaction_id.isdigit():
            continue
        if isinstance(purchase_ms, bool) or not isinstance(purchase_ms, (int, float)):
            continue
        if isinstance(expiry_ms, bool) or not isinstance(expiry_ms, (int, float)):
            continue
        try:
            purchased_at = datetime.fromtimestamp(purchase_ms / 1000, tz=timezone.utc)
            expires_at = datetime.fromtimestamp(expiry_ms / 1000, tz=timezone.utc)
        except (ValueError, OverflowError, OSError):
            continue
        if expires_at <= now or transaction.get("revocationDate") is not None:
            continue
        valid.append((purchased_at, transaction_id, expires_at))

    if valid:
        # A restore presents the user's current StoreKit entitlements. Its
        # transaction's purchaseDate can be weeks or months older than a more
        # recent server notification (for example, a stale expiration event).
        # Ordering the reconciliation by purchaseDate would then discard this
        # verified active entitlement as out of order. Use the restore time as
        # the reconciliation event time; the signed transaction still supplies
        # the authoritative product and expiry, and revoked/expired entries
        # were filtered above.
        _, transaction_id, expires_at = max(valid)
        _apply_notification_to_profile(
            user_uuid,
            "apple-restore:" + transaction_id,
            now,
            {"is_pro": True, "pro_expires_at": expires_at.isoformat()},
        )

    profile = fetch_one(
        "select is_pro, pro_expires_at, deleted_at from profiles where id = %s limit 1",
        (user_uuid,),
    )
    if not profile or profile.get("deleted_at"):
        raise HTTPException(status_code=403, detail="Account unavailable")
    expires_at = profile.get("pro_expires_at")
    restored = bool(profile.get("is_pro")) and (
        not expires_at or datetime.fromisoformat(str(expires_at).replace("Z", "+00:00")) > now
    )
    return {
        "restored": restored,
        "is_pro": restored,
        "pro_expires_at": str(expires_at) if expires_at else None,
    }
