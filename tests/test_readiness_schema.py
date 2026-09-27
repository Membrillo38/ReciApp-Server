import asyncio
from contextlib import asynccontextmanager

import pytest

from app import db, main


def test_ready_requires_delivery_column_and_unique_index(monkeypatch):
    queries = []

    def fetch(_sql):
        queries.append(_sql)
        return {
            "delivery_column": True,
            "delivery_index": True,
            "refresh_replay_columns": True,
            "push_device_columns": True,
            "push_delivery_columns": True,
            "push_completion_trigger": True,
            "push_logout_function": True,
        }

    monkeypatch.setattr(
        db,
        "fetch_one",
        fetch,
    )
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
            "push_device_columns": True,
            "push_delivery_columns": False,
            "push_completion_trigger": True,
            "push_logout_function": True,
        },
    )
    with pytest.raises(ValueError, match="push notification schema is missing"):
        asyncio.run(db.probe_postgres())


def test_ready_fails_closed_when_durable_worker_heartbeat_is_stale(monkeypatch):
    monkeypatch.setattr(db.settings, "worker_enabled", True)
    monkeypatch.setattr(
        db,
        "fetch_one",
        lambda _sql: {
            "delivery_column": True,
            "delivery_index": True,
            "refresh_replay_columns": True,
            "push_device_columns": True,
            "push_delivery_columns": True,
            "push_completion_trigger": True,
            "push_logout_function": True,
            "worker_heartbeat_recent": False,
        },
    )
    with pytest.raises(ValueError, match="Recipe worker heartbeat is stale"):
        asyncio.run(db.probe_postgres())


def test_ready_returns_unavailable_until_required_schema_exists(monkeypatch):
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
