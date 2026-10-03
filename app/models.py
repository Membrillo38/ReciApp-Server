from __future__ import annotations

import json
from datetime import datetime
from enum import Enum
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, Field, HttpUrl, ValidationError, field_validator


class Platform(str, Enum):
    tiktok = "tiktok"
    youtube = "youtube"
    instagram = "instagram"
    facebook = "facebook"
    unknown = "unknown"


class Ingredient(BaseModel):
    name: str
    quantity: str | None = None
    unit: str | None = None
    # Culinary bulk density for volume↔mass. Positive or null; never invent.
    density_g_per_ml: float | None = None

    @field_validator("density_g_per_ml", mode="before")
    @classmethod
    def _sanitize_density(cls, value: object) -> float | None:
        from app.ingredient_density import parse_density_g_per_ml

        return parse_density_g_per_ml(value)


class IngredientSection(BaseModel):
    title: str
    ingredients: list[Ingredient] = Field(default_factory=list)


class RecipeTip(BaseModel):
    title: str | None = None
    text: str


class Step(BaseModel):
    order: int
    text: str
    duration_minutes: int | None = None


class Recipe(BaseModel):
    """Internal / admin — includes debug fields."""
    id: UUID | None = None
    title: str
    ingredients: list[Ingredient]
    ingredient_sections: list[IngredientSection] = Field(default_factory=list)
    steps: list[Step]
    servings: int | None = None
    prep_minutes: int | None = None
    cook_minutes: int | None = None
    tags: list[str] = Field(default_factory=list)
    confidence: float = Field(ge=0, le=1, default=0.5)
    missing_fields: list[str] = Field(default_factory=list)
    source_url: str
    platform: Platform
    thumbnail_url: str | None = None
    carousel_image_urls: list[str] = Field(default_factory=list)
    author: str | None = None
    description: str | None = None
    tips: list[RecipeTip] = Field(default_factory=list)
    raw_transcript: str | None = None
    language_code: str = "en-US"


class RecipePublic(BaseModel):
    """User-facing recipe — no transcript or internal QA fields."""
    id: UUID
    title: str
    ingredients: list[Ingredient]
    ingredient_sections: list[IngredientSection] = Field(default_factory=list)
    steps: list[Step]
    servings: int | None = None
    prep_minutes: int | None = None
    cook_minutes: int | None = None
    tags: list[str] = Field(default_factory=list)
    source_url: str
    platform: Platform
    thumbnail_url: str | None = None
    carousel_image_urls: list[str] = Field(default_factory=list)
    author: str | None = None
    description: str | None = None
    tips: list[RecipeTip] = Field(default_factory=list)
    language_code: str = "en-US"


class RecipeSummary(BaseModel):
    """List row — small payload for home/history."""
    id: UUID
    title: str
    platform: Platform
    source_url: str
    thumbnail_url: str | None = None
    author: str | None = None
    servings: int | None = None
    prep_minutes: int | None = None
    cook_minutes: int | None = None
    saved_at: str
    language_code: str = "en-US"


class ExtractRequest(BaseModel):
    url: HttpUrl
    language: str = "en-US"
    client_delivery_id: UUID | None = None


class JobStatus(str, Enum):
    pending = "pending"
    processing = "processing"
    completed = "completed"
    failed = "failed"


class ExtractJobResponse(BaseModel):
    job_id: UUID
    status: JobStatus
    cache_hit: bool = False
    progress: int = 0
    queued: bool = False
    queue_position: int | None = None


class JobResponse(BaseModel):
    job_id: UUID
    status: JobStatus
    cache_hit: bool = False
    recipe: RecipePublic | None = None
    recipe_id: UUID | None = None
    error: str | None = None
    error_code: str | None = None
    progress: int = 0
    # When a shared base extraction finishes in another language, polling can
    # hand the client the newly-created shared translation job.
    next_job_id: UUID | None = None


class QueuedJobItem(BaseModel):
    job_id: UUID
    status: JobStatus
    progress: int = 0
    source_url: str
    queue_position: int
    created_at: str | None = None
    job_kind: str = "extract"


class QueuedJobsResponse(BaseModel):
    items: list[QueuedJobItem] = Field(default_factory=list)


class MeResponse(BaseModel):
    id: UUID
    display_name: str | None
    is_pro: bool
    pro_expires_at: str | None
    free_used_this_week: int
    # Kept alongside the legacy field while older iOS builds still decode it.
    free_used_this_year: int | None = None
    free_limit: int
    free_remaining: int
    pro_remaining_cents: float | None = None


class SubscriptionRestoreRequest(BaseModel):
    # One current entitlement per configured App Store subscription product.
    # Keep aligned with iOS PricingCatalog and the server product allowlist.
    signed_transactions: list[str] = Field(min_length=1, max_length=11)

    @field_validator("signed_transactions")
    @classmethod
    def _validate_signed_transactions(cls, values: list[str]) -> list[str]:
        if any(not value or len(value) > 16_384 or value.count(".") != 2 for value in values):
            raise ValueError("Invalid signed transaction")
        return list(dict.fromkeys(values))


class SubscriptionRestoreResponse(BaseModel):
    restored: bool
    is_pro: bool
    pro_expires_at: str | None = None


