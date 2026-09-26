"""Serial extract queue: enqueue when busy, expose /v1/me/jobs, Retry-After on 429."""
from uuid import uuid4

import pytest
from fastapi import BackgroundTasks, HTTPException
from starlette.requests import Request

import app.main as main
import app.security as security
from app.auth import AuthUser
from app.job_guard import release, try_claim
from app.models import JobStatus


def _request() -> Request:
    return Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/v1/extract",
            "headers": [],
            "client": ("127.0.0.1", 1234),
        }
    )


def test_extract_queues_when_user_already_has_open_job(monkeypatch):
    user = AuthUser(uuid4(), None, None, True, None)
    created = []
    background = BackgroundTasks()
    monkeypatch.setattr(main.settings, "worker_enabled", False)
    monkeypatch.setattr(main.settings, "openai_api_key", "test")
    monkeypatch.setattr(main.settings, "max_pending_jobs_per_user", 20)
    for name in ("require_rate_limit", "validate_public_url", "assert_can_extract"):
        monkeypatch.setattr(main, name, lambda *a, **kw: None)
    monkeypatch.setattr(main, "get_recipe_by_norm", lambda *a: None)
    monkeypatch.setattr(main, "get_active_job", lambda **kw: None)
    monkeypatch.setattr(main, "count_user_open_extract_jobs", lambda uid: 1)
    monkeypatch.setattr(main, "try_claim_job", lambda uid: (_ for _ in ()).throw(AssertionError("must not claim")))
    monkeypatch.setattr(main, "reserve_spend", lambda **kw: None)

    def create_job(**kw):
        row = {"id": str(uuid4()), **kw}
        created.append(row)
        return row

    monkeypatch.setattr(main, "create_job", create_job)

    response = main.extract_recipe(
        _request(),
        main.ExtractRequest(url="https://youtu.be/queued"),
        background,
        user,
    )
    assert response.queued is True
    assert response.status == JobStatus.pending
    assert response.queue_position == 2
    assert background.tasks == []
    assert len(created) == 1


def test_extract_starts_when_queue_empty(monkeypatch):
    user = AuthUser(uuid4(), None, None, True, None)
    claims = []
    background = BackgroundTasks()
    monkeypatch.setattr(main.settings, "worker_enabled", False)
    monkeypatch.setattr(main.settings, "openai_api_key", "test")
    for name in ("require_rate_limit", "validate_public_url", "assert_can_extract"):
        monkeypatch.setattr(main, name, lambda *a, **kw: None)
    monkeypatch.setattr(main, "get_recipe_by_norm", lambda *a: None)
    monkeypatch.setattr(main, "get_active_job", lambda **kw: None)
    monkeypatch.setattr(main, "count_user_open_extract_jobs", lambda uid: 0)
    monkeypatch.setattr(main, "try_claim_job", lambda uid: claims.append(uid) or True)
    monkeypatch.setattr(main, "create_job", lambda **kw: {"id": str(uuid4())})
    monkeypatch.setattr(main, "reserve_spend", lambda **kw: None)
    monkeypatch.setattr(main, "run_extract_job", lambda *a, **kw: None)

    response = main.extract_recipe(
        _request(),
        main.ExtractRequest(url="https://youtu.be/start"),
        background,
        user,
    )
    assert response.queued is False
    assert claims == [user.id]
    assert len(background.tasks) == 1


def test_extract_rejects_when_user_queue_full(monkeypatch):
    user = AuthUser(uuid4(), None, None, True, None)
    monkeypatch.setattr(main.settings, "worker_enabled", False)
    monkeypatch.setattr(main.settings, "openai_api_key", "test")
    monkeypatch.setattr(main.settings, "max_pending_jobs_per_user", 20)
    for name in ("require_rate_limit", "validate_public_url", "assert_can_extract"):
        monkeypatch.setattr(main, name, lambda *a, **kw: None)
    monkeypatch.setattr(main, "get_recipe_by_norm", lambda *a: None)
    monkeypatch.setattr(main, "get_active_job", lambda **kw: None)
    monkeypatch.setattr(main, "count_user_open_extract_jobs", lambda uid: 20)

    with pytest.raises(HTTPException) as caught:
        main.extract_recipe(
            _request(),
            main.ExtractRequest(url="https://youtu.be/full"),
            BackgroundTasks(),
            user,
        )
    assert caught.value.status_code == 429
    assert caught.value.headers.get("Retry-After") == "30"


