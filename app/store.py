from __future__ import annotations

import re
import os
from pathlib import Path
from urllib.parse import urlparse
from uuid import UUID

from psycopg.errors import UniqueViolation
from psycopg.types.json import Jsonb

from app.config import settings
from app.db import execute, execute_returning, fetch_all, fetch_one, get_conn
from app.localization import ingredient_section_name, normalize_language
from app.translation_cache import get_recipe_translations, source_recipe_fingerprint
from app.models import (
    Ingredient,
    IngredientSection,
    Platform,
    Recipe,
    RecipePublic,
    RecipeSummary,
    RecipeTip,
    Step,
)


class JobAccessUnavailable(RuntimeError):
    """The shared-job access table could not be read or written."""


_MAX_CAROUSEL_IMAGES = 12
_MAX_COVER_BYTES = 2_000_000
_JSONB_COLUMNS = {"carousel_image_urls", "ingredients", "ingredient_sections", "steps", "tags", "missing_fields", "tips"}


def upload_cover_jpeg(jpeg: bytes, *, key: str) -> str | None:
    """Store a public cover JPEG. Returns None when storage is unavailable."""
    # Size + magic bytes (SOI). Extension alone is not a type check.
    if not jpeg or len(jpeg) > _MAX_COVER_BYTES or not jpeg.startswith(b"\xff\xd8\xff"):
        return None
    safe_key = re.sub(r"[^A-Za-z0-9._-]", "_", key.strip())[:120]
    if not safe_key.endswith(".jpg"):
        return None
    try:
        directory = Path(os.environ.get("COVER_STORAGE_DIR", "/tmp/reciapp-covers"))
        directory.mkdir(parents=True, exist_ok=True)
        (directory / safe_key).write_bytes(jpeg)
    except Exception:
        return None
    base = settings.cover_public_base_url.strip().rstrip("/")
    if not base:
        return None
    url = f"{base}/{safe_key}"
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return None
    return url


def _carousel_urls(values: object) -> list[str]:
    raw_values = values if isinstance(values, list) else []
    urls: list[str] = []
    seen: set[str] = set()
    for value in raw_values:
        candidate = str(value or "").strip()
        parsed = urlparse(candidate)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            continue
        if len(candidate) > 2048 or candidate in seen:
            continue
        seen.add(candidate)
        urls.append(candidate)
        if len(urls) >= _MAX_CAROUSEL_IMAGES:
            break
    return urls


def _ingredient_sections_from_row(row: dict) -> list[IngredientSection]:
    raw_sections = row.get("ingredient_sections") or []
    if raw_sections:
        return [IngredientSection(**section) for section in raw_sections]

    ingredients = [Ingredient(**ingredient) for ingredient in (row.get("ingredients") or [])]
    language_code = normalize_language(row.get("language_code"))
    return [IngredientSection(title=ingredient_section_name(language_code), ingredients=ingredients)] if ingredients else []


def _flatten_ingredient_sections(sections: list[IngredientSection]) -> list[Ingredient]:
    return [ingredient for section in sections for ingredient in section.ingredients]


def _tips_from_row(row: dict) -> list[RecipeTip]:
    return [RecipeTip(**tip) for tip in (row.get("tips") or [])]


def recipe_from_row(row: dict) -> Recipe:
    ingredient_sections = _ingredient_sections_from_row(row)
    return Recipe(
        id=(row["id"] if isinstance(row["id"], UUID) else UUID(str(row["id"]))) if row.get("id") else None,
        title=row["title"],
        ingredients=_flatten_ingredient_sections(ingredient_sections),
        ingredient_sections=ingredient_sections,
        steps=[Step(**s) for s in (row.get("steps") or [])],
        servings=row.get("servings"),
        prep_minutes=row.get("prep_minutes"),
        cook_minutes=row.get("cook_minutes"),
        tags=row.get("tags") or [],
        confidence=float(row.get("confidence") or 0.5),
        missing_fields=row.get("missing_fields") or [],
        source_url=row.get("source_url_raw") or row.get("source_url") or "",
        platform=Platform(row.get("platform") or "unknown"),
        thumbnail_url=row.get("thumbnail_url"),
        carousel_image_urls=_carousel_urls(row.get("carousel_image_urls")),
        author=row.get("author"),
        description=row.get("description"),
        tips=_tips_from_row(row),
        language_code=normalize_language(row.get("language_code")),
        raw_transcript=row.get("raw_transcript"),
    )