def _normalize_library_snapshot(
    value: dict[str, Any], *, allow_legacy_empty: bool = False
) -> dict[str, Any]:
    snapshot = dict(value)
    if allow_legacy_empty and not snapshot:
        snapshot["schema_version"] = 1
    if type(snapshot.get("schema_version")) is not int or snapshot["schema_version"] != 1:
        raise ValueError("Unsupported library snapshot version")
    snapshot.setdefault("folders", None)
    snapshot.setdefault("shopping_list", [])
    snapshot.setdefault("preferences", None)
    snapshot.setdefault("cooking_progress", {})
    if snapshot["folders"] is not None and not isinstance(snapshot["folders"], dict):
        raise ValueError("Invalid library folder state")
    if not isinstance(snapshot["shopping_list"], list):
        raise ValueError("Invalid shopping list state")
    if snapshot["preferences"] is not None and not isinstance(snapshot["preferences"], dict):
        raise ValueError("Invalid library preferences")
    if not isinstance(snapshot["cooking_progress"], dict):
        raise ValueError("Invalid cooking progress state")

    try:
        encoded = json.dumps(snapshot, ensure_ascii=False, allow_nan=False).encode("utf-8")
    except (TypeError, ValueError, RecursionError):
        raise ValueError("Invalid library snapshot") from None
    if len(encoded) > 262_144:
        raise ValueError("Library snapshot is too large")
    return snapshot


class UserLibraryStateUpdateRequest(BaseModel):
    revision: int = Field(ge=0)
    snapshot: dict[str, Any]

    @field_validator("snapshot")
    @classmethod
    def _validate_library_snapshot(cls, value: dict[str, Any]) -> dict[str, Any]:
        return _normalize_library_snapshot(value)


class UserLibraryStateHistoryEntry(BaseModel):
    revision: int = Field(ge=1)
    snapshot: dict[str, Any]
    updated_at: datetime | None = None

    @field_validator("snapshot")
    @classmethod
    def _normalize_history_snapshot(cls, value: dict[str, Any]) -> dict[str, Any]:
        return _normalize_library_snapshot(value, allow_legacy_empty=True)


class UserLibraryStateResponse(BaseModel):
    revision: int
    snapshot: dict[str, Any]
    history: list[UserLibraryStateHistoryEntry] = Field(default_factory=list)
    updated_at: datetime | None = None

    @field_validator("snapshot")
    @classmethod
    def _normalize_stored_snapshot(cls, value: dict[str, Any]) -> dict[str, Any]:
        return _normalize_library_snapshot(value, allow_legacy_empty=True)

    @field_validator("history", mode="before")
    @classmethod
    def _normalize_stored_history(cls, value: Any) -> Any:
        if value is None:
            return []
        if not isinstance(value, list):
            raise ValueError("Invalid library state history")
        normalized: list[UserLibraryStateHistoryEntry] = []
        for entry in value:
            if not isinstance(entry, dict):
                continue
            try:
                normalized.append(UserLibraryStateHistoryEntry.model_validate(entry))
            except ValidationError:
                # Keep the current snapshot readable if one old history row is
                # malformed; the client treats history as recovery-only data.
                continue
        return normalized


class RecipeListResponse(BaseModel):
    items: list[RecipeSummary]


class AuthAppleRequest(BaseModel):
    identity_token: str
    nonce: str | None = None
    full_name: str | None = None
    authorization_code: str | None = Field(default=None, max_length=2048)


class AuthRefreshRequest(BaseModel):
    refresh_token: str
    request_id: UUID | None = None


class AuthLogoutRequest(BaseModel):
    refresh_token: str
    request_id: UUID | None = None
    push_token: str | None = Field(default=None, min_length=32, max_length=512, pattern=r"^[0-9A-Fa-f]+$")

    @field_validator("push_token")
    @classmethod
    def _push_token_is_byte_aligned(cls, value: str | None) -> str | None:
        if value is not None and len(value) % 2:
            raise ValueError("Invalid push token")
        return value


class PushDeviceRequest(BaseModel):
    token: str = Field(min_length=32, max_length=512, pattern=r"^[0-9A-Fa-f]+$")
    environment: Literal["sandbox", "production"]
    language: str = Field(default="en-US", min_length=2, max_length=20)

    @field_validator("token")
    @classmethod
    def _token_is_byte_aligned(cls, value: str) -> str:
        if len(value) % 2:
            raise ValueError("Invalid push token")
        return value


class PushDeviceDeleteRequest(BaseModel):
    token: str = Field(min_length=32, max_length=512, pattern=r"^[0-9A-Fa-f]+$")

    @field_validator("token")
    @classmethod
    def _token_is_byte_aligned(cls, value: str) -> str:
        if len(value) % 2:
            raise ValueError("Invalid push token")
        return value


class PushDeviceResponse(BaseModel):
    registered: bool
    push_enabled: bool


class AuthUserResponse(BaseModel):
    id: UUID
    email: str | None = None
    display_name: str | None = None
    is_pro: bool = False
    pro_expires_at: str | None = None


class AuthTokenResponse(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    expires_in: int
    user: AuthUserResponse | None = None


class AdminUserCreate(BaseModel):
    email: str
    display_name: str | None = None
    is_pro: bool = False
    password: str | None = None


class AdminUserPatch(BaseModel):
    display_name: str | None = None
    is_pro: bool | None = None
    pro_expires_at: str | None = None
    free_weekly_limit: int | None = None
    pro_monthly_price_cents: int | None = None
    pro_margin_ratio: float | None = None


class HealthResponse(BaseModel):
    status: Literal["ok"] = "ok"


class OkResponse(BaseModel):
    ok: bool = True


class ListResponse(BaseModel):
    items: list[dict[str, Any]]
