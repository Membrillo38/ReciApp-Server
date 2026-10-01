from __future__ import annotations

import logging
import base64
from pathlib import Path
from typing import Callable

from openai import OpenAI
from youtube_transcript_api import YouTubeTranscriptApi
from youtube_transcript_api._errors import (
    NoTranscriptFound,
    TranscriptsDisabled,
    VideoUnavailable,
)

from app.config import settings
from app.costing import estimate_miss_cost_cents, record_chat_usage, record_transcription_usage
from app.extract import ExtractError, MAX_UNIQUE_VISION_FRAMES
from app.platforms import youtube_video_id
from app.tiktok_slides import MAX_CAROUSEL_SLIDES, SlideInfo, download_image_b64

logger = logging.getLogger(__name__)

def youtube_transcript(url: str) -> str | None:
    video_id = youtube_video_id(url)
    if not video_id:
        return None

    languages = ["es", "es-ES", "en", "en-US"]
    try:
        fetched = YouTubeTranscriptApi.get_transcript(video_id, languages=languages)
    except (NoTranscriptFound, TranscriptsDisabled, VideoUnavailable):
        return None
    except Exception:
        return None

    parts = [entry.get("text", "").strip() for entry in fetched if entry.get("text")]
    text = " ".join(parts).strip()
    return text or None


def whisper_transcript(audio_path: Path, *, duration_seconds: float | None = None) -> str:
    """Transcribe audio via OpenAI Transcriptions API (gpt-4o-mini-transcribe)."""
    if not settings.openai_api_key:
        raise ExtractError("OPENAI_API_KEY is not configured")

    client = OpenAI(api_key=settings.openai_api_key)
    with audio_path.open("rb") as audio_file:
        result = client.audio.transcriptions.create(
            model=settings.transcribe_model,
            file=audio_file,
            response_format="json",
        )
    record_transcription_usage(
        result,
        duration_seconds=duration_seconds,
        fallback_cents=estimate_miss_cost_cents(
            duration_seconds=int(duration_seconds) if duration_seconds else None,
            used_transcribe=True,
        )
        - settings.cost_text_cents_per_extract,
    )
    text = (getattr(result, "text", None) or str(result)).strip()
    if not text:
        raise ExtractError("Transcription returned empty text")
    return text


def _ocr_image(
    client: OpenAI,
    *,
    b64: str,
    label: str,
    on_attempt: Callable[[], None] | None,
) -> str:
    if on_attempt:
        on_attempt()
    response = client.chat.completions.create(
        model=settings.vision_model,
        messages=[
            {
                "role": "user",
                "content": [
                    {
                        "type": "text",
                        "text": (
                            f"{label}. Extract all visible recipe text: section headings, "
                            "ingredients, quantities, steps, times, and tips. Preserve headings "
                            "such as component names and storage/reheating tips. Return concise "
                            "plain text only. Skip decorative slogans and engagement text."
                        ),
                    },
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:image/jpeg;base64,{b64}"},
                    },
                ],
            }
        ],
        reasoning_effort="none",
        max_completion_tokens=settings.ocr_slide_max_tokens,
    )
    record_chat_usage(
        response,
        fallback_cents=settings.cost_ocr_cents_per_slide,
        frames=1,
    )
    choices = getattr(response, "choices", None) or []
    if not choices:
        raise ExtractError(f"{label} OCR returned no result")
    choice = choices[0]
    if getattr(choice, "finish_reason", None) == "length":
        raise ExtractError(f"{label} OCR output was truncated")
    message = getattr(choice, "message", None)
    chunk = (getattr(message, "content", None) or "").strip()
    if not chunk:
        raise ExtractError(f"{label} OCR returned empty text")
    return chunk


def ocr_one_slide(
    *,
    image_url: str,
    slide_index: int,
    on_attempt: Callable[[], None] | None = None,
) -> str:
    """OCR a single carousel slide. Raises ExtractError on hard failure."""
    if not settings.openai_api_key:
        raise ExtractError("OPENAI_API_KEY is not configured")
    client = OpenAI(api_key=settings.openai_api_key)
    try:
        b64 = download_image_b64(image_url)
    except Exception as exc:
        logger.warning(
            "extract stage=ocr_slide_download slide_index=%d error_type=%s",
            slide_index,
            type(exc).__name__,
        )
        raise ExtractError(f"Incomplete TikTok carousel: unreadable slides {slide_index}") from exc
    if not b64:
        raise ExtractError(f"Incomplete TikTok carousel: unreadable slides {slide_index}")
    try:
        return _ocr_image(
            client,
            b64=b64,
            label=f"Slide {slide_index}",
            on_attempt=on_attempt,
        )
    except ExtractError:
        raise
    except Exception as exc:
        logger.warning(
            "extract stage=ocr_slide_model slide_index=%d error_type=%s",
            slide_index,
            type(exc).__name__,
        )
        raise ExtractError(f"Incomplete TikTok carousel: unreadable slides {slide_index}") from exc


