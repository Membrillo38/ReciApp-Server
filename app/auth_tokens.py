from __future__ import annotations

import hashlib
import hmac
import secrets
from base64 import urlsafe_b64encode
from datetime import datetime, timedelta, timezone
from uuid import UUID

import jwt

from app.config import settings
from app.db import execute, execute_returning, fetch_one

REFRESH_ROTATION_REPLAY_SECONDS = 900
_REFRESH_ROTATION_DOMAIN = b"reciapp.refresh-rotation.v1\0"


def _hash(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _refresh_token_for_request(raw: str, request_id: UUID) -> str:
    secret = settings.auth_jwt_secret.encode("utf-8")
    if not secret:
        raise RuntimeError("AUTH_JWT_SECRET is required for refresh rotation")
    material = _REFRESH_ROTATION_DOMAIN + _hash(raw).encode("ascii") + b"\0" + request_id.bytes
    digest = hmac.new(secret, material, hashlib.sha256).digest()
    return urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def create_access_token(user_id: UUID, email: str | None) -> str:
    now = datetime.now(timezone.utc)
    payload = {
        "sub": str(user_id),
        "email": email,
        "iss": settings.auth_jwt_issuer,
        "aud": settings.auth_jwt_audience,
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(seconds=settings.auth_access_token_ttl_seconds)).timestamp()),
    }
    return jwt.encode(payload, settings.auth_jwt_secret, algorithm="HS256")


def create_refresh_token(user_id: UUID, *, user_agent: str | None = None, ip_hash: str | None = None) -> str:
    raw = secrets.token_urlsafe(48)
    expires_at = datetime.now(timezone.utc) + timedelta(seconds=settings.auth_refresh_token_ttl_seconds)
    execute(
        """
        insert into auth_refresh_tokens (user_id, token_hash, expires_at, user_agent, ip_hash)
        values (%s, %s, %s, %s, %s)
        """,
        (user_id, _hash(raw), expires_at, (user_agent or "")[:300] or None, ip_hash),
    )
    return raw


def rotate_refresh_token(raw: str, request_id: UUID | None = None) -> dict | None:
    old_hash = _hash(raw)
    new_refresh = (
        _refresh_token_for_request(raw, request_id)
        if request_id is not None
        else secrets.token_urlsafe(48)
    )
    new_hash = _hash(new_refresh)
    expires_at = datetime.now(timezone.utc) + timedelta(seconds=settings.auth_refresh_token_ttl_seconds)
    row = execute_returning(
        """
        with source as (
            select t.id, t.user_id, t.user_agent, t.ip_hash
              from auth_refresh_tokens t
              join profiles p on p.id = t.user_id
             where t.token_hash = %s
               and t.revoked_at is null
               and t.expires_at > now()
               and p.deleted_at is null
             for update of t
        ), revoked as (
            update auth_refresh_tokens t
               set revoked_at = now()
                   , rotation_request_id = %s
                   , rotation_retry_until = case
                       when %s::uuid is null then null
                       else now() + (%s * interval '1 second')
                     end
                   , rotated_token_hash = case
                       when %s::uuid is null then null
                       else %s
                     end
              from source
             where t.id = source.id
            returning t.user_id, t.user_agent, t.ip_hash
        ), issued as (
            insert into auth_refresh_tokens (user_id, token_hash, expires_at, user_agent, ip_hash)
            select user_id, %s, %s, user_agent, ip_hash from revoked
            returning user_id
        )
        select issued.user_id, p.email
          from issued
          join profiles p on p.id = issued.user_id
         where p.deleted_at is null
        """,
        (
            old_hash,
            request_id,
            request_id,
            REFRESH_ROTATION_REPLAY_SECONDS,
            request_id,
            new_hash,
            new_hash,
            expires_at,
        ),
    )
    if not row and request_id is not None:
        row = fetch_one(
            """
            select t.user_id, p.email
              from auth_refresh_tokens t
              join profiles p on p.id = t.user_id
              join auth_refresh_tokens successor
                on successor.token_hash = t.rotated_token_hash
               and successor.revoked_at is null
               and successor.expires_at > now()
             where t.token_hash = %s
               and t.revoked_at is not null
               and t.rotation_request_id = %s
               and t.rotation_retry_until > now()
               and p.deleted_at is null
             limit 1
            """,
            (old_hash, request_id),
        )
    if not row:
        return None
    return {
        "access_token": create_access_token(UUID(str(row["user_id"])), row.get("email")),
        "refresh_token": new_refresh,
    }


def revoke_refresh_token(raw: str, request_id: UUID | None = None) -> None:
    if not raw:
        return
    old_hash = _hash(raw)
    if request_id is not None:
        execute(
            """
            with successor as (
                select rotated_token_hash
                  from auth_refresh_tokens
                 where token_hash = %s
                   and rotation_request_id = %s
                   and rotated_token_hash is not null
            )
            update auth_refresh_tokens t
               set revoked_at = coalesce(t.revoked_at, now())
             where t.token_hash = %s
                or t.token_hash = (select rotated_token_hash from successor)
            """,
            (old_hash, request_id, old_hash),
        )
        return
    execute(
        """
        update auth_refresh_tokens
           set revoked_at = now()
         where token_hash = %s
           and revoked_at is null
        """,
        (old_hash,),
    )


def revoke_all_refresh_tokens(user_id: UUID) -> None:
    execute(
        """
        update auth_refresh_tokens
           set revoked_at = now()
         where user_id = %s
           and revoked_at is null
        """,
        (user_id,),
    )
