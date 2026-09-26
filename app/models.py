from __future__ import annotations

from enum import Enum
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, Field, HttpUrl, field_validator


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
