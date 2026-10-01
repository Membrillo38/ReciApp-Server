from __future__ import annotations

import hashlib
import logging
import shutil
from pathlib import Path
from urllib.parse import urlparse
from uuid import UUID

from app.config import settings
from app.costing import JobCostMeter, cost_meter_scope
from app.extract import (
    ExtractError,
    VideoFrames,
    download_audio,
    MediaInfo,
    download_cover_frames,
    download_video_frames,
    fetch_media_info,
    select_best_cover_path,
    select_spread_frame_indexes,
)
from app.db import db_context
from app.models import JobStatus, Platform
from app.platforms import detect_platform
from app.quota import record_usage
from app.job_guard import release as release_job, try_claim as try_claim_job
from app.job_errors import (
    EXTRACTION_RETRYABLE,
    INCOMPLETE_CAROUSEL,
    NO_RECIPE_TEXT,
    RECIPE_NO_INGREDIENTS,
    RECIPE_NO_METHOD,
    SOURCE_BOUND,
    UNSUPPORTED_URL,
    VIDEO_TOO_LONG,
    canonicalize_job_error,
)
from app.localization import normalize_language
from app.spend import settle_spend
from app.recipe_builder import (
    LINK_IN_BIO_ERROR,
    RECIPE_INCOMPLETE_ERROR,
    RECIPE_UNDETERMINED_ERROR,
    build_recipe,
    reject_clearly_non_recipe,
    reject_link_in_bio,
    translate_recipe,
)
from app.store import (
    claim_next_pending_extract,
    claim_next_pending_extract_for_user,
    get_recipe,
    recipe_from_row,
    save_user_recipe,
    update_job,
    upsert_recipe,
    upload_cover_jpeg,
)
from app.translation_cache import (
    recipe_translation_payload,
    source_recipe_fingerprint,
    upsert_recipe_translation,
)
from app.tiktok_slides import SlideInfo, fetch_tiktok_slides
from app.transcript import (
    ocr_one_slide,
    ocr_video_frames,
    youtube_transcript,
)
from app.stt import rotate_transcript

logger = logging.getLogger(__name__)
_RETRYABLE_EXTRACTION_ERROR = EXTRACTION_RETRYABLE
_VISION_BATCH = 4
_COVER_BUCKET_KEY_LEN = 40


def choose_video_cover_url(media: MediaInfo) -> str | None:
    """Best of five frames from the first 2.5 seconds; keep source thumb on failure."""
    frames: VideoFrames | None = None
    try:
        frames = download_cover_frames(
            media.webpage_url,
            media_id=media.media_id,
            duration_seconds=media.duration_seconds,
            play_urls=media.play_urls,
        )
        best = select_best_cover_path(frames.paths, frames.directory)
        if best is None:
            return None
        key = hashlib.sha256(media.webpage_url.encode("utf-8")).hexdigest()[:_COVER_BUCKET_KEY_LEN] + ".jpg"
        return upload_cover_jpeg(best.read_bytes(), key=key)
    except Exception as exc:
        logger.warning("extract stage=cover_frame skipped error_type=%s", type(exc).__name__)
        return None
    finally:
        if frames is not None:
            shutil.rmtree(frames.directory, ignore_errors=True)