def ocr_slides(
    slides: SlideInfo,
    max_images: int = MAX_CAROUSEL_SLIDES,
    *,
    on_attempt: Callable[[], None] | None = None,
) -> str:
    if not settings.openai_api_key:
        raise ExtractError("OPENAI_API_KEY is not configured")

    limit = min(max_images, MAX_CAROUSEL_SLIDES)
    if slides.incomplete_reason:
        raise ExtractError(f"Incomplete TikTok carousel: {slides.incomplete_reason}")
    if slides.total_image_count > limit or len(slides.image_urls) > limit:
        raise ExtractError(
            f"Incomplete TikTok carousel: supported limit is {limit} slides"
        )
    parts: list[str] = []
    failures: list[int] = []

    for idx, url in enumerate(slides.image_urls, start=1):
        try:
            parts.append(
                ocr_one_slide(image_url=url, slide_index=idx, on_attempt=on_attempt)
            )
        except ExtractError:
            failures.append(idx)

    if failures:
        indexes = ",".join(str(index) for index in failures)
        raise ExtractError(f"Incomplete TikTok carousel: unreadable slides {indexes}")
    merged = "\n\n".join(parts).strip()
    if not merged:
        raise ExtractError("Could not OCR slideshow images")
    return merged


def _usable_overlay_text(text: str) -> str | None:
    cleaned = (text or "").strip()
    if not cleaned:
        return None
    marker = cleaned.upper().replace("-", "_").replace(" ", "_").strip(".")
    if marker in {"NO_RECIPE_TEXT", "NO_VISIBLE_TEXT", "NONE", "N_A"}:
        return None
    return cleaned


def _ocr_overlay_batch(
    client: OpenAI,
    frame_paths: list[Path],
    *,
    start_index: int,
) -> str:
    content: list[dict] = [
        {
            "type": "text",
            "text": (
                f"Visual frames {start_index}-{start_index + len(frame_paths) - 1} of one cooking video. "
                "Extract ALL recipe-relevant details in chronological order: ingredients, quantities, "
                "units, utensils, actions performed, temperatures, times, step order, and on-screen text. "
                "Copy measurements exactly. Deduplicate identical overlays. "
                "Do not drop an ingredient or action that appears in only one frame. "
                "Skip watermarks, usernames, like/follow prompts, and frames with no recipe content. "
                "Return concise time-ordered plain text notes only — not a finished recipe."
            ),
        }
    ]
    for idx, path in enumerate(frame_paths, start=start_index):
        raw = path.read_bytes()
        if not raw or len(raw) > 2_000_000:
            raise ExtractError(f"Video frame {idx} has an invalid size")
        content.append(
            {
                "type": "image_url",
                "image_url": {"url": f"data:image/jpeg;base64,{base64.b64encode(raw).decode('ascii')}"},
            }
        )
    response = client.chat.completions.create(
        model=settings.vision_model,
        messages=[{"role": "user", "content": content}],
        reasoning_effort="none",
        max_completion_tokens=settings.ocr_overlay_max_tokens,
    )
    record_chat_usage(
        response,
        fallback_cents=len(frame_paths) * settings.cost_ocr_cents_per_slide,
        frames=len(frame_paths),
    )
    choices = getattr(response, "choices", None) or []
    if not choices:
        raise ExtractError("Overlay OCR returned no result")
    choice = choices[0]
    if getattr(choice, "finish_reason", None) == "length":
        raise ExtractError("Overlay OCR output was truncated")
    message = getattr(choice, "message", None)
    chunk = (getattr(message, "content", None) or "").strip()
    if not chunk:
        raise ExtractError("Overlay OCR returned empty text")
    return chunk


def ocr_video_frames(
    frame_paths: list[Path],
    *,
    on_attempt: Callable[[], None] | None = None,
) -> str:
    """OCR vision frames. Empty frames are skipped, not fatal."""
    if not settings.openai_api_key:
        raise ExtractError("OPENAI_API_KEY is not configured")
    if not frame_paths or len(frame_paths) > MAX_UNIQUE_VISION_FRAMES:
        raise ExtractError("Video frame set is invalid or exceeds supported bound")

    client = OpenAI(api_key=settings.openai_api_key)
    parts: list[str] = []
    batch_size = 8
    for start in range(0, len(frame_paths), batch_size):
        batch = frame_paths[start : start + batch_size]
        if on_attempt:
            for _ in batch:
                on_attempt()
        try:
            chunk = _ocr_overlay_batch(client, batch, start_index=start + 1)
        except Exception as exc:
            logger.warning(
                "extract stage=ocr_video_frame frame_index=%d error_type=%s",
                start + 1,
                type(exc).__name__,
            )
            continue
        usable = _usable_overlay_text(chunk)
        if usable:
            parts.append(usable)
    return "\n\n".join(parts)
