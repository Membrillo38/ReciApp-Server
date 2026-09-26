from contextlib import contextmanager
from uuid import uuid4

import pytest
from fastapi import HTTPException

from app import store
from app.auth import ACCOUNT_DELETED, ACCOUNT_UNAVAILABLE, AuthUser, current_user, current_user_allow_closed
from app.main import admin_delete_user, delete_me, revoke_stored_apple_authorization
import app.main as main


def test_account_data_deletion_is_atomic_and_keeps_usage_history(monkeypatch):
    sqls = []
    exits = []

    class Cursor:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def execute(self, sql, params=None):
            sqls.append((" ".join(sql.split()), params))

    class Connection:
        def cursor(self):
            return Cursor()

    @contextmanager
    def fake_get_conn():
        try:
            yield Connection()
        except Exception as exc:
            exits.append(exc)
            raise

    monkeypatch.setattr(store, "get_conn", fake_get_conn)
    store.delete_account_data(uuid4())
    normalized = [sql for sql, _params in sqls]
    assert any(sql.startswith("delete from user_recipes") for sql in normalized)
    assert any(sql.startswith("update extract_jobs") for sql in normalized)
    assert any(sql.startswith("delete from extract_job_access") for sql in normalized)
    assert any(sql.startswith("update auth_refresh_tokens") for sql in normalized)
    assert any(sql.startswith("update profiles") for sql in normalized)
    assert not any("usage_events" in sql for sql in normalized)
    assert len(exits) == 0


def test_account_data_deletion_propagates_database_failure(monkeypatch):
    class Cursor:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def execute(self, *_args):
            raise RuntimeError("database unavailable")

    class Connection:
        def cursor(self):
            return Cursor()

    @contextmanager
    def fake_get_conn():
        yield Connection()

    monkeypatch.setattr(store, "get_conn", fake_get_conn)
    with pytest.raises(RuntimeError, match="database unavailable"):
        store.delete_account_data(uuid4())


def test_recipe_completion_cannot_reattach_to_deleted_profile(monkeypatch):
    captured = {}
    monkeypatch.setattr(
        store,
        "execute",
        lambda sql, params=None: captured.update(sql=" ".join(sql.split()), params=params),
    )
    user_id, recipe_id = uuid4(), uuid4()
    store.save_user_recipe(user_id, recipe_id)
    assert "select id, %s from profiles where id = %s and deleted_at is null" in captured["sql"]
    assert captured["params"] == (recipe_id, user_id)


def test_delete_me_revokes_apple_then_runs_atomic_database_cleanup():
    source = open("app/main.py", encoding="utf-8").read()
    purge = source.split("def _purge_account", 1)[1].split("def auth_apple", 1)[0]
    assert purge.index("revoke_stored_apple_authorization") < purge.index("delete_account_data")
    delete_body = source.split("def delete_me", 1)[1].split("def extract_recipe", 1)[0]
    assert "current_user_allow_closed" in delete_body
    assert "_purge_account" in source.split("def admin_delete_user", 1)[1].split("def admin_list_recipes", 1)[0]
    assert callable(delete_me) and callable(admin_delete_user) and callable(store.delete_account_data)


def test_auth_apple_reactivates_closed_profile_for_same_apple_sub():
    source = open("app/main.py", encoding="utf-8").read()
    upsert = source.split("def _upsert_apple_profile", 1)[1].split("def _persist_apple_authorization_code", 1)[0]
    body = source.split("def auth_apple", 1)[1].split("def auth_refresh", 1)[0]
    assert "insert into profiles" in upsert
    assert "deleted_at = null" in upsert
    assert "where profiles.deleted_at is null" not in upsert
    assert "release_deleted_apple_identity" not in body
    assert "authorization_code" in body