def _safe_job_error(error: ExtractError) -> str:
    """Keep user-facing job errors stable and free of provider payloads."""
    message = str(error).strip()
    safe_prefixes = (
        "Unsupported URL.",
        "Video too long (",
        "No usable recipe text found in source",
        RECIPE_UNDETERMINED_ERROR,
        RECIPE_INCOMPLETE_ERROR,
        LINK_IN_BIO_ERROR,
        "Incomplete TikTok carousel:",
        "TikTok video evidence incomplete:",
        "Recipe source text exceeds supported bound",
        "OPENAI_API_KEY is not configured",
        "Recipe model refused",
        "Recipe model returned",
        "Recipe output failed validation",
        "Translated recipe output failed validation",
        "Recipe not found for translation",
        RECIPE_NO_INGREDIENTS,
        RECIPE_NO_METHOD,
    )
    if message.startswith(safe_prefixes) or message in {
        RECIPE_UNDETERMINED_ERROR,
        RECIPE_INCOMPLETE_ERROR,
        LINK_IN_BIO_ERROR,
        RECIPE_NO_INGREDIENTS,
        RECIPE_NO_METHOD,
        UNSUPPORTED_URL,
        VIDEO_TOO_LONG,
        INCOMPLETE_CAROUSEL,
        NO_RECIPE_TEXT,
        SOURCE_BOUND,
        EXTRACTION_RETRYABLE,
    }:
        return canonicalize_job_error(message)[:300]
    return _RETRYABLE_EXTRACTION_ERROR


def run_extract_job(job_id: UUID, user_id: UUID, url: str, url_norm: str, language_code: str) -> None:
    with db_context(actor="service"):
        _run_extract_job(job_id, user_id, url, url_norm, language_code)


