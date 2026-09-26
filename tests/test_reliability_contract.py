import pytest
import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import UUID, uuid4

import httpx
from fastapi import BackgroundTasks, HTTPException
from starlette.requests import Request

import app.main as main
from app.auth import AuthUser
from app.main import _job_is_stale
from app.models import JobStatus
from app.store import recipe_public_from_row


def test_limit_resolution_skips_remote_defaults_when_profile_is_complete():
    import app.limits as limits

    original = limits.get_app_defaults
    limits.get_app_defaults = lambda: (_ for _ in ()).throw(
        AssertionError("complete profile must not read remote defaults")
    )
    try:
        resolved = limits.resolve_user_limits(
            {
                "free_weekly_limit": 1,
                "pro_monthly_price_cents": 999,
                "pro_margin_ratio": 0.20,
            }
        )
    finally:
        limits.get_app_defaults = original

    assert resolved.pro_budget_cents == 799.2


def test_pending_job_expires_only_after_ttl():
    fresh = {
        "status": JobStatus.pending.value,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    old = {
        "status": JobStatus.processing.value,
        "updated_at": (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat(),
    }
    assert _job_is_stale(fresh) is False
    assert _job_is_stale(old) is True


def test_completed_job_never_expires():
    row = {
        "status": JobStatus.completed.value,
        "updated_at": (datetime.now(timezone.utc) - timedelta(days=4)).isoformat(),
    }
    assert _job_is_stale(row) is False


def test_carousel_contract_keeps_source_order_without_reordering_thumbnail():
    recipe = recipe_public_from_row(
        {
            "id": str(uuid4()),
            "title": "Carousel",
            "ingredients": [],
            "steps": [],
            "platform": "tiktok",
            "source_url_raw": "https://www.tiktok.com/@cook/video/1",
            "thumbnail_url": "https://cdn.example/cover.jpg",
            "carousel_image_urls": [
                "https://cdn.example/slide-1.jpg",
                "https://cdn.example/slide-1.jpg",
                "https://cdn.example/slide-2.jpg",
            ],
        }
    )
    assert recipe.carousel_image_urls == [
        "https://cdn.example/slide-1.jpg",
        "https://cdn.example/slide-2.jpg",
    ]


def test_legacy_recipe_keeps_thumbnail_as_separate_fallback():
    recipe = recipe_public_from_row(
        {
            "id": str(uuid4()),
            "title": "Legacy",
            "ingredients": [],
            "steps": [],
            "platform": "tiktok",
            "source_url_raw": "https://www.tiktok.com/@cook/video/1",
            "thumbnail_url": "https://cdn.example/cover.jpg",
        }
    )
    assert recipe.carousel_image_urls == []
    assert recipe.thumbnail_url == "https://cdn.example/cover.jpg"


def test_profile_upstream_disconnect_is_a_retryable_client_error():
    request = Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/v1/me",
            "headers": [],
            "query_string": b"",
            "scheme": "https",
            "client": ("127.0.0.1", 1234),
            "server": ("localhost", 8000),
        }
    )
    reset_calls = 0
    original_reset = main.reset_db

    def record_reset():
        nonlocal reset_calls
        reset_calls += 1

    main.reset_db = record_reset
    try:
        response = asyncio.run(main.upstream_request_error(request, httpx.RequestError("disconnect")))
    finally:
        main.reset_db = original_reset

    assert reset_calls == 0
    assert response.status_code == 503
    assert response.headers["retry-after"] == "1"
    assert b'"code":"AUTH_UPSTREAM_ERROR"' in response.body
    assert b'"detail":{"code":"AUTH_UPSTREAM_ERROR","message":"Authentication temporarily unavailable"}' in response.body


def test_job_polling_sends_language_and_client_handles_handoff(ios_root: Path):
    source = (ios_root / "ReciApp/Services/APIClient.swift").read_text(encoding="utf-8")
    models = (ios_root / "ReciApp/Models/Models.swift").read_text(encoding="utf-8")
    policy = (ios_root / "ReciApp/Services/ClientStatePolicy.swift").read_text(encoding="utf-8")
    assert "func job(id: UUID, language: String)" in source
    assert 'URLQueryItem(name: "language", value: language)' in source
    assert "func myJobs()" in source
    assert "JobFollowPolicy.persistedID" in source
    assert "let recipeId: UUID?" in models
    assert "let nextJobId: UUID?" in models
    assert "QueuedJobsResponse" in models
    assert "enum JobFollowPolicy" in policy
    assert "func waitForRecipe(" in source


