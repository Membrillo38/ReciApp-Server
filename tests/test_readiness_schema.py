import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

from app import db, main
from app.config import Settings


def test_settings_default_to_pinned_apple_root_ca_when_env_is_blank(monkeypatch):
    monkeypatch.setenv("APPLE_ROOT_CA_PEM", "")
    configured = Settings(_env_file=None)
    certificate = x509.load_pem_x509_certificate(configured.apple_root_ca_pem.encode("ascii"))
    assert certificate.fingerprint(hashes.SHA256()).hex() == (
        "63343abfb89a6a03ebb57e9b3f5fa7be7c4f5c756f3017b3a8c488c3653e9179"
    )


def _valid_root_certificate_pem():
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "readiness-test-root")])
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    certificate = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(days=1))
        .not_valid_after(now + timedelta(days=1))
        .sign(key, hashes.SHA256())
    )
    return certificate.public_bytes(serialization.Encoding.PEM).decode("utf-8")


def _valid_apns_private_key_pem():
    key = ec.generate_private_key(ec.SECP256R1())
    return key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode("utf-8")


def _configure_valid_apns(monkeypatch):
    monkeypatch.setattr(db.settings, "worker_enabled", True)
    monkeypatch.setattr(db.settings, "apns_enabled", True)
    monkeypatch.setattr(db.settings, "apns_team_id", "TEAM123456")
    monkeypatch.setattr(db.settings, "apns_key_id", "KEY1234567")
    monkeypatch.setattr(db.settings, "apns_auth_key", _valid_apns_private_key_pem())
    monkeypatch.setattr(db.settings, "apns_auth_key_b64", "")
    monkeypatch.setattr(db.settings, "apns_topic", "com.membri.reciapp")
    monkeypatch.setattr(db.settings, "apple_bundle_id", "com.membri.reciapp")
    monkeypatch.setattr(db.settings, "apns_environment", "production")


def test_container_healthcheck_uses_dependency_readiness():
    dockerfile = Path("Dockerfile").read_text(encoding="utf-8")
    assert "urlopen('http://127.0.0.1:8000/ready')" in dockerfile


def test_ready_requires_delivery_column_and_unique_index(monkeypatch):
    queries = []

    def fetch(_sql):
        queries.append(_sql)
        return {
            "delivery_column": True,
            "delivery_index": True,
            "refresh_replay_columns": True,
            "library_state_columns": True,
            "library_state_history": True,
            "library_state_rls": True,
            "library_state_service_policy": True,
            "push_device_columns": True,
            "push_delivery_columns": True,
            "push_completion_trigger": True,
            "push_logout_function": True,
            "runtime_db_privileges": True,
        }

    monkeypatch.setattr(
        db,
        "fetch_one",
        fetch,
    )
    monkeypatch.setattr(db.settings, "apple_root_ca_pem", _valid_root_certificate_pem())
    asyncio.run(db.probe_postgres())
    assert len(queries) == 1
    assert "udt_name = 'uuid'" in queries[0]
    assert "ix.indisunique" in queries[0]
    assert "ix.indisvalid" in queries[0]
    assert "ix.indisready" in queries[0]
    assert "(client_delivery_id IS NOT NULL)" in queries[0]
    assert "array['user_id', 'client_delivery_id']::name[]" in queries[0]
    assert "rotation_request_id" in queries[0]
    assert "rotation_retry_until" in queries[0]
    assert "rotated_token_hash" in queries[0]
    assert "user_library_state_history_is_array" in queries[0]
    assert "rolsuper or rolbypassrls" in queries[0]
    assert "has_table_privilege(current_user" in queries[0]
    assert "has_function_privilege(current_user" in queries[0]


@pytest.mark.parametrize(
    "schema_state",
    [
        {"delivery_column": False, "delivery_index": False},
        {"delivery_column": True, "delivery_index": False},
    ],
)
def test_ready_fails_closed_when_idempotency_migration_is_incomplete(monkeypatch, schema_state):
    monkeypatch.setattr(db, "fetch_one", lambda _sql: schema_state)
    with pytest.raises(ValueError, match="idempotency schema is missing"):
        asyncio.run(db.probe_postgres())