def _run_extract_job(job_id: UUID, user_id: UUID, url: str, url_norm: str, language_code: str) -> None:
    if settings.maintenance_mode:
        # Keep the durable job pending; release only the process-local slot.
        # Operators must drain already-running jobs before backup/reset.
        release_job(user_id)
        return
    language_code = normalize_language(language_code)
    audio_path: Path | None = None
    video_frames: VideoFrames | None = None
    slide_count = 0
    frame_count = 0

    with cost_meter_scope() as meter:
        try:
            update_job(job_id, status=JobStatus.processing.value, progress=5)
            platform = detect_platform(url)
            logger.info("extract stage=platform job_id=%s platform=%s", job_id, platform.value)
            update_job(job_id, progress=15)
            if platform == Platform.unknown:
                raise ExtractError(UNSUPPORTED_URL)

            slide_info: SlideInfo | None = None
            if platform == Platform.tiktok:
                slide_info = fetch_tiktok_slides(url)
                logger.info(
                    "extract stage=slides job_id=%s platform=%s slide_count=%d",
                    job_id,
                    platform.value,
                    len(slide_info.image_urls) if slide_info else 0,
                )
                update_job(job_id, progress=30)
                if slide_info is None and "/photo/" in (urlparse(url).path or "").lower():
                    raise ExtractError(INCOMPLETE_CAROUSEL)

            recipe: Recipe | None
            reject_reasons: list[str] = []
            if slide_info and slide_info.image_urls:
                if slide_info.incomplete_reason:
                    raise ExtractError(INCOMPLETE_CAROUSEL)
                # Caption / bio first — never OCR when recipe points off-app.
                reject_link_in_bio(slide_info.title, slide_info.description)
                # Rich non-food caption → fail before OpenAI/OCR.
                reject_clearly_non_recipe(slide_info.title, slide_info.description)

                def record_slide_attempt() -> None:
                    nonlocal slide_count
                    slide_count += 1

                slide_text: str | None = None
                recipe = None
                if any(
                    text and text.strip()
                    for text in (slide_info.title, slide_info.description)
                ):
                    recipe = build_recipe(
                        platform=platform,
                        source_url=url,
                        title=slide_info.title,
                        description=slide_info.description,
                        author=slide_info.author,
                        thumbnail_url=slide_info.image_urls[0],
                        carousel_image_urls=slide_info.image_urls,
                        transcript=None,
                        slide_text=None,
                        language_code=language_code,
                        require_complete=True,
                        reject_reasons=reject_reasons,
                    )

                # OCR last resort: one slide at a time, stop when complete.
                if recipe is None:
                    parts: list[str] = []
                    failures: list[int] = []
                    for idx, image_url in enumerate(slide_info.image_urls, start=1):
                        try:
                            chunk = ocr_one_slide(
                                image_url=image_url,
                                slide_index=idx,
                                on_attempt=record_slide_attempt,
                            )
                        except ExtractError:
                            failures.append(idx)
                            continue
                        if chunk.strip():
                            parts.append(chunk.strip())
                        slide_text = "\n\n".join(parts) if parts else None
                        reject_link_in_bio(
                            slide_info.title, slide_info.description, slide_text
                        )
                        update_job(job_id, progress=min(30 + idx * 5, 80))
                        recipe = build_recipe(
                            platform=platform,
                            source_url=url,
                            title=slide_info.title,
                            description=slide_info.description,
                            author=slide_info.author,
                            thumbnail_url=slide_info.image_urls[0],
                            carousel_image_urls=slide_info.image_urls,
                            transcript=None,
                            slide_text=slide_text,
                            language_code=language_code,
                            require_complete=True,
                            reject_reasons=reject_reasons,
                        )
                        if recipe is not None:
                            break
                    if recipe is None:
                        if failures and not parts:
                            indexes = ",".join(str(i) for i in failures)
                            raise ExtractError(
                                f"Incomplete TikTok carousel: unreadable slides {indexes}"
                            )
                        if failures and parts:
                            raise ExtractError(INCOMPLETE_CAROUSEL)
                        raise ExtractError(
                            reject_reasons[-1] if reject_reasons else RECIPE_INCOMPLETE_ERROR
                        )
                update_job(job_id, progress=60)
            else:
                media = fetch_media_info(url)
                logger.info(
                    "extract stage=media job_id=%s platform=%s duration_seconds=%s subtitles=%s",
                    job_id,
                    platform.value,
                    media.duration_seconds,
                    bool(media.subtitles_text),
                )
                update_job(job_id, progress=30)
                # Abort before STT/vision when caption already says recipe lives in bio.
                reject_link_in_bio(media.title, media.description, media.extra_text)
                duration_seconds = media.duration_seconds
                transcript = media.subtitles_text
                if not transcript and platform == Platform.youtube:
                    transcript = youtube_transcript(url)
                reject_link_in_bio(media.title, media.description, transcript)
                # Title/desc only — thin marketing still escalates; free subs stay for stage 1.
                reject_clearly_non_recipe(media.title, media.description, media.extra_text)
                video_text = media.extra_text or ""

                def try_build(current_transcript: str | None, current_visual: str | None) -> Recipe | None:
                    if not any(
                        text and text.strip()
                        for text in (
                            current_transcript,
                            media.title,
                            media.description,
                            current_visual or "",
                        )
                    ):
                        return None
                    return build_recipe(
                        platform=platform,
                        source_url=media.webpage_url,
                        title=media.title,
                        description=media.description,
                        author=media.author,
                        thumbnail_url=media.thumbnail_url,
                        transcript=current_transcript,
                        slide_text=current_visual or None,
                        language_code=language_code,
                        require_complete=True,
                        reject_reasons=reject_reasons,
                    )

                # Stage 1: description / metadata / captions only.
                recipe = try_build(transcript, video_text)
                update_job(job_id, progress=45)

                # Model already said "not a recipe" with real caption/subs — stop.
                # Incomplete/missing-ingredients still escalate (more evidence may help).
                if (
                    recipe is None
                    and reject_reasons
                    and reject_reasons[-1] == RECIPE_UNDETERMINED_ERROR
                ):
                    raise ExtractError(RECIPE_UNDETERMINED_ERROR)

                # Stage 2–3: OpenAI STT only.
                # Audio only when recipe still incomplete.
                if recipe is None:
                    try:
                        audio_path = download_audio(media.webpage_url, media.media_id)
                    except ExtractError as exc:
                        logger.warning(
                            "extract stage=audio_fallback job_id=%s error_type=%s",
                            job_id,
                            type(exc).__name__,
                        )
                        audio_path = None

                    if audio_path is not None:
                        try:
                            spoken, provider = rotate_transcript(
                                audio_path,
                                duration_seconds=float(duration_seconds)
                                if duration_seconds
                                else None,
                                language_code=language_code,
                            )
                            transcript = _merge_evidence(transcript, spoken)
                            recipe = try_build(transcript, video_text)
                            logger.info(
                                "extract stage=stt_transcribe job_id=%s provider=%s chars=%d",
                                job_id,
                                provider,
                                len(spoken),
                            )
                        except Exception as exc:
                            logger.warning(
                                "extract stage=stt_fallback job_id=%s platform=%s error_type=%s",
                                job_id,
                                platform.value,
                                type(exc).__name__,
                            )

                update_job(job_id, progress=60)

                # Stage 4: visual OCR last resort only.
                if recipe is None:
                    reject_link_in_bio(media.title, media.description, transcript, video_text)
                    remaining_budget = max(settings.max_job_cost_cents - meter.cents, 0.0)
                    max_frames = max(
                        1,
                        min(
                            int(remaining_budget // max(settings.cost_ocr_cents_per_slide, 0.01)),
                            settings.max_vision_frames,
                        ),
                    )
                    try:
                        video_frames = download_video_frames(
                            media.webpage_url,
                            media_id=media.media_id,
                            duration_seconds=duration_seconds,
                            play_urls=media.play_urls,
                            max_unique_frames=max_frames,
                        )
                        recipe, frame_count, video_text = _vision_incremental_build(
                            frame_paths=video_frames.paths,
                            try_build=try_build,
                            transcript=transcript,
                            video_text=video_text,
                            meter=meter,
                        )
                    except ExtractError as exc:
                        logger.warning(
                            "extract stage=frame_fallback job_id=%s error_type=%s",
                            job_id,
                            type(exc).__name__,
                        )

                if recipe is None:
                    if not transcript and not any(
                        text.strip() for text in (media.title, media.description, video_text or "")
                    ):
                        raise ExtractError(RECIPE_UNDETERMINED_ERROR)
                    raise ExtractError(reject_reasons[-1] if reject_reasons else RECIPE_INCOMPLETE_ERROR)

                cover_url = choose_video_cover_url(media)
                if cover_url:
                    recipe.thumbnail_url = cover_url

            update_job(job_id, progress=85)
            cost = round(meter.cents, 4)
            row = upsert_recipe(recipe, source_url_norm=url_norm, language_code=language_code)
            recipe_id = row["id"] if isinstance(row["id"], UUID) else UUID(str(row["id"]))
            logger.info(
                "extract stage=persisted job_id=%s platform=%s recipe_id=%s slide_count=%d frames=%d cost_cents=%s",
                job_id,
                platform.value,
                recipe_id,
                slide_count,
                frame_count,
                cost,
            )
            save_user_recipe(user_id, recipe_id)
            update_job(
                job_id,
                status=JobStatus.completed.value,
                progress=100,
                lease_until=None,
                recipe_id=recipe_id,
                cost_cents=cost,
                cache_hit=False,
            )
            try:
                record_usage(
                    user_id=user_id,
                    kind="extract_miss",
                    cost_cents=cost,
                    recipe_id=recipe_id,
                    job_id=job_id,
                )
            except Exception:
                pass
            settle_spend(job_id=job_id, actual_cents=cost, status="settled")
        except ExtractError as exc:
            logger.warning(
                "extract failed job_id=%s error_type=%s",
                job_id,
                type(exc).__name__,
            )
            cost = round(meter.cents, 4) if meter.openai_called else 0.0
            _mark_job_failed(job_id, _safe_job_error(exc), cost_cents=cost)
            if meter.openai_called and cost > 0:
                try:
                    record_usage(
                        user_id=user_id,
                        kind="extract_miss",
                        cost_cents=cost,
                        recipe_id=None,
                        job_id=job_id,
                    )
                except Exception:
                    pass
            settle_spend(job_id=job_id, actual_cents=cost, status="failed")
        except Exception as exc:
            logger.error(
                "extract failed job_id=%s error_type=%s",
                job_id,
                type(exc).__name__,
            )
            cost = round(meter.cents, 4) if meter.openai_called else 0.0
            _mark_job_failed(job_id, _RETRYABLE_EXTRACTION_ERROR, cost_cents=cost)
            if meter.openai_called and cost > 0:
                try:
                    record_usage(
                        user_id=user_id,
                        kind="extract_miss",
                        cost_cents=cost,
                        recipe_id=None,
                        job_id=job_id,
                    )
                except Exception:
                    pass
            settle_spend(job_id=job_id, actual_cents=cost, status="failed")
        finally:
            release_job(user_id)
            if audio_path:
                shutil.rmtree(audio_path.parent, ignore_errors=True)
            if video_frames:
                shutil.rmtree(video_frames.directory, ignore_errors=True)
            _drain_next_extract_for_user(user_id)


def _vision_incremental_build(
    *,
    frame_paths: list[Path],
    try_build,
    transcript: str | None,
    video_text: str,
    meter: JobCostMeter,
) -> tuple[Recipe | None, int, str]:
    """Analyze spread frames first, then chronological leftovers until complete."""
    if not frame_paths:
        return None, 0, video_text

    analyzed = 0
    notes: list[str] = []
    remaining_budget = max(settings.max_job_cost_cents - meter.cents, 0.0)
    max_affordable = max(
        1,
        int(remaining_budget // max(settings.cost_ocr_cents_per_slide, 0.01)),
    )
    paths = frame_paths[:max_affordable]
    first_n = max(1, int(settings.first_vision_pass_frames))
    first_indexes = select_spread_frame_indexes(len(paths), min(first_n, len(paths)))
    first_paths = [paths[i] for i in first_indexes]
    analyzed_set = set(first_indexes)

    def run_batch(batch: list[Path]) -> None:
        nonlocal analyzed, video_text
        if not batch:
            return
        if meter.cents + len(batch) * settings.cost_ocr_cents_per_slide > settings.max_job_cost_cents:
            return

        def on_attempt() -> None:
            nonlocal analyzed
            analyzed += 1

        chunk = ocr_video_frames(batch, on_attempt=on_attempt)
        if chunk:
            notes.append(chunk)
            video_text = _merge_evidence(video_text, chunk) or ""

    run_batch(first_paths)
    recipe = try_build(transcript, video_text or None)
    if recipe is not None:
        return recipe, analyzed, video_text

    leftover = [paths[i] for i in range(len(paths)) if i not in analyzed_set]
    for start in range(0, len(leftover), _VISION_BATCH):
        batch = leftover[start : start + _VISION_BATCH]
        run_batch(batch)
        recipe = try_build(transcript, video_text or None)
        if recipe is not None:
            return recipe, analyzed, video_text
    return None, analyzed, video_text


def _merge_evidence(*parts: str | None) -> str | None:
    cleaned = [part.strip() for part in parts if part and part.strip()]
    if not cleaned:
        return None
    # Prefer later evidence last so the model sees newest additions.
    merged: list[str] = []
    seen: set[str] = set()
    for part in cleaned:
        key = part.lower()
        if key in seen:
            continue
        seen.add(key)
        merged.append(part)
    return "\n\n".join(merged)


def _drain_next_extract_for_user(user_id: UUID) -> None:
    """Start oldest pending for this user; if none, kick oldest global pending."""
    if settings.maintenance_mode or settings.worker_enabled:
        return
    if try_claim_job(user_id):
        row = claim_next_pending_extract_for_user(user_id)
        if row:
            _run_claimed_pending(row, user_id)
            return
        release_job(user_id)

    row = claim_next_pending_extract()
    if not row:
        return
    owner = UUID(str(row["user_id"]))
    if not try_claim_job(owner):
        update_job(UUID(str(row["id"])), status=JobStatus.pending.value, progress=0)
        return
    _run_claimed_pending(row, owner)


def _run_claimed_pending(row: dict, user_id: UUID) -> None:
    try:
        run_extract_job(
            UUID(str(row["id"])),
            user_id,
            str(row["source_url_raw"]),
            str(row["source_url_norm"]),
            str(row.get("language_code") or "en-US"),
        )
    except Exception:
        logger.exception(
            "drain next extract failed user_id=%s",
            user_id,
        )


def _mark_job_failed(job_id: UUID, error: str, *, cost_cents: float = 0.0) -> None:
    try:
        update_job(
            job_id,
            status=JobStatus.failed.value,
            progress=0,
            lease_until=None,
            error=canonicalize_job_error(error)[:1000],
            cost_cents=cost_cents,
        )
    except Exception:
        # Preserve original extraction failure; the job may be recovered by TTL.
        pass


def run_translation_job(job_id: UUID, user_id: UUID, recipe_id: UUID, language_code: str) -> None:
    with db_context(actor="service"):
        _run_translation_job(job_id, user_id, recipe_id, language_code)


def _run_translation_job(job_id: UUID, user_id: UUID, recipe_id: UUID, language_code: str) -> None:
    if settings.maintenance_mode:
        # Keep the durable job pending; release only the process-local slot.
        # Operators must drain already-running jobs before backup/reset.
        release_job(user_id)
        return
    language_code = normalize_language(language_code)
    with cost_meter_scope() as meter:
        try:
            update_job(job_id, status=JobStatus.processing.value, progress=10)
            row = get_recipe(recipe_id)
            if not row:
                raise ExtractError("Recipe not found for translation")
            base_recipe = recipe_from_row(row)
            source_fingerprint = source_recipe_fingerprint(row)
            if normalize_language(row.get("language_code")) == language_code:
                payload = recipe_translation_payload(base_recipe)
            else:
                translated = translate_recipe(base_recipe, language_code)
                payload = recipe_translation_payload(translated)
            cost = round(meter.cents, 4)
            update_job(job_id, progress=75)
            upsert_recipe_translation(
                recipe_id,
                language_code,
                payload,
                source_fingerprint=source_fingerprint,
            )
            save_user_recipe(user_id, recipe_id)
            update_job(
                job_id,
                status=JobStatus.completed.value,
                progress=100,
                lease_until=None,
                recipe_id=recipe_id,
                cost_cents=cost if meter.openai_called else 0,
                cache_hit=not meter.openai_called,
            )
            if meter.openai_called:
                try:
                    record_usage(
                        user_id=user_id,
                        kind="extract_miss",
                        cost_cents=cost,
                        recipe_id=recipe_id,
                        job_id=job_id,
                    )
                except Exception:
                    pass
            settle_spend(
                job_id=job_id,
                actual_cents=cost if meter.openai_called else 0,
                status="settled",
            )
        except ExtractError as exc:
            logger.warning(
                "translation failed job_id=%s error_type=%s",
                job_id,
                type(exc).__name__,
            )
            cost = round(meter.cents, 4) if meter.openai_called else 0.0
            _mark_job_failed(job_id, _safe_job_error(exc), cost_cents=cost)
            settle_spend(job_id=job_id, actual_cents=cost, status="failed")
        except Exception as exc:
            logger.error(
                "translation failed job_id=%s error_type=%s",
                job_id,
                type(exc).__name__,
            )
            cost = round(meter.cents, 4) if meter.openai_called else 0.0
            _mark_job_failed(job_id, _RETRYABLE_EXTRACTION_ERROR, cost_cents=cost)
            settle_spend(job_id=job_id, actual_cents=cost, status="failed")
        finally:
            release_job(user_id)