def test_swift_models_match_server_retry_and_idempotency_fields(ios_root: Path):
    client_models = (ios_root / "ReciApp/Models/Models.swift").read_text(encoding="utf-8")
    server_models = Path("app/models.py").read_text(encoding="utf-8")
    assert "let freeUsedThisYear: Int?" in client_models
    assert "free_used_this_year: int | None = None" in server_models
    assert "let clientDeliveryId: UUID?" in client_models
    assert "client_delivery_id: UUID | None = None" in server_models
    assert "let errorCode: String?" in client_models
    assert "error_code: str | None = None" in server_models


def test_share_delivery_is_acknowledged_only_after_job_is_persisted(ios_root: Path):
    source = (ios_root / "ReciApp/ViewModels/AppViewModel.swift").read_text(encoding="utf-8")
    submit = source.split("private func submitShareInbox(language:", 1)[1].split(
        "private func submitShareInboxAndFollow", 1
    )[0]
    accepted = submit.index("let started = try await api.extract")
    persisted = submit.index("ImportJobStore.save(", accepted)
    acknowledged = submit.index("markShareDeliveryProcessed(delivery.id", accepted)
    assert persisted < acknowledged


@pytest.mark.skip(reason="Replaced by runnable ClientStateHarness behavioral tests")
def test_ios_recipe_refresh_is_cached_coalesced_and_not_blocked_by_profile():
    view_model = Path("IosAPP/ReciApp/ViewModels/AppViewModel.swift").read_text(encoding="utf-8")
    home = Path("IosAPP/ReciApp/Views/HomeView.swift").read_text(encoding="utf-8")
    api = Path("IosAPP/ReciApp/Services/APIClient.swift").read_text(encoding="utf-8")

    refresh_body = view_model.split("func refreshAll() async {", 1)[1].split("/// Accept raw", 1)[0]
    assert "guard !refreshInFlight" in refresh_body
    assert refresh_body.index("fetchRecipeSummariesWithAuthRetry") < refresh_body.index("fetchMeWithAuthRetry")
    assert "if let recipeFailure, recipes.isEmpty" in refresh_body
    assert 'recipeCacheKeyPrefix = "reciapp.recipeCache.v2"' in view_model
    assert "recipe detail cache hit source=disk" in view_model
    assert "app.groupedRecipesCache" in home
    assert "app.categoryFoldersCache" in home
    assert 'request.setValue(requestID, forHTTPHeaderField: "X-Request-ID")' in api
    assert 'http.value(forHTTPHeaderField: "X-Correlation-ID")' in api


def test_ios_polling_covers_backend_media_timeout_with_margin(ios_root: Path):
    source = (ios_root / "ReciApp/Config/AppConfig.swift").read_text(encoding="utf-8")
    assert "static let maxPollAttempts = 330" in source


def test_ios_warms_backend_before_authenticated_requests(ios_root: Path):
    client = (ios_root / "ReciApp/Services/APIClient.swift").read_text(encoding="utf-8")
    view_model = (ios_root / "ReciApp/ViewModels/AppViewModel.swift").read_text(encoding="utf-8")
    app = (ios_root / "ReciApp/ReciAppApp.swift").read_text(encoding="utf-8")
    assert 'appending(path: "health")' in client or 'appendingPathComponent("/health")' in client
    assert "warmUpBackend()" in client
    assert "func warmUpBackend() async" in view_model
    assert "await app.warmUpBackend()" in app