def recipe_public_from_row(row: dict) -> RecipePublic:
    rid = row.get("id")
    if not rid:
        raise ValueError("recipe row missing id")
    ingredient_sections = _ingredient_sections_from_row(row)
    return RecipePublic(
        id=UUID(str(rid)),
        title=row["title"],
        ingredients=_flatten_ingredient_sections(ingredient_sections),
        ingredient_sections=ingredient_sections,
        steps=[Step(**s) for s in (row.get("steps") or [])],
        servings=row.get("servings"),
        prep_minutes=row.get("prep_minutes"),
        cook_minutes=row.get("cook_minutes"),
        tags=row.get("tags") or [],
        source_url=row.get("source_url_raw") or row.get("source_url") or "",
        platform=Platform(row.get("platform") or "unknown"),
        thumbnail_url=row.get("thumbnail_url"),
        carousel_image_urls=_carousel_urls(row.get("carousel_image_urls")),
        author=row.get("author"),
        description=row.get("description"),
        tips=_tips_from_row(row),
        language_code=normalize_language(row.get("language_code")),
    )


def recipe_summary_from_row(row: dict, *, saved_at: str) -> RecipeSummary:
    rid = row.get("id")
    if not rid:
        raise ValueError("recipe row missing id")
    return RecipeSummary(
        id=UUID(str(rid)),
        title=row["title"],
        platform=Platform(row.get("platform") or "unknown"),
        source_url=row.get("source_url_raw") or row.get("source_url") or "",
        thumbnail_url=row.get("thumbnail_url"),
        author=row.get("author"),
        servings=row.get("servings"),
        prep_minutes=row.get("prep_minutes"),
        cook_minutes=row.get("cook_minutes"),
        saved_at=saved_at,
        language_code=normalize_language(row.get("language_code")),
    )


def recipe_to_row(recipe: Recipe, *, source_url_norm: str, language_code: str = "en-US") -> dict:
    return {
        "source_url_raw": recipe.source_url,
        "source_url_norm": source_url_norm,
        "language_code": language_code,
        "platform": recipe.platform.value,
        "title": recipe.title,
        "description": recipe.description,
        "author": recipe.author,
        "thumbnail_url": recipe.thumbnail_url,
        "carousel_image_urls": _carousel_urls(recipe.carousel_image_urls),
        "ingredients": [i.model_dump() for i in recipe.ingredients],
        "ingredient_sections": [section.model_dump() for section in recipe.ingredient_sections],
        "steps": [s.model_dump() for s in recipe.steps],
        "servings": recipe.servings,
        "prep_minutes": recipe.prep_minutes,
        "cook_minutes": recipe.cook_minutes,
        "tags": recipe.tags,
        "confidence": recipe.confidence,
        "missing_fields": recipe.missing_fields,
        "raw_transcript": recipe.raw_transcript,
        "tips": [tip.model_dump() for tip in recipe.tips],
    }


def _insert_sql(table: str, payload: dict, *, returning: str = "*") -> tuple[str, tuple]:
    columns = list(payload)
    placeholders = ", ".join(["%s"] * len(columns))
    names = ", ".join(columns)
    return f"insert into {table} ({names}) values ({placeholders}) returning {returning}", _values_for_columns(payload, columns)


def _values_for_columns(payload: dict, columns: list[str]) -> tuple:
    return tuple(
        Jsonb(payload[column]) if column in _JSONB_COLUMNS else payload[column]
        for column in columns
    )


def _update_sql(table: str, payload: dict, where: str, *, returning: str = "*") -> str:
    assignments = ", ".join(f"{column} = %s" for column in payload)
    return f"update {table} set {assignments} {where} returning {returning}"


def get_recipe_by_norm(url_norm: str) -> dict | None:
    return fetch_one("select * from recipes where source_url_norm = %s limit 1", (url_norm,))


def get_job_by_delivery_id(user_id: UUID, client_delivery_id: UUID) -> dict | None:
    return fetch_one(
        """
        select * from extract_jobs
         where user_id = %s and client_delivery_id = %s
         limit 1
        """,
        (user_id, client_delivery_id),
    )


