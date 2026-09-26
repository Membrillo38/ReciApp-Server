from uuid import uuid4
from types import SimpleNamespace

from app import auth_tokens, main


def test_refresh_rotation_revokes_and_issues_in_one_statement(monkeypatch):
    user_id = uuid4()
    calls = []

    def execute_returning(sql, params):
        calls.append((sql, params))
        return {"user_id": user_id, "email": "person@example.com"}

    monkeypatch.setattr(auth_tokens, "execute_returning", execute_returning)
    monkeypatch.setattr(auth_tokens, "create_access_token", lambda received_id, email: "access")
    monkeypatch.setattr(auth_tokens.settings, "auth_refresh_token_ttl_seconds", 3600)

    result = auth_tokens.rotate_refresh_token("old-refresh")

    assert result is not None
    assert result["access_token"] == "access"
    assert result["refresh_token"] != "old-refresh"
    assert len(calls) == 1
    sql, params = calls[0]
    assert "with source as" in sql.lower()
    assert "revoked as" in sql.lower()
    assert "issued as" in sql.lower()
    assert params[0] == auth_tokens._hash("old-refresh")
    assert params[1] is None
    assert params[5] == auth_tokens._hash(result["refresh_token"])


def test_refresh_rotation_returns_none_for_missing_or_expired_token(monkeypatch):
    monkeypatch.setattr(auth_tokens, "execute_returning", lambda *_: None)
    assert auth_tokens.rotate_refresh_token("invalid") is None


def test_refresh_retry_with_same_request_id_replays_same_successor(monkeypatch):
    user_id = uuid4()
    request_id = uuid4()
    issued = {"user_id": user_id, "email": "person@example.com"}
    writes = iter((issued, None))
    replay_queries = []
    monkeypatch.setattr(auth_tokens.settings, "auth_jwt_secret", "test-secret-long-enough-for-hmac")
    monkeypatch.setattr(auth_tokens.settings, "auth_refresh_token_ttl_seconds", 3600)
    monkeypatch.setattr(auth_tokens, "execute_returning", lambda *_: next(writes))
    monkeypatch.setattr(auth_tokens, "fetch_one", lambda sql, params: replay_queries.append((sql, params)) or issued)
    monkeypatch.setattr(auth_tokens, "create_access_token", lambda *_: "access")

    first = auth_tokens.rotate_refresh_token("old-refresh", request_id)
    retry = auth_tokens.rotate_refresh_token("old-refresh", request_id)

    assert first is not None and retry is not None
    assert first["refresh_token"] == retry["refresh_token"]
    assert first["refresh_token"] != "old-refresh"
    assert len(replay_queries) == 1
    sql, params = replay_queries[0]
    assert "rotation_request_id" in sql
    assert "rotation_retry_until > now()" in sql
    assert "join auth_refresh_tokens successor" in sql
    assert "successor.revoked_at is null" in sql
    assert "successor.expires_at > now()" in sql
    assert "p.deleted_at is null" in sql
    assert params == (auth_tokens._hash("old-refresh"), request_id)


def test_refresh_retry_with_different_request_id_is_rejected(monkeypatch):
    monkeypatch.setattr(auth_tokens.settings, "auth_jwt_secret", "test-secret-long-enough-for-hmac")
    monkeypatch.setattr(auth_tokens, "execute_returning", lambda *_: None)
    monkeypatch.setattr(auth_tokens, "fetch_one", lambda *_: None)

    assert auth_tokens.rotate_refresh_token("old-refresh", uuid4()) is None


def test_refresh_rotation_stores_only_hash_and_bounded_replay_state(monkeypatch):
    user_id = uuid4()
    request_id = uuid4()
    captured = {}
    monkeypatch.setattr(auth_tokens.settings, "auth_jwt_secret", "test-secret-long-enough-for-hmac")
    monkeypatch.setattr(auth_tokens.settings, "auth_refresh_token_ttl_seconds", 3600)
    monkeypatch.setattr(
        auth_tokens,
        "execute_returning",
        lambda sql, params: captured.update(sql=sql, params=params) or {"user_id": user_id, "email": None},
    )
    monkeypatch.setattr(auth_tokens, "create_access_token", lambda *_: "access")

    result = auth_tokens.rotate_refresh_token("old-refresh", request_id)

    assert result is not None
    sql = captured["sql"].lower()
    params = captured["params"]
    assert "rotation_request_id" in sql
    assert "rotated_token_hash" in sql
    assert "rotation_retry_until" in sql
    assert "900" in str(params)
    assert result["refresh_token"] not in str(params)
    assert auth_tokens._hash(result["refresh_token"]) in params


def test_logout_with_pending_request_revokes_rotated_successor(monkeypatch):
    captured = {}
    request_id = uuid4()
    monkeypatch.setattr(
        auth_tokens,
        "execute",
        lambda sql, params: captured.update(sql=" ".join(sql.split()).lower(), params=params),
    )

    auth_tokens.revoke_refresh_token("old-refresh", request_id)

    assert "rotated_token_hash" in captured["sql"]
    assert "rotation_request_id" in captured["sql"]
    assert captured["params"] == (
        auth_tokens._hash("old-refresh"),
        request_id,
        auth_tokens._hash("old-refresh"),
    )


def test_profile_preserves_legacy_quota_field_and_returns_yearly_field(monkeypatch):
    user_id = uuid4()
    monkeypatch.setattr(
        main,
        "get_quota",
        lambda _: SimpleNamespace(
            free_used_this_week=2,
            free_limit=3,
            free_remaining=1,
            pro_remaining_cents=0,
        ),
    )

    result = main.me(SimpleNamespace(
        id=user_id,
        display_name="Test",
        is_pro=False,
        pro_expires_at=None,
    ))

    assert result.free_used_this_week == 2
    assert result.free_used_this_year == 2