@pytest.mark.skip(reason="Replaced by runnable ClientStateHarness behavioral tests")
def test_refresh_coalesces_responses_and_subscription_poll_does_not_reload_recipes():
    view_model = Path("IosAPP/ReciApp/ViewModels/AppViewModel.swift").read_text(encoding="utf-8")
    app = Path("IosAPP/ReciApp/ReciAppApp.swift").read_text(encoding="utf-8")
    assert "private var refreshInFlight = false" in view_model
    assert "private var subscriptionRefreshGeneration = 0" in view_model
    assert "guard !refreshInFlight" in view_model
    assert "generation == subscriptionRefreshGeneration" in view_model
    assert "fetchRecipeSummariesWithAuthRetry" in view_model
    assert "await app.refreshAll()" in app
    assert "Task { await app.refreshSubscriptionState() }" not in app


def test_job_status_reuses_initial_row_for_access_check():
    source = Path("app/main.py").read_text(encoding="utf-8")
    store = Path("app/store.py").read_text(encoding="utf-8")
    assert "user_can_access_job(job_id=job_id, user_id=user.id, row=row)" in source
    assert "row: dict | None = None" in store


def test_completed_job_returns_retryable_error_when_attachment_write_fails():
    job_id = uuid4()
    recipe_id = uuid4()
    user = AuthUser(uuid4(), None, None, False, None)
    row = {
        "id": str(job_id),
        "user_id": str(user.id),
        "status": JobStatus.completed.value,
        "progress": 100,
        "cache_hit": False,
        "recipe_id": str(recipe_id),
        "language_code": "en-US",
    }
    recipe_row = {
        "id": str(recipe_id),
        "title": "Recovered recipe",
        "ingredients": [],
        "ingredient_sections": [],
        "steps": [],
        "platform": "tiktok",
        "source_url_raw": "https://www.tiktok.com/@cook/video/1",
        "language_code": "en-US",
        "carousel_image_urls": [],
    }
    replacements = {
        "get_job": main.get_job,
        "user_can_access_job": main.user_can_access_job,
        "_expire_stale_job": main._expire_stale_job,
        "get_recipe": main.get_recipe,
        "save_user_recipe": main.save_user_recipe,
        "localized_recipe_row": main.localized_recipe_row,
    }
    main.get_job = lambda _job_id: row
    main.user_can_access_job = lambda **_kwargs: True
    main._expire_stale_job = lambda _row: False
    main.get_recipe = lambda _recipe_id: recipe_row
    main.save_user_recipe = lambda *_args, **_kwargs: (_ for _ in ()).throw(
        RuntimeError("temporary attachment failure")
    )
    main.localized_recipe_row = lambda recipe, _language: recipe
    try:
        with pytest.raises(HTTPException) as caught:
            main.get_job_status(job_id, object(), "en-US", user)
    finally:
        main.get_job = replacements["get_job"]
        main.user_can_access_job = replacements["user_can_access_job"]
        main._expire_stale_job = replacements["_expire_stale_job"]
        main.get_recipe = replacements["get_recipe"]
        main.save_user_recipe = replacements["save_user_recipe"]
        main.localized_recipe_row = replacements["localized_recipe_row"]

    assert caught.value.status_code == 503
    assert caught.value.headers["Retry-After"] == "1"
    assert caught.value.detail["code"] == "RECIPE_ATTACH_UNAVAILABLE"


def test_translation_jobs_have_language_scoped_concurrency_index():
    migration = Path("migrations/001_init.sql").read_text(encoding="utf-8")
    assert "on public.extract_jobs (recipe_id, language_code)" in migration
    assert "job_kind = 'extract'" in migration
    assert "job_kind = 'translation'" in migration


def test_recipe_write_uses_postgres_unique_conflict_and_jsonb_columns():
    source = Path("app/store.py").read_text(encoding="utf-8")
    assert "UniqueViolation" in source
    assert '"carousel_image_urls"' in source
    assert "Jsonb(payload[column])" in source


def test_carousel_migration_enforces_server_side_bound():
    migration = Path("migrations/001_init.sql").read_text(encoding="utf-8")
    assert "jsonb_array_length(" in migration
    assert ") <= 12" in migration