def upsert_recipe(recipe: Recipe, *, source_url_norm: str, language_code: str = "en-US") -> dict:
    existing = get_recipe_by_norm(source_url_norm)
    payload = recipe_to_row(recipe, source_url_norm=source_url_norm, language_code=language_code)

    def write(row_payload: dict) -> dict:
        if existing:
            sql = _update_sql("recipes", row_payload, "where id = %s")
            return execute_returning(sql, (*_values_for_columns(row_payload, list(row_payload)), existing["id"])) or existing
        try:
            sql, params = _insert_sql("recipes", row_payload)
            return execute_returning(sql, params) or {}
        except UniqueViolation:
            winner = get_recipe_by_norm(source_url_norm)
            if winner:
                return winner
            raise

    return write(payload)


def save_user_recipe(user_id: UUID, recipe_id: UUID) -> None:
    execute(
        """
        insert into user_recipes (user_id, recipe_id)
        select id, %s from profiles where id = %s and deleted_at is null
        on conflict (user_id, recipe_id) do nothing
        """,
        (recipe_id, user_id),
    )


_RECIPE_SUMMARY_COLS = (
    "id,title,thumbnail_url,platform,source_url_raw,author,servings,prep_minutes,cook_minutes,language_code"
)


def list_user_recipe_summaries(user_id: UUID, language_code: str = "en-US") -> list[RecipeSummary]:
    links = fetch_all(
        f"""
        select ur.saved_at, ur.recipe_id, to_jsonb(r) as recipes
          from user_recipes ur
          join recipes r on r.id = ur.recipe_id
         where ur.user_id = %s
         order by ur.saved_at desc
        """,
        (user_id,),
    )
    recipes = [row.get("recipes") for row in links if row.get("recipes")]
    target = normalize_language(language_code)
    translations = get_recipe_translations(
        [UUID(str(recipe["id"])) for recipe in recipes if recipe.get("id")],
        target,
    )
    out: list[RecipeSummary] = []
    for row in links:
        recipe = row.get("recipes")
        saved_at = row.get("saved_at")
        if recipe and saved_at:
            recipe = dict(recipe)
            recipe_id = str(recipe.get("id") or "")
            if normalize_language(recipe.get("language_code")) != target:
                translation = translations.get(recipe_id)
                if translation and translation.get("source_fingerprint") == source_recipe_fingerprint(recipe):
                    recipe.update(translation.get("payload") or {})
                    recipe["language_code"] = target
            out.append(recipe_summary_from_row(recipe, saved_at=str(saved_at)))
    return out


def user_owns_recipe(user_id: UUID, recipe_id: UUID) -> bool:
    return bool(fetch_one("select recipe_id from user_recipes where user_id = %s and recipe_id = %s limit 1", (user_id, recipe_id)))


def list_user_recipes(user_id: UUID) -> list[dict]:
    links = fetch_all(
        """
        select ur.saved_at, ur.recipe_id, to_jsonb(r) as recipes
          from user_recipes ur
          join recipes r on r.id = ur.recipe_id
         where ur.user_id = %s
         order by ur.saved_at desc
        """,
        (user_id,),
    )
    out = []
    for row in links:
        recipe = row.get("recipes")
        if recipe:
            recipe = dict(recipe)
            recipe["saved_at"] = row.get("saved_at")
            out.append(recipe)
    return out


def delete_user_recipe(user_id: UUID, recipe_id: UUID) -> bool:
    row = execute_returning(
        "delete from user_recipes where user_id = %s and recipe_id = %s returning recipe_id",
        (user_id, recipe_id),
    )
    return bool(row)


def create_job(
    *,
    user_id: UUID,
    source_url_raw: str,
    source_url_norm: str,
    language_code: str = "en-US",
    job_kind: str = "extract",
    status: str = "pending",
    cache_hit: bool = False,
    recipe_id: UUID | None = None,
    client_delivery_id: UUID | None = None,
    cost_cents: float = 0,
) -> dict:
    payload = {
        "user_id": user_id,
        "status": status,
        "source_url_raw": source_url_raw,
        "source_url_norm": source_url_norm,
        "language_code": language_code,
        "job_kind": job_kind,
        "cache_hit": cache_hit,
        "cost_cents": cost_cents,
        "recipe_id": recipe_id,
        "progress": 100 if status == "completed" else 0,
    }
    if client_delivery_id is not None:
        payload["client_delivery_id"] = client_delivery_id
    sql, params = _insert_sql("extract_jobs", payload)
    try:
        return execute_returning(sql, params) or {}
    except UniqueViolation:
        if client_delivery_id is not None:
            replay = get_job_by_delivery_id(user_id, client_delivery_id)
            if replay:
                replay["_idempotent_replay"] = True
                return replay
        # Concurrent extract for same normalized URL — return the winner.
        active = get_active_job(
            source_url_norm=source_url_norm,
            language_code=language_code,
            job_kind=job_kind,
            recipe_id=recipe_id,
        )
        if active:
            return active
        raise


