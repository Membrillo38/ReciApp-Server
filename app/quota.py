from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from uuid import UUID

from fastapi import HTTPException
from starlette.requests import Request

from app.auth import AuthUser
from app.db import execute, fetch_one
from app.limits import resolve_user_limits
from app.security import audit_security_event

# Product cap for non-Pro. DB free_weekly_limit is legacy; this is the ceiling.
FREE_YEARLY_LIMIT = 3


@dataclass
class QuotaStatus:
    is_pro: bool
    free_used_this_week: int
    free_limit: int
    free_remaining: int
    pro_cost_cents_this_month: float
    pro_budget_cents: float
    pro_remaining_cents: float
    pro_monthly_price_cents: int


def _month_start_utc(now: datetime | None = None) -> datetime:
    now = now or datetime.now(timezone.utc)
    return now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)


def _year_start_utc(now: datetime | None = None) -> datetime:
    now = now or datetime.now(timezone.utc)
    return now.replace(month=1, day=1, hour=0, minute=0, second=0, microsecond=0)


def _next_month_start_utc(now: datetime | None = None) -> datetime:
    now = now or datetime.now(timezone.utc)
    if now.month == 12:
        return now.replace(year=now.year + 1, month=1, day=1, hour=0, minute=0, second=0, microsecond=0)
    return now.replace(month=now.month + 1, day=1, hour=0, minute=0, second=0, microsecond=0)


def _next_year_start_utc(now: datetime | None = None) -> datetime:
    now = now or datetime.now(timezone.utc)
    return now.replace(year=now.year + 1, month=1, day=1, hour=0, minute=0, second=0, microsecond=0)


def get_quota(user: AuthUser) -> QuotaStatus:
    year_start = _year_start_utc()
    month_start = _month_start_utc()

    row = fetch_one(
        """
        select free_weekly_limit, pro_monthly_price_cents, pro_margin_ratio, is_pro
          from profiles
         where id = %s
         limit 1
        """,
        (user.id,),
    ) or {}
    limits = resolve_user_limits(row)
    free_limit = FREE_YEARLY_LIMIT

    year = fetch_one(
        """
        select count(*) as count
          from usage_events
         where user_id = %s and kind = 'extract_miss' and created_at >= %s
        """,
        (user.id, year_start),
    )
    free_used = int((year or {}).get("count") or 0)

    month = fetch_one(
        """
        select coalesce(sum(cost_cents), 0) as cost
          from usage_events
         where user_id = %s and kind = 'extract_miss' and created_at >= %s
        """,
        (user.id, month_start),
    )
    pro_cost = float((month or {}).get("cost") or 0)

    return QuotaStatus(
        is_pro=user.is_pro,
        free_used_this_week=free_used,
        free_limit=free_limit,
        free_remaining=max(free_limit - free_used, 0),
        pro_cost_cents_this_month=pro_cost,
        pro_budget_cents=limits.pro_budget_cents,
        pro_remaining_cents=max(limits.pro_budget_cents - pro_cost, 0),
        pro_monthly_price_cents=limits.pro_monthly_price_cents,
    )


def assert_can_extract(
    user: AuthUser, *, cache_hit: bool, request: Request | None = None
) -> None:
    q = get_quota(user)

    if not user.is_pro:
        if q.free_remaining <= 0:
            if request is not None:
                audit_security_event(
                    event="extract_quota_denied",
                    request=request,
                    user_id=user.id,
                    metadata={
                        "code": "FREE_YEARLY_LIMIT",
                        "period": "year",
                        "limit": q.free_limit,
                        "used": q.free_used_this_week,
                        "correlation_id": getattr(request.state, "correlation_id", None),
                    },
                )
            raise HTTPException(
                status_code=403,
                detail={
                    "code": "FREE_YEARLY_LIMIT",
                    "message": f"Free plan: {q.free_limit} recipe(s) per year. Upgrade to Pro.",
                    "free_used_this_week": q.free_used_this_week,
                    "free_used_this_year": q.free_used_this_week,
                    "free_limit": q.free_limit,
                    "period": "year",
                    "reset_at": _next_year_start_utc().isoformat(),
                },
            )
        return

    if cache_hit:
        return
    if q.pro_remaining_cents <= 0:
        if request is not None:
            audit_security_event(
                event="extract_quota_denied",
                request=request,
                user_id=user.id,
                metadata={
                    "code": "PRO_FAIR_USE_LIMIT",
                    "period": "month",
                    "correlation_id": getattr(request.state, "correlation_id", None),
                },
            )
        raise HTTPException(
            status_code=403,
            detail={
                "code": "PRO_FAIR_USE_LIMIT",
                "message": "Pro fair-use limit reached this month (keeps margin).",
                "pro_cost_cents_this_month": q.pro_cost_cents_this_month,
                "pro_budget_cents": q.pro_budget_cents,
                "pro_monthly_price_cents": q.pro_monthly_price_cents,
                "period": "month",
                "reset_at": _next_month_start_utc().isoformat(),
            },
        )


def record_usage(
    *,
    user_id: UUID,
    kind: str,
    cost_cents: float,
    recipe_id: UUID | None,
    job_id: UUID | None,
) -> None:
    execute(
        """
        insert into usage_events (user_id, kind, cost_cents, recipe_id, job_id)
        values (%s, %s, %s, %s, %s)
        """,
        (user_id, kind, cost_cents, recipe_id, job_id),
    )