def test_job_owner_does_not_depend_on_access_table():
    user = AuthUser(uuid4(), None, None, False, None)
    row = {"id": str(uuid4()), "user_id": str(user.id)}
    original = main.grant_job_access
    main.grant_job_access = lambda **kwargs: (_ for _ in ()).throw(main.JobAccessUnavailable("access table down"))
    try:
        main._share_job_or_http(row, user)
        shared = dict(row, user_id=str(uuid4()))
        try:
            main._share_job_or_http(shared, user)
        except HTTPException as exc:
            assert exc.status_code == 503
            assert exc.detail["code"] == "JOB_ACCESS_UNAVAILABLE"
        else:
            raise AssertionError("shared job should fail closed when access storage is unavailable")
    finally:
        main.grant_job_access = original


def test_durable_worker_claim_is_atomic_and_web_defaults_to_background_tasks():
    migration = Path("migrations/001_init.sql").read_text(encoding="utf-8")
    config = Path("app/config.py").read_text(encoding="utf-8")
    main_source = Path("app/main.py").read_text(encoding="utf-8")
    worker = Path("app/worker.py").read_text(encoding="utf-8")
    assert "for update skip locked" in migration
    assert "lease_until" in migration
    assert "attempt_count" in migration
    assert "WORKER_ENABLED" in config.upper()
    assert "if not settings.worker_enabled" in main_source
    assert "local_claimed = not settings.worker_enabled" in main_source
    assert '"claim_next_extract_job"' in worker


def test_pipeline_clears_lease_on_terminal_state():
    source = Path("app/pipeline.py").read_text(encoding="utf-8")
    assert source.count("lease_until=None") >= 3


def test_database_advisor_hardening_indexes_foreign_keys_and_caches_rls_identity():
    migration = Path("migrations/001_init.sql").read_text(encoding="utf-8")
    assert "function public.set_updated_at()" in migration
    assert "security_events_user_idx" in migration
    assert "usage_events_job_idx" in migration
    assert "usage_events_recipe_idx" in migration
    assert "user_recipes_recipe_idx" in migration


def test_reserve_api_spend_qualifies_ledger_reserved_cents():
    migration = Path("migrations/001_init.sql").read_text(encoding="utf-8")
    assert "then ledger.reserved_cents else ledger.actual_cents" in migration
    assert migration.count("then ledger.reserved_cents else ledger.actual_cents") == 3


def test_account_deletion_atomically_anonymizes_job_identity_and_source():
    source = Path("app/store.py").read_text(encoding="utf-8")
    cleanup = source.split("def delete_account_data", 1)[1].split("def upsert_apple_refresh_token", 1)[0]
    assert "with get_conn() as conn" in cleanup
    assert "delete from user_recipes" in cleanup
    assert "user_id = null" in cleanup
    assert "source_url_raw = '[deleted]'" in cleanup
    assert "source_url_norm = '[deleted]:' || id::text" in cleanup
    assert "status = case when status in ('pending', 'processing') then 'failed'" in cleanup
    assert "lease_until = null" in cleanup
    assert "delete from extract_job_access" in cleanup
    assert "update auth_refresh_tokens" in cleanup
    assert "update profiles" in cleanup


def test_superwall_processing_errors_are_stored_without_exception_payload():
    source = Path("app/superwall.py").read_text(encoding="utf-8")
    assert 'error="processing_failed"' in source
    assert "error=str(exc)" not in source