def update_job(job_id: UUID, **fields) -> dict | None:
    clean = {k: v for k, v in fields.items()}
    if not clean:
        return get_job(job_id)
    sql = _update_sql("extract_jobs", clean, "where id = %s")
    return execute_returning(sql, (*clean.values(), job_id))


def get_job(job_id: UUID) -> dict | None:
    return fetch_one("select * from extract_jobs where id = %s limit 1", (job_id,))


def get_active_job_for_user(*, user_id: UUID, source_url_norm: str, language_code: str = "en-US", job_kind: str = "extract") -> dict | None:
    # Kept as a compatibility helper for callers outside the API module.
    return get_active_job(source_url_norm=source_url_norm, language_code=language_code, job_kind=job_kind)


def get_active_job(*, source_url_norm: str, language_code: str = "en-US", job_kind: str = "extract", recipe_id: UUID | None = None) -> dict | None:
    if job_kind == "translation":
        if recipe_id is None:
            return None
        return fetch_one(
            """
            select * from extract_jobs
             where source_url_norm = %s
               and job_kind = %s
               and status = any(%s)
               and recipe_id = %s
               and language_code = %s
             order by created_at desc
             limit 1
            """,
            (source_url_norm, job_kind, ["pending", "processing"], recipe_id, normalize_language(language_code)),
        )
    return fetch_one(
        """
        select * from extract_jobs
         where source_url_norm = %s
           and job_kind = %s
           and status = any(%s)
         order by created_at desc
         limit 1
        """,
        (source_url_norm, job_kind, ["pending", "processing"]),
    )


def grant_job_access(*, job_id: UUID, user_id: UUID) -> None:
    try:
        execute(
            """
            insert into extract_job_access (job_id, user_id)
            values (%s, %s)
            on conflict (job_id, user_id) do nothing
            """,
            (job_id, user_id),
        )
    except Exception as exc:
        raise JobAccessUnavailable("Shared job access unavailable") from exc


def user_can_access_job(*, job_id: UUID, user_id: UUID, row: dict | None = None) -> bool:
    # Reuse a row already loaded by the endpoint when available. A second
    # read creates a needless race where a transient DB read can turn a valid
    # owner poll into a false 403.
    row = row or get_job(job_id)
    if row and str(row.get("user_id") or "") == str(user_id):
        return True
    try:
        return bool(fetch_one("select job_id from extract_job_access where job_id = %s and user_id = %s limit 1", (job_id, user_id)))
    except Exception as exc:
        raise JobAccessUnavailable("Shared job access unavailable") from exc


def list_jobs(limit: int = 50) -> list[dict]:
    return fetch_all("select * from extract_jobs order by created_at desc limit %s", (limit,))


def count_user_open_extract_jobs(user_id: UUID) -> int:
    """Pending + processing extract jobs owned by the user (serial queue size)."""
    row = fetch_one(
        """
        select count(*) as count
          from extract_jobs
         where user_id = %s and job_kind = 'extract' and status = any(%s)
        """,
        (user_id, ["pending", "processing"]),
    )
    return int((row or {}).get("count") or 0)


def user_has_processing_extract(user_id: UUID) -> bool:
    return bool(fetch_one("select id from extract_jobs where user_id = %s and job_kind = 'extract' and status = 'processing' limit 1", (user_id,)))


def list_user_open_extract_jobs(user_id: UUID, *, limit: int = 50) -> list[dict]:
    """Oldest-first pending/processing extracts for the user's import queue."""
    return fetch_all(
        """
        select * from extract_jobs
         where user_id = %s and job_kind = 'extract' and status = any(%s)
         order by created_at
         limit %s
        """,
        (user_id, ["pending", "processing"], limit),
    )


def list_live_queue_jobs(*, limit: int = 100) -> list[dict]:
    """Dashboard live queue: processing then pending, oldest first."""
    processing = fetch_all(
        "select * from extract_jobs where job_kind = 'extract' and status = 'processing' order by created_at limit %s",
        (limit,),
    )
    remaining = max(0, limit - len(processing))
    pending = fetch_all(
        "select * from extract_jobs where job_kind = 'extract' and status = 'pending' order by created_at limit %s",
        (remaining,),
    ) if remaining else []
    return list(processing) + list(pending)