def test_ready_fails_closed_when_refresh_replay_columns_are_missing(monkeypatch):
    monkeypatch.setattr(
        db,
        "fetch_one",
        lambda _sql: {
            "delivery_column": True,
            "delivery_index": True,
            "refresh_replay_columns": False,
            "library_state_columns": True,
            "library_state_history": True,
            "library_state_rls": True,
            "library_state_service_policy": True,
            "push_device_columns": True,
            "push_delivery_columns": True,
            "push_completion_trigger": True,
            "push_logout_function": True,
        },
    )
    with pytest.raises(ValueError, match="refresh replay schema is missing"):
        asyncio.run(db.probe_postgres())


def test_ready_fails_closed_when_push_notification_schema_is_missing(monkeypatch):
    monkeypatch.setattr(
        db,
        "fetch_one",
        lambda _sql: {
            "delivery_column": True,
            "delivery_index": True,
            "refresh_replay_columns": True,
            "library_state_columns": True,
            "library_state_history": True,
            "library_state_rls": True,
            "library_state_service_policy": True,
            "push_device_columns": True,
            "push_delivery_columns": False,
            "push_completion_trigger": True,
            "push_logout_function": True,
        },
    )
    with pytest.raises(ValueError, match="push notification schema is missing"):
        asyncio.run(db.probe_postgres())


def _ready_schema_state(**overrides):
    state = {
        "delivery_column": True,
        "delivery_index": True,
        "refresh_replay_columns": True,
        "library_state_columns": True,
        "library_state_history": True,
        "library_state_rls": True,
        "library_state_service_policy": True,
        "push_device_columns": True,
        "push_delivery_columns": True,
        "push_completion_trigger": True,
        "push_logout_function": True,
        "runtime_role_bypasses_rls": False,
        "runtime_db_privileges": True,
        "worker_heartbeat_recent": False,
    }
    state.update(overrides)
    return state


def test_ready_fails_when_database_role_bypasses_rls(monkeypatch):
    monkeypatch.setattr(db.settings, "apple_root_ca_pem", _valid_root_certificate_pem())
    monkeypatch.setattr(db, "fetch_one", lambda _sql: _ready_schema_state(runtime_role_bypasses_rls=True))

    with pytest.raises(ValueError, match="Database role bypasses row-level security"):
        asyncio.run(db.probe_postgres())


def test_ready_fails_when_runtime_role_lacks_database_grants(monkeypatch):
    monkeypatch.setattr(db.settings, "apple_root_ca_pem", _valid_root_certificate_pem())
    monkeypatch.setattr(db, "fetch_one", lambda _sql: _ready_schema_state(runtime_db_privileges=False))

    with pytest.raises(ValueError, match="Runtime database role has incomplete privileges"):
        asyncio.run(db.probe_postgres())


def test_ready_fails_when_worker_is_running_but_api_durable_mode_is_disabled(monkeypatch):
    monkeypatch.setattr(db.settings, "apple_root_ca_pem", _valid_root_certificate_pem())
    monkeypatch.setattr(db, "fetch_one", lambda _sql: _ready_schema_state(worker_heartbeat_recent=True))

    with pytest.raises(ValueError, match="worker is running while durable worker mode is disabled"):
        asyncio.run(db.probe_postgres())


def test_ready_fails_when_apns_is_enabled_without_durable_worker(monkeypatch):
    monkeypatch.setattr(db.settings, "apns_enabled", True)
    monkeypatch.setattr(db.settings, "apple_root_ca_pem", _valid_root_certificate_pem())
    monkeypatch.setattr(db, "fetch_one", lambda _sql: _ready_schema_state())

    with pytest.raises(ValueError, match="APNs requires durable worker mode"):
        asyncio.run(db.probe_postgres())


def test_ready_fails_when_apns_credentials_are_invalid(monkeypatch):
    _configure_valid_apns(monkeypatch)
    monkeypatch.setattr(db.settings, "apns_auth_key", "not-a-private-key")
    monkeypatch.setattr(db.settings, "apple_root_ca_pem", _valid_root_certificate_pem())
    monkeypatch.setattr(db, "fetch_one", lambda _sql: _ready_schema_state(worker_heartbeat_recent=True))

    with pytest.raises(ValueError, match="APNs is enabled but its credentials are invalid"):
        asyncio.run(db.probe_postgres())


def test_ready_accepts_valid_apns_and_escaped_apple_root_pem(monkeypatch):
    _configure_valid_apns(monkeypatch)
    monkeypatch.setattr(
        db.settings,
        "apple_root_ca_pem",
        _valid_root_certificate_pem().replace("\n", "\\n"),
    )
    monkeypatch.setattr(db, "fetch_one", lambda _sql: _ready_schema_state(worker_heartbeat_recent=True))

    asyncio.run(db.probe_postgres())