def test_worker_mode_does_not_schedule_a_second_request_local_job():
    user = AuthUser(uuid4(), None, None, True, None)
    body = main.ExtractRequest(url="https://www.youtube.com/watch?v=worker-test", language="es-ES")
    request = Request({
        "type": "http",
        "method": "POST",
        "path": "/v1/extract",
        "headers": [],
        "query_string": b"",
        "scheme": "https",
        "client": ("127.0.0.1", 1234),
        "server": ("localhost", 8000),
    })

    class Background:
        def __init__(self):
            self.tasks = []

        def add_task(self, *args, **kwargs):
            self.tasks.append((args, kwargs))

    background = Background()
    replacements = {
        "get_recipe_by_norm": main.get_recipe_by_norm,
        "get_active_job": main.get_active_job,
        "assert_can_extract": main.assert_can_extract,
        "create_job": main.create_job,
        "reserve_spend": main.reserve_spend,
        "require_rate_limit": main.require_rate_limit,
        "validate_public_url": main.validate_public_url,
        "normalize_url": main.normalize_url,
        "count_user_open_extract_jobs": main.count_user_open_extract_jobs,
        "try_claim_job": main.try_claim_job,
        "openai_api_key": main.settings.openai_api_key,
        "worker_enabled": main.settings.worker_enabled,
    }
    main.get_recipe_by_norm = lambda _url: None
    main.get_active_job = lambda **_kwargs: None
    main.assert_can_extract = lambda *args, **_kwargs: None
    main.create_job = lambda **_kwargs: {"id": str(uuid4()), "status": "pending", "progress": 0}
    main.reserve_spend = lambda **_kwargs: None
    main.require_rate_limit = lambda *args, **_kwargs: None
    main.validate_public_url = lambda *args, **kwargs: None
    main.normalize_url = lambda _url: "youtube:worker-test"
    main.count_user_open_extract_jobs = lambda _uid: 0
    main.try_claim_job = lambda _uid: True
    main.settings.openai_api_key = "test-key"
    main.settings.worker_enabled = True
    try:
        response = main.extract_recipe(request, body, background, user)
    finally:
        main.get_recipe_by_norm = replacements["get_recipe_by_norm"]
        main.get_active_job = replacements["get_active_job"]
        main.assert_can_extract = replacements["assert_can_extract"]
        main.create_job = replacements["create_job"]
        main.reserve_spend = replacements["reserve_spend"]
        main.require_rate_limit = replacements["require_rate_limit"]
        main.validate_public_url = replacements["validate_public_url"]
        main.normalize_url = replacements["normalize_url"]
        main.count_user_open_extract_jobs = replacements["count_user_open_extract_jobs"]
        main.try_claim_job = replacements["try_claim_job"]
        main.settings.openai_api_key = replacements["openai_api_key"]
        main.settings.worker_enabled = replacements["worker_enabled"]

    assert response.status == JobStatus.pending
    assert background.tasks == []


def test_cached_recipe_attachment_failure_returns_retryable_error():
    user = AuthUser(uuid4(), None, None, False, None)
    cached = {
        "id": str(uuid4()),
        "source_url_raw": "https://www.youtube.com/watch?v=cached",
        "source_url_norm": "youtube:cached",
        "language_code": "en-US",
    }
    request = Request({
        "type": "http",
        "method": "POST",
        "path": "/v1/extract",
        "headers": [],
        "query_string": b"",
        "scheme": "https",
        "client": ("127.0.0.1", 1234),
        "server": ("localhost", 8000),
    })

    class Background:
        def add_task(self, *args, **kwargs):
            raise AssertionError("cache hit must not schedule extraction")

    replacements = {
        "openai_api_key": main.settings.openai_api_key,
        "get_recipe_by_norm": main.get_recipe_by_norm,
        "localized_recipe_row": main.localized_recipe_row,
        "normalize_url": main.normalize_url,
        "require_rate_limit": main.require_rate_limit,
        "validate_public_url": main.validate_public_url,
        "create_job": main.create_job,
        "save_user_recipe": main.save_user_recipe,
        "record_usage": main.record_usage,
    }
    main.settings.openai_api_key = ""
    main.get_recipe_by_norm = lambda _url: cached
    main.localized_recipe_row = lambda row, _language: row
    main.normalize_url = lambda _url: "youtube:cached"
    main.require_rate_limit = lambda *args, **kwargs: None
    main.validate_public_url = lambda *args, **kwargs: None
    main.create_job = lambda **kwargs: {"id": str(uuid4()), "status": "completed"}
    main.save_user_recipe = lambda *_args, **_kwargs: (_ for _ in ()).throw(
        RuntimeError("temporary attachment failure")
    )
    main.record_usage = lambda *args, **kwargs: None
    try:
        with pytest.raises(HTTPException) as caught:
            main.extract_recipe(
                request,
                main.ExtractRequest(url=cached["source_url_raw"], language="en-US"),
                Background(),
                user,
            )
    finally:
        main.settings.openai_api_key = replacements["openai_api_key"]
        main.get_recipe_by_norm = replacements["get_recipe_by_norm"]
        main.localized_recipe_row = replacements["localized_recipe_row"]
        main.normalize_url = replacements["normalize_url"]
        main.require_rate_limit = replacements["require_rate_limit"]
        main.validate_public_url = replacements["validate_public_url"]
        main.create_job = replacements["create_job"]
        main.save_user_recipe = replacements["save_user_recipe"]
        main.record_usage = replacements["record_usage"]

    assert caught.value.status_code == 503
    assert caught.value.headers["Retry-After"] == "1"
    assert caught.value.detail["code"] == "RECIPE_ATTACH_UNAVAILABLE"