def test_list_my_jobs_orders_and_positions(monkeypatch):
    user = AuthUser(uuid4(), None, None, True, None)
    first = uuid4()
    second = uuid4()
    monkeypatch.setattr(
        main,
        "list_user_open_extract_jobs",
        lambda uid, limit=20: [
            {
                "id": str(first),
                "status": "processing",
                "progress": 40,
                "source_url_raw": "https://youtu.be/a",
                "created_at": "2026-01-01T00:00:00Z",
                "job_kind": "extract",
            },
            {
                "id": str(second),
                "status": "pending",
                "progress": 0,
                "source_url_raw": "https://youtu.be/b",
                "created_at": "2026-01-01T00:01:00Z",
                "job_kind": "extract",
            },
        ],
    )
    response = main.list_my_jobs(user)
    assert [item.queue_position for item in response.items] == [1, 2]
    assert response.items[0].job_id == first
    assert response.items[1].status == JobStatus.pending


def test_rate_limit_includes_retry_after(monkeypatch):
    security._rate_limiters.clear()
    request = _request()
    security.require_rate_limit(
        request, key="test-rl", limit=1, window_seconds=60, event="test"
    )
    with pytest.raises(HTTPException) as caught:
        security.require_rate_limit(
            request, key="test-rl", limit=1, window_seconds=60, event="test"
        )
    assert caught.value.status_code == 429
    assert caught.value.headers.get("Retry-After") == "60"


def test_user_rate_limit_audit_includes_user_and_safe_context(monkeypatch):
    security._rate_limiters.clear()
    request = _request()
    request.state.correlation_id = "corr-test"
    events = []
    monkeypatch.setattr(security, "audit_security_event", lambda **kwargs: events.append(kwargs))
    security.require_rate_limit(
        request,
        key="extract-user:test-user",
        limit=1,
        window_seconds=60,
        event="extract_user_rate_limited",
    )

    with pytest.raises(HTTPException):
        security.require_rate_limit(
            request,
            key="extract-user:test-user",
            limit=1,
            window_seconds=60,
            event="extract_user_rate_limited",
            audit_user_id="test-user",
        )

    assert len(events) == 1
    assert events[0]["user_id"] == "test-user"
    assert events[0]["metadata"] == {
        "limit": 1,
        "window_seconds": 60,
        "correlation_id": "corr-test",
    }


def test_rate_limiters_do_not_clobber_each_other():
    security._rate_limiters.clear()
    assert security.allow_rate_limit("a", limit=2, window_seconds=60)
    assert security.allow_rate_limit("b", limit=1, window_seconds=60)
    assert security.allow_rate_limit("a", limit=2, window_seconds=60)
    assert not security.allow_rate_limit("b", limit=1, window_seconds=60)


def test_try_claim_respects_capacity(monkeypatch):
    import app.job_guard as jg
    from app.config import settings

    monkeypatch.setattr(settings, "max_concurrent_jobs", 1)
    monkeypatch.setattr(settings, "max_concurrent_jobs_per_user", 2)
    jg._global_active = 0
    jg._user_active.clear()
    uid = uuid4()
    assert try_claim(uid) is True
    assert try_claim(uuid4()) is False
    release(uid)
    assert try_claim(uuid4()) is True
    release(uuid4())  # no-op safe if different id — reset below
    jg._global_active = 0
    jg._user_active.clear()


def test_failed_admission_releases_try_claim(monkeypatch):
    user = AuthUser(uuid4(), None, None, False, None)
    claims, releases = [], []
    monkeypatch.setattr(main.settings, "worker_enabled", False)
    monkeypatch.setattr(main.settings, "openai_api_key", "test")
    monkeypatch.setattr(main, "try_claim_job", lambda uid: claims.append(uid) or True)
    monkeypatch.setattr(main, "release_job", lambda uid: releases.append(uid))
    monkeypatch.setattr(main, "count_user_open_extract_jobs", lambda uid: 0)
    for name in ("require_rate_limit", "validate_public_url", "assert_can_extract"):
        monkeypatch.setattr(main, name, lambda *a, **kw: None)
    monkeypatch.setattr(main, "get_recipe_by_norm", lambda *a: None)
    monkeypatch.setattr(main, "get_active_job", lambda **kw: None)
    monkeypatch.setattr(main, "create_job", lambda **kw: {"id": str(uuid4())})
    monkeypatch.setattr(main, "reserve_spend", lambda **kw: (_ for _ in ()).throw(RuntimeError("database unavailable")))
    monkeypatch.setattr(main, "update_job", lambda *a, **kw: None)
    with pytest.raises(RuntimeError, match="database unavailable"):
        main.extract_recipe(
            _request(),
            main.ExtractRequest(url="https://youtu.be/fail"),
            BackgroundTasks(),
            user,
        )
    assert claims == [user.id]
    assert releases == [user.id]