@pytest.mark.parametrize(
    "root_certificate, message",
    [
        ("", "certificate is missing"),
        ("-----BEGIN CERTIFICATE-----\\ninvalid\\n-----END CERTIFICATE-----", "certificate is invalid"),
    ],
)
def test_ready_fails_when_apple_transaction_root_is_missing_or_invalid(monkeypatch, root_certificate, message):
    monkeypatch.setattr(db.settings, "apple_root_ca_pem", root_certificate)
    monkeypatch.setattr(db, "fetch_one", lambda _sql: _ready_schema_state())

    with pytest.raises(ValueError, match=message):
        asyncio.run(db.probe_postgres())


def test_ready_fails_closed_when_durable_worker_heartbeat_is_stale(monkeypatch):
    monkeypatch.setattr(db.settings, "worker_enabled", True)
    monkeypatch.setattr(db.settings, "apple_root_ca_pem", _valid_root_certificate_pem())
    monkeypatch.setattr(
        db,
        "fetch_one",
        lambda _sql: {
            "delivery_column": True,
            "delivery_index": True,
            "refresh_replay_columns": True,
            "library_state_columns": True,
            "library_state_history": True,
            "library_state_rls": True,
            "library_state_service_policy": True,
            "push_device_columns": True,
            "push_delivery_columns": True,
            "push_completion_trigger": True,
            "push_logout_function": True,
            "runtime_db_privileges": True,
            "worker_heartbeat_recent": False,
        },
    )
    with pytest.raises(ValueError, match="Recipe worker heartbeat is stale"):
        asyncio.run(db.probe_postgres())


def test_ready_fails_closed_when_library_state_schema_is_incomplete(monkeypatch):
    monkeypatch.setattr(
        db,
        "fetch_one",
        lambda _sql: {
            "delivery_column": True,
            "delivery_index": True,
            "refresh_replay_columns": True,
            "library_state_columns": True,
            "library_state_history": True,
            "library_state_rls": True,
            "library_state_service_policy": False,
            "push_device_columns": True,
            "push_delivery_columns": True,
            "push_completion_trigger": True,
            "push_logout_function": True,
        },
    )
    with pytest.raises(ValueError, match="user library state schema is missing"):
        asyncio.run(db.probe_postgres())


def test_ready_fails_closed_when_library_state_history_is_missing(monkeypatch):
    monkeypatch.setattr(
        db,
        "fetch_one",
        lambda _sql: {
            "delivery_column": True,
            "delivery_index": True,
            "refresh_replay_columns": True,
            "library_state_columns": True,
            "library_state_history": False,
            "library_state_rls": True,
            "library_state_service_policy": True,
            "push_device_columns": True,
            "push_delivery_columns": True,
            "push_completion_trigger": True,
            "push_logout_function": True,
        },
    )
    with pytest.raises(ValueError, match="user library state history migration is missing"):
        asyncio.run(db.probe_postgres())


def test_ready_returns_unavailable_and_safe_operator_code_until_required_schema_exists(monkeypatch, caplog):
    @asynccontextmanager
    async def timeout_compat(_seconds):
        yield

    async def missing_schema():
        raise ValueError("Required import idempotency schema is missing")

    monkeypatch.setattr(main.asyncio, "timeout", timeout_compat, raising=False)
    monkeypatch.setattr(main, "probe_postgres", missing_schema)
    response = asyncio.run(main.ready())

    assert response.status_code == 503
    assert response.headers["Retry-After"] == "1"
    assert b'"status":"unavailable"' in response.body
    assert b'"error":"ValueError"' in response.body
    assert b"migration_008_missing" not in response.body
    assert "error_code=migration_008_missing" in caplog.text
    assert "Required import idempotency schema is missing" not in caplog.text


@pytest.mark.parametrize(
    ("error", "code"),
    [
        (ValueError("Apple transaction verification certificate is missing"), "apple_root_certificate_missing"),
        (ValueError("Recipe worker heartbeat is stale"), "worker_heartbeat_stale"),
        (TimeoutError(), "readiness_timeout"),
        (RuntimeError("connection string must never be logged"), "dependency_unavailable"),
    ],
)
def test_readiness_failure_codes_are_safe_and_actionable(error, code):
    assert db.readiness_failure_code(error) == code