def test_cached_delivery_replay_repairs_missing_user_recipe_link(monkeypatch):
    user = AuthUser(uuid4(), None, None, False, None)
    delivery_id = uuid4()
    job_id = uuid4()
    recipe_id = uuid4()
    request = Request({
        "type": "http",
        "method": "POST",
        "path": "/v1/extract",
        "headers": [],
        "query_string": b"",
        "scheme": "https",
        "client": ("127.0.0.1", 1234),
        "server": ("localhost", 8000),
    })

    class Background:
        def add_task(self, *args, **kwargs):
            raise AssertionError("delivery replay must not schedule extraction")

    attached = []
    monkeypatch.setattr(main, "validate_public_url", lambda *args, **kwargs: None)
    monkeypatch.setattr(main, "normalize_url", lambda _url: "youtube:cached")
    monkeypatch.setattr(
        main,
        "get_job_by_delivery_id",
        lambda *_args: {
            "id": job_id,
            "status": JobStatus.completed.value,
            "cache_hit": True,
            "recipe_id": recipe_id,
            "source_url_norm": "youtube:cached",
            "language_code": "en-US",
            "progress": 100,
        },
    )
    monkeypatch.setattr(main, "save_user_recipe", lambda uid, rid: attached.append((uid, rid)))

    response = main.extract_recipe(
        request,
        main.ExtractRequest(
            url="https://www.youtube.com/watch?v=cached",
            language="en-US",
            client_delivery_id=delivery_id,
        ),
        Background(),
        user,
    )

    assert response.job_id == job_id
    assert response.cache_hit is True
    assert attached == [(user.id, recipe_id)]


def test_failed_job_response_includes_stable_error_code(monkeypatch):
    user = AuthUser(uuid4(), None, None, False, None)
    job_id = uuid4()
    monkeypatch.setattr(main, "require_writes_enabled", lambda: None)
    monkeypatch.setattr(
        main,
        "get_job",
        lambda _job_id: {
            "id": job_id,
            "user_id": user.id,
            "status": JobStatus.failed.value,
            "error": "Extraction temporarily failed. Retry the import.",
            "updated_at": datetime.now(timezone.utc).isoformat(),
        },
    )
    monkeypatch.setattr(main, "user_can_access_job", lambda **_kwargs: True)

    response = main.get_job_status(job_id, BackgroundTasks(), "es-ES", user)

    assert response.error_code == "extraction_retryable"
    assert response.error