def test_delete_me_is_idempotent(monkeypatch):
    calls = []
    monkeypatch.setattr(main, "revoke_stored_apple_authorization", lambda uid: calls.append("apple"))
    monkeypatch.setattr(main, "delete_account_data", lambda uid: calls.append("db"))
    user = AuthUser(id=uuid4(), email=None, display_name=None, is_pro=False, pro_expires_at=None)
    assert delete_me(user).ok is True
    assert delete_me(user).ok is True
    assert calls == ["apple", "db", "apple", "db"]


def test_account_deletion_database_failure_is_retryable(monkeypatch):
    monkeypatch.setattr(main, "revoke_stored_apple_authorization", lambda _uid: None)
    monkeypatch.setattr(main, "delete_account_data", lambda _uid: (_ for _ in ()).throw(RuntimeError("db")))
    user = AuthUser(id=uuid4(), email=None, display_name=None, is_pro=False, pro_expires_at=None)
    with pytest.raises(HTTPException) as caught:
        delete_me(user)
    assert caught.value.status_code == 503
    assert caught.value.headers["Retry-After"] == "3"
    assert caught.value.detail["code"] == "ACCOUNT_DELETION_UNAVAILABLE"


def test_revoke_stored_apple_token_deletes_only_after_success(monkeypatch):
    user_id = uuid4()
    deleted = []
    monkeypatch.setattr(main, "get_apple_refresh_token_ciphertext", lambda uid: "cipher")
    monkeypatch.setattr(main, "decrypt_apple_refresh_token", lambda blob: "apple-refresh-secret")
    monkeypatch.setattr(main, "revoke_apple_refresh_token", lambda token: True)
    monkeypatch.setattr(main, "delete_apple_refresh_token", lambda uid: deleted.append(uid))
    revoke_stored_apple_authorization(user_id)
    assert deleted == [user_id]

    deleted.clear()
    monkeypatch.setattr(main, "revoke_apple_refresh_token", lambda token: False)
    revoke_stored_apple_authorization(user_id)
    assert deleted == []


def test_auth_apple_persists_encrypted_refresh_token(monkeypatch):
    captured = {}
    monkeypatch.setattr(main, "exchange_apple_authorization_code", lambda code, expected_sub: "apple-refresh-secret")
    monkeypatch.setattr(main, "encrypt_apple_refresh_token", lambda raw: "cipher-not-plaintext")
    monkeypatch.setattr(main, "upsert_apple_refresh_token", lambda uid, ciphertext: captured.update({"uid": uid, "ciphertext": ciphertext}))
    user_id = uuid4()
    main._persist_apple_authorization_code(user_id, "auth-code-secret", "apple-sub")
    assert captured["uid"] == user_id
    assert captured["ciphertext"] == "cipher-not-plaintext"
    assert captured["ciphertext"] != "apple-refresh-secret"
    captured.clear()
    main._persist_apple_authorization_code(user_id, None, "apple-sub")
    assert captured == {}


def test_current_user_returns_stable_account_codes(monkeypatch):
    import app.auth as authmod

    user_id = uuid4()
    monkeypatch.setattr(authmod, "_bearer_claims", lambda creds: (user_id, None))
    monkeypatch.setattr(authmod, "_load_profile", lambda uid: None)
    with pytest.raises(HTTPException) as missing:
        current_user(creds=object())
    assert missing.value.status_code == 403
    assert missing.value.detail == {"code": ACCOUNT_UNAVAILABLE, "message": "Account unavailable"}

    monkeypatch.setattr(
        authmod,
        "_load_profile",
        lambda uid: {
            "deleted_at": "2026-01-01",
            "email": None,
            "display_name": None,
            "is_pro": False,
            "pro_expires_at": None,
        },
    )
    with pytest.raises(HTTPException) as deleted:
        current_user(creds=object())
    assert deleted.value.detail == {"code": ACCOUNT_DELETED, "message": "Account deleted"}

    closed = current_user_allow_closed(creds=object())
    assert closed.id == user_id
    assert closed.display_name is None
