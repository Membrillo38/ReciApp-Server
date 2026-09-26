from datetime import datetime, timezone
from uuid import uuid4

import pytest
from fastapi import HTTPException

from app.auth import AuthUser
from app import quota
from app.quota import QuotaStatus


def _limits_row():
    return {
        "free_weekly_limit": 1,
        "pro_monthly_price_cents": 499,
        "pro_margin_ratio": 0.20,
        "is_pro": False,
    }


def _patch_quota_reads(monkeypatch, *, miss_count: int):
    def fake_fetch_one(sql, params=None):
        text = " ".join(sql.split())
        if "from profiles" in text:
            return _limits_row()
        if "count(*) as count" in text:
            assert params[1] == datetime(datetime.now(timezone.utc).year, 1, 1, tzinfo=timezone.utc)
            assert "kind = 'extract_miss'" in text
            return {"count": miss_count}
        if "sum(cost_cents)" in text:
            return {"cost": 0}
        raise AssertionError(text)

    monkeypatch.setattr(quota, "fetch_one", fake_fetch_one)


def test_free_user_blocked_after_yearly_cap(monkeypatch):
    user = AuthUser(uuid4(), None, None, False, None)
    _patch_quota_reads(monkeypatch, miss_count=3)

    status = quota.get_quota(user)
    assert status.free_limit == 3
    assert status.free_used_this_week == 3
    assert status.free_remaining == 0

    with pytest.raises(HTTPException) as exc:
        quota.assert_can_extract(user, cache_hit=False)
    assert exc.value.status_code == 403
    assert exc.value.detail["code"] == "FREE_YEARLY_LIMIT"
    assert exc.value.detail["free_used_this_year"] == 3
    assert exc.value.detail["period"] == "year"
    assert exc.value.detail["reset_at"].endswith("+00:00")
    assert "per year" in exc.value.detail["message"]


def test_free_quota_denial_is_audited_with_code_and_correlation(monkeypatch):
    from types import SimpleNamespace

    user = AuthUser(uuid4(), None, None, False, None)
    request = SimpleNamespace(state=SimpleNamespace(correlation_id="corr-test"))
    events = []
    _patch_quota_reads(monkeypatch, miss_count=3)
    monkeypatch.setattr(quota, "audit_security_event", lambda **kwargs: events.append(kwargs))

    with pytest.raises(HTTPException):
        quota.assert_can_extract(user, cache_hit=False, request=request)

    assert len(events) == 1
    assert events[0]["event"] == "extract_quota_denied"
    assert events[0]["user_id"] == user.id
    assert events[0]["metadata"]["code"] == "FREE_YEARLY_LIMIT"
    assert events[0]["metadata"]["correlation_id"] == "corr-test"


def test_pro_fair_use_denial_is_audited_with_code(monkeypatch):
    from types import SimpleNamespace

    user = AuthUser(uuid4(), None, None, True, None)
    request = SimpleNamespace(state=SimpleNamespace(correlation_id="corr-pro"))
    events = []
    monkeypatch.setattr(
        quota,
        "get_quota",
        lambda _user: QuotaStatus(
            is_pro=True,
            free_used_this_week=0,
            free_limit=3,
            free_remaining=3,
            pro_cost_cents_this_month=1000,
            pro_budget_cents=1000,
            pro_remaining_cents=0,
            pro_monthly_price_cents=499,
        ),
    )
    monkeypatch.setattr(quota, "audit_security_event", lambda **kwargs: events.append(kwargs))

    with pytest.raises(HTTPException) as exc:
        quota.assert_can_extract(user, cache_hit=False, request=request)

    assert exc.value.detail["code"] == "PRO_FAIR_USE_LIMIT"
    assert len(events) == 1
    assert events[0]["user_id"] == user.id
    assert events[0]["metadata"]["code"] == "PRO_FAIR_USE_LIMIT"
    assert events[0]["metadata"]["correlation_id"] == "corr-pro"


def test_free_user_allowed_under_yearly_cap(monkeypatch):
    user = AuthUser(uuid4(), None, None, False, None)
    _patch_quota_reads(monkeypatch, miss_count=2)

    status = quota.get_quota(user)
    assert status.free_remaining == 1
    quota.assert_can_extract(user, cache_hit=False)


def test_pro_user_skips_yearly_recipe_cap(monkeypatch):
    user = AuthUser(uuid4(), None, None, True, None)
    _patch_quota_reads(monkeypatch, miss_count=100)
    quota.assert_can_extract(user, cache_hit=False)