def test_cached_recipe_without_translation_does_not_create_job_without_openai_key():
    user = AuthUser(uuid4(), None, None, False, None)
    cached = {
        "id": str(uuid4()),
        "source_url_raw": "https://www.youtube.com/watch?v=translated-later",
        "source_url_norm": "youtube:translated-later",
        "language_code": "en-US",
    }
    request = Request({
        "type": "http",
        "method": "POST",
        "path": "/v1/extract",
        "headers": [],
        "query_string": b"",
        "scheme": "https",
        "client": ("127.0.0.1", 1234),
        "server": ("localhost", 8000),
    })

    replacements = {
        "openai_api_key": main.settings.openai_api_key,
        "get_recipe_by_norm": main.get_recipe_by_norm,
        "localized_recipe_row": main.localized_recipe_row,
        "normalize_url": main.normalize_url,
        "require_rate_limit": main.require_rate_limit,
        "validate_public_url": main.validate_public_url,
        "_ensure_translation_job": main._ensure_translation_job,
    }
    main.settings.openai_api_key = ""
    main.get_recipe_by_norm = lambda _url: cached
    main.localized_recipe_row = lambda row, _language: None
    main.normalize_url = lambda _url: "youtube:translated-later"
    main.require_rate_limit = lambda *args, **kwargs: None
    main.validate_public_url = lambda *args, **kwargs: None
    main._ensure_translation_job = lambda *args, **kwargs: (_ for _ in ()).throw(
        AssertionError("translation job must not be created without OpenAI")
    )
    try:
        try:
            main.extract_recipe(
                request,
                main.ExtractRequest(url=cached["source_url_raw"], language="es-ES"),
                type("Background", (), {})(),
                user,
            )
        except HTTPException as exc:
            assert exc.status_code == 503
            assert exc.detail == "OPENAI_API_KEY not configured"
        else:
            raise AssertionError("missing OpenAI key must fail before translation enqueue")
    finally:
        main.settings.openai_api_key = replacements["openai_api_key"]
        main.get_recipe_by_norm = replacements["get_recipe_by_norm"]
        main.localized_recipe_row = replacements["localized_recipe_row"]
        main.normalize_url = replacements["normalize_url"]
        main.require_rate_limit = replacements["require_rate_limit"]
        main.validate_public_url = replacements["validate_public_url"]
        main._ensure_translation_job = replacements["_ensure_translation_job"]


def test_active_job_is_reused_without_openai_key():
    user = AuthUser(uuid4(), None, None, False, None)
    active = {"id": str(uuid4()), "status": "processing", "progress": 64, "user_id": str(user.id)}
    request = Request({
        "type": "http",
        "method": "POST",
        "path": "/v1/extract",
        "headers": [],
        "query_string": b"",
        "scheme": "https",
        "client": ("127.0.0.1", 1234),
        "server": ("localhost", 8000),
    })
    replacements = {
        "openai_api_key": main.settings.openai_api_key,
        "get_recipe_by_norm": main.get_recipe_by_norm,
        "get_active_job": main.get_active_job,
        "normalize_url": main.normalize_url,
        "require_rate_limit": main.require_rate_limit,
        "validate_public_url": main.validate_public_url,
    }
    main.settings.openai_api_key = ""
    main.get_recipe_by_norm = lambda _url: None
    main.get_active_job = lambda **_kwargs: active
    main.normalize_url = lambda _url: "youtube:active"
    main.require_rate_limit = lambda *args, **kwargs: None
    main.validate_public_url = lambda *args, **kwargs: None
    try:
        response = main.extract_recipe(
            request,
            main.ExtractRequest(url="https://www.youtube.com/watch?v=active", language="en-US"),
            type("Background", (), {})(),
            user,
        )
    finally:
        main.settings.openai_api_key = replacements["openai_api_key"]
        main.get_recipe_by_norm = replacements["get_recipe_by_norm"]
        main.get_active_job = replacements["get_active_job"]
        main.normalize_url = replacements["normalize_url"]
        main.require_rate_limit = replacements["require_rate_limit"]
        main.validate_public_url = replacements["validate_public_url"]

    assert response.job_id == UUID(active["id"])
    assert response.status == JobStatus.processing
    assert response.progress == 64


def test_e2e_matrix_is_bounded_and_does_not_echo_secret_payloads():
    source = Path("scripts/e2e_matrix.sh").read_text(encoding="utf-8")
    assert "POLL_ATTEMPTS=\"${POLL_ATTEMPTS:-330}\"" in source
    assert "POLL_SECONDS=\"${POLL_SECONDS:-2}\"" in source
    assert "--retry-all-errors" in source
    assert "carousel_image_urls" in source
    assert "TOKEN=$(" in source
    assert "echo \"$TOKEN\"" not in source


def test_ios_auth_uses_backend_token_endpoints_and_secure_session_storage(ios_root: Path):
    auth = (ios_root / "ReciApp/Services/AuthService.swift").read_text(encoding="utf-8")
    assert 'post("v1/auth/apple", body: payload)' in auth
    assert '"v1/auth/refresh"' in auth
    assert '"com.membri.reciapp.auth"' in auth
    assert "kSecAttrAccessibleAfterFirstUnlockThisDeviceOnly" in auth
