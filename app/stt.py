"""Speech-to-text using OpenAI only."""

from __future__ import annotations

import logging
from pathlib import Path

from openai import OpenAI

from app.config import settings
from app.costing import estimate_miss_cost_cents, record_transcription_usage
from app.extract import ExtractError

logger = logging.getLogger(__name__)


class SttBadResult(Exception):
    """OpenAI returned empty or unusable transcript."""


def _lang(language_code: str | None) -> str | None:
    raw = (language_code or "").strip()
    if not raw:
        return None
    return raw.split("-", 1)[0].lower() or None


def rotate_transcript(
    audio_path: Path,
    *,
    duration_seconds: float | None = None,
    language_code: str | None = None,
    local_fn=None,
    is_good=None,
) -> tuple[str, str]:
    """Transcribe audio with OpenAI; never use local or third-party STT."""
    if not settings.openai_api_key:
        raise ExtractError("OPENAI_API_KEY is not configured")

    client = OpenAI(api_key=settings.openai_api_key)
    with audio_path.open("rb") as audio_file:
        kwargs: dict = {
            "model": settings.transcribe_model,
            "file": audio_file,
            "response_format": "json",
        }
        language = _lang(language_code)
        if language:
            kwargs["language"] = language
        result = client.audio.transcriptions.create(**kwargs)

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
        raise SttBadResult("openai empty")
    if is_good is not None and not is_good(text):
        raise SttBadResult("openai rejected_by_quality")
    logger.info("extract stage=stt_ok provider=openai chars=%d", len(text))
    return text, "openai"
