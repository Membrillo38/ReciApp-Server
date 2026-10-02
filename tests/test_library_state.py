from datetime import datetime, timezone
from contextlib import contextmanager
from uuid import uuid4

import pytest
from pydantic import ValidationError
from fastapi import HTTPException

from app import library_state, main
from app.auth import AuthUser
from app.models import UserLibraryStateResponse, UserLibraryStateUpdateRequest


def test_missing_user_state_is_an_empty_versioned_snapshot(monkeypatch):
    monkeypatch.setattr(library_state, "fetch_one", lambda *_: None)

    result = library_state.get_user_library_state(uuid4())

    assert result == {
        "revision": 0,
        "snapshot": {
            "schema_version": 1,
            "folders": None,
            "shopping_list": [],
            "preferences": None,
            "cooking_progress": {},
        },
        "history": [],
        "updated_at": None,
    }
    result["snapshot"]["shopping_list"].append({"id": "test"})

    next_result = library_state.get_user_library_state(uuid4())
    assert next_result["snapshot"]["shopping_list"] == []


def test_library_state_response_normalizes_legacy_empty_snapshot_and_history():
    response = UserLibraryStateResponse(
        revision=0,
        snapshot={"schema_version": 1},
        history=[{"revision": 1, "snapshot": {"schema_version": 1}}],
    )

    assert len(response.history) == 1
    assert response.snapshot == {
        "schema_version": 1,
        "folders": None,
        "shopping_list": [],
        "preferences": None,
        "cooking_progress": {},
    }
    assert response.history[0].revision == 1
    assert response.history[0].snapshot == response.snapshot
    assert response.history[0].updated_at is None

    legacy_empty = UserLibraryStateResponse(revision=1, snapshot={})
    assert legacy_empty.snapshot == response.snapshot


def test_invalid_history_entries_do_not_break_current_library_snapshot():
    response = UserLibraryStateResponse(
        revision=4,
        snapshot={"schema_version": 1},
        history=[
            {"revision": 3, "snapshot": {"schema_version": 1}},
            {"snapshot": {"schema_version": 1}},
            {"revision": 2, "snapshot": {"schema_version": 99}},
            "invalid",
        ],
    )

    assert response.snapshot["schema_version"] == 1
    assert [entry.revision for entry in response.history] == [3]


def test_snapshot_update_fills_optional_state_fields_for_older_clients():
    request = UserLibraryStateUpdateRequest(revision=0, snapshot={"schema_version": 1})

    assert request.snapshot == {
        "schema_version": 1,
        "folders": None,
        "shopping_list": [],
        "preferences": None,
        "cooking_progress": {},
    }


def test_first_snapshot_insert_is_revision_one(monkeypatch):
    user_id = uuid4()
    snapshot = {"schema_version": 1, "favorites": []}
    recorded = {}

    def insert(sql, params):
        recorded.update(sql=sql, params=params)
        return {"revision": 1, "snapshot": snapshot, "updated_at": datetime.now(timezone.utc)}

    monkeypatch.setattr(library_state, "execute_returning", insert)

    result = library_state.save_user_library_state(user_id, expected_revision=0, snapshot=snapshot)

    assert result["revision"] == 1
    assert recorded["params"] == (user_id, snapshot)
    assert "from public.profiles" in recorded["sql"]
    assert "deleted_at is null" in recorded["sql"]
    assert "for update" in recorded["sql"]
    assert "on conflict (user_id) do nothing" in recorded["sql"]


def test_snapshot_update_requires_current_revision(monkeypatch):
    user_id = uuid4()
    snapshot = {"schema_version": 1, "favorites": [str(uuid4())]}
    recorded = {}

    def update(sql, params):
        recorded.update(sql=sql, params=params)
        return {
            "revision": 8,
            "snapshot": snapshot,
            "history": [{"revision": 7, "snapshot": {"schema_version": 1}}],
            "updated_at": datetime.now(timezone.utc),
        }

    monkeypatch.setattr(library_state, "execute_returning", update)

    result = library_state.save_user_library_state(user_id, expected_revision=7, snapshot=snapshot)

    assert result["revision"] == 8
    assert recorded["params"] == (user_id, snapshot, 7)
    assert "from public.profiles" in recorded["sql"]
    assert "deleted_at is null" in recorded["sql"]
    assert "for update" in recorded["sql"]
    assert "state.user_id = active_profile.id and state.revision = %s" in recorded["sql"]
    assert "jsonb_array_elements" in recorded["sql"]
    assert "'revision', state.revision" in recorded["sql"]
    assert result["history"][-1]["revision"] == 7


def test_library_snapshot_rejects_unknown_version_and_oversize():
    with pytest.raises(ValidationError):
        UserLibraryStateUpdateRequest(revision=0, snapshot={"schema_version": 2})
    with pytest.raises(ValidationError):
        UserLibraryStateUpdateRequest(revision=0, snapshot={"schema_version": 1, "blob": "x" * 262_145})


def test_library_state_routes_use_service_context_for_authenticated_account(monkeypatch):
    user = AuthUser(id=uuid4(), email=None, display_name=None, is_pro=False, pro_expires_at=None)
    seen = []

    @contextmanager
    def context(*, actor):
        seen.append(actor)
        yield

    monkeypatch.setattr(main, "db_context", context)
    monkeypatch.setattr(
        main,
        "get_user_library_state",
        lambda user_id: {"revision": 3, "snapshot": {"schema_version": 1}, "updated_at": None},
    )

    response = main.get_library_state(user)

    assert response.revision == 3
    assert seen == ["service"]


def test_library_state_route_returns_conflict_instead_of_overwriting(monkeypatch):
    user = AuthUser(id=uuid4(), email=None, display_name=None, is_pro=False, pro_expires_at=None)

    @contextmanager
    def context(*, actor):
        yield

    monkeypatch.setattr(main, "db_context", context)
    monkeypatch.setattr(main, "save_user_library_state", lambda *_args, **_kwargs: None)

    with pytest.raises(HTTPException) as error:
        main.put_library_state(
            UserLibraryStateUpdateRequest(revision=2, snapshot={"schema_version": 1}),
            user,
        )

    assert error.value.status_code == 409
    assert error.value.detail["code"] == "LIBRARY_STATE_CONFLICT"