def claim_next_pending_extract_for_user(user_id: UUID) -> dict | None:
    """Atomically flip oldest pending extract for user to processing. Returns row or None."""
    return _claim_next_pending_extract(user_id=user_id)


def claim_next_pending_extract() -> dict | None:
    """Oldest pending extract across all users (global drain when a slot frees)."""
    return _claim_next_pending_extract(user_id=None)


def _claim_next_pending_extract(*, user_id: UUID | None) -> dict | None:
    user_filter = "and user_id = %s" if user_id is not None else ""
    params: tuple = (user_id,) if user_id is not None else ()
    return execute_returning(
        f"""
        with candidate as (
            select id from extract_jobs
             where job_kind = 'extract'
               and status = 'pending'
               {user_filter}
             order by created_at
             for update skip locked
             limit 1
        )
        update extract_jobs job
           set status = 'processing', progress = 0, error = null
          from candidate
         where job.id = candidate.id
        returning job.*
        """,
        params,
    )


def list_recipes(limit: int = 50) -> list[dict]:
    return fetch_all("select * from recipes order by created_at desc limit %s", (limit,))


def get_recipe(recipe_id: UUID) -> dict | None:
    return fetch_one("select * from recipes where id = %s limit 1", (recipe_id,))


def list_profiles(limit: int = 100) -> list[dict]:
    return fetch_all("select * from profiles where deleted_at is null order by created_at desc limit %s", (limit,))


def release_deleted_apple_identity(*, apple_sub: str, email: str | None) -> None:
    """Free Apple/email unique keys on closed accounts so Sign in with Apple can create a new profile."""
    if email:
        execute(
            """
            update profiles
               set apple_sub = null,
                   email = null
             where deleted_at is not null
               and (
                    apple_sub = %s
                    or email = %s
               )
            """,
            (apple_sub, email),
        )
        return
    execute(
        """
        update profiles
           set apple_sub = null,
               email = null
         where deleted_at is not null
           and apple_sub = %s
        """,
        (apple_sub,),
    )


def delete_account_data(user_id: UUID) -> None:
    """Scrub account-owned data and close its sessions atomically."""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("delete from user_recipes where user_id = %s", (user_id,))
            cur.execute(
                """
                update extract_jobs
                   set user_id = null,
                       status = case when status in ('pending', 'processing') then 'failed' else status end,
                       progress = case when status in ('pending', 'processing') then 0 else progress end,
                       lease_until = null,
                       source_url_raw = '[deleted]',
                       source_url_norm = '[deleted]:' || id::text,
                       error = null
                 where user_id = %s
                """,
                (user_id,),
            )
            cur.execute(
                "update api_spend_ledger set user_id = null where user_id = %s",
                (user_id,),
            )
            cur.execute(
                "update security_events set user_id = null where user_id = %s",
                (user_id,),
            )
            cur.execute(
                "delete from extract_job_access where user_id = %s",
                (user_id,),
            )
            cur.execute(
                """
                update auth_refresh_tokens
                   set revoked_at = coalesce(revoked_at, now())
                 where user_id = %s and revoked_at is null
                """,
                (user_id,),
            )
            cur.execute(
                """
                update profiles
                   set deleted_at = coalesce(deleted_at, now()),
                       email = null,
                       display_name = null,
                       is_pro = false,
                       pro_expires_at = null
                 where id = %s
                """,
                (user_id,),
            )


def upsert_apple_refresh_token(user_id: UUID, ciphertext: str) -> None:
    execute(
        """
        insert into auth_provider_tokens (user_id, provider, token_ciphertext)
        values (%s, 'apple', %s)
        on conflict (user_id, provider) do update
           set token_ciphertext = excluded.token_ciphertext
        """,
        (user_id, ciphertext),
    )


def get_apple_refresh_token_ciphertext(user_id: UUID) -> str | None:
    row = fetch_one(
        """
        select token_ciphertext
          from auth_provider_tokens
         where user_id = %s
           and provider = 'apple'
         limit 1
        """,
        (user_id,),
    )
    if not row:
        return None
    value = str(row.get("token_ciphertext") or "").strip()
    return value or None


def delete_apple_refresh_token(user_id: UUID) -> None:
    execute(
        """
        delete from auth_provider_tokens
         where user_id = %s
           and provider = 'apple'
        """,
        (user_id,),
    )
