"""Cheap extract path: skip paid STT/OCR when cheaper stages already work."""

from __future__ import annotations

import re
from uuid import uuid4

import pytest

import app.extract as extract
from app.config import settings
from app.models import Ingredient, IngredientSection, Platform, Recipe, Step
from app.tiktok_slides import SlideInfo


def _complete_recipe(**kwargs) -> Recipe:
    ingredient = Ingredient(name="egg")
    return Recipe(
        title="Eggs",
        ingredients=[ingredient],
        ingredient_sections=[IngredientSection(title="Ingredients", ingredients=[ingredient])],
        steps=[Step(order=1, text="Cook egg")],
        confidence=0.9,
        source_url=kwargs.get("source_url") or "https://example.com",
        platform=Platform.tiktok,
    )


def test_default_margin_is_forty_percent(monkeypatch):
    from app.limits import AppDefaults, resolve_user_limits

    assert AppDefaults().pro_margin_ratio == 0.40
    monkeypatch.setattr(
        "app.limits.get_app_defaults",
        lambda: AppDefaults(pro_margin_ratio=0.40),
    )
    limits = resolve_user_limits(
        {
            "free_weekly_limit": 10,
            "pro_monthly_price_cents": 1000,
            "pro_margin_ratio": None,
        }
    )
    assert limits.pro_margin_ratio == 0.40
    assert limits.pro_budget_cents == 600.0


def test_max_job_cost_default_is_fifty_cents():
    assert settings.max_job_cost_cents == 50.0
    assert settings.max_vision_frames == 8
    assert settings.first_vision_pass_frames == 2
    assert settings.max_concurrent_jobs == 8


def test_rich_captions_skip_audio_and_local_whisper(monkeypatch):
    import app.pipeline as pipeline

    media = extract.MediaInfo(
        title="Pasta night",
        description="Ingredients listed in captions below.",
        author="cook",
        thumbnail_url=None,
        duration_seconds=40,
        webpage_url="https://www.tiktok.com/@cook/video/caption-rich",
        subtitles_text=(
            "Ingredients: pasta, olive oil, garlic, salt. "
            "Steps: boil water, cook pasta, saute garlic, toss and serve hot."
        ),
        audio_path=None,
        media_id="caption-rich",
    )
    audio_calls = []
    settled = []
    recipe_id = uuid4()

    monkeypatch.setattr(pipeline, "detect_platform", lambda url: Platform.tiktok)
    monkeypatch.setattr(pipeline, "fetch_tiktok_slides", lambda url: None)
    monkeypatch.setattr(pipeline, "fetch_media_info", lambda url: media)
    monkeypatch.setattr(
        pipeline,
        "download_audio",
        lambda *a, **k: audio_calls.append(1) or pytest.fail("audio must not download"),
    )
    monkeypatch.setattr(
        pipeline,
        "local_transcript",
        lambda *a, **k: pytest.fail("local whisper must not run"),
    )
    monkeypatch.setattr(
        pipeline,
        "download_video_frames",
        lambda *a, **k: pytest.fail("vision must not run when captions complete"),
    )
    monkeypatch.setattr(pipeline, "build_recipe", lambda **kwargs: _complete_recipe(**kwargs))
    monkeypatch.setattr(pipeline, "choose_video_cover_url", lambda *a, **k: None)
    monkeypatch.setattr(pipeline, "update_job", lambda *a, **k: None)
    monkeypatch.setattr(pipeline, "upsert_recipe", lambda *a, **k: {"id": str(recipe_id)})
    monkeypatch.setattr(pipeline, "save_user_recipe", lambda *a, **k: None)
    monkeypatch.setattr(pipeline, "record_usage", lambda **k: None)
    monkeypatch.setattr(pipeline, "settle_spend", lambda **k: settled.append(k))
    monkeypatch.setattr(pipeline, "release_job", lambda *a: None)
    monkeypatch.setattr(pipeline, "_drain_next_extract_for_user", lambda *a, **k: None)

    pipeline.run_extract_job(uuid4(), uuid4(), media.webpage_url, "tiktok:caption-rich", "en-US")

    assert audio_calls == []
    assert settled[-1]["status"] == "settled"


def test_incomplete_recipe_tries_audio_before_vision(monkeypatch, tmp_path):
    """Incomplete after captions → audio first; vision only if STT still fails."""
    import app.pipeline as pipeline
    from app.extract import VideoFrames

    media = extract.MediaInfo(
        title="Yum",
        description="Watch till the end!",
        author="cook",
        thumbnail_url=None,
        duration_seconds=40,
        webpage_url="https://www.tiktok.com/@cook/video/caption-thin-recipe",
        subtitles_text="A" * 100,
        audio_path=None,
        media_id="caption-vision",
    )
    frame = tmp_path / "f.jpg"
    frame.write_bytes(b"x")
    audio_path = tmp_path / "a.wav"
    audio_path.write_bytes(b"audio")
    audio_calls = []
    vision_calls = []
    settled = []
    recipe_id = uuid4()

    def fake_build(**kwargs):
        if kwargs.get("slide_text"):
            return _complete_recipe(**kwargs)
        return None

    monkeypatch.setattr(pipeline, "detect_platform", lambda url: Platform.tiktok)
    monkeypatch.setattr(pipeline, "fetch_tiktok_slides", lambda url: None)
    monkeypatch.setattr(pipeline, "fetch_media_info", lambda url: media)
    monkeypatch.setattr(pipeline, "download_audio", lambda *a, **k: audio_calls.append(1) or audio_path)
    monkeypatch.setattr(
        pipeline,
        "rotate_transcript",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("stt miss")),
    )
    monkeypatch.setattr(
        pipeline,
        "download_video_frames",
        lambda *a, **k: vision_calls.append(1)
        or VideoFrames(directory=tmp_path, paths=[frame]),
    )
    monkeypatch.setattr(pipeline, "ocr_video_frames", lambda *a, **k: "Ingredients: egg. Steps: cook.")
    monkeypatch.setattr(pipeline, "build_recipe", fake_build)
    monkeypatch.setattr(pipeline, "choose_video_cover_url", lambda *a, **k: None)
    monkeypatch.setattr(pipeline, "update_job", lambda *a, **k: None)
    monkeypatch.setattr(pipeline, "upsert_recipe", lambda *a, **k: {"id": str(recipe_id)})
    monkeypatch.setattr(pipeline, "save_user_recipe", lambda *a, **k: None)
    monkeypatch.setattr(pipeline, "record_usage", lambda **k: None)
    monkeypatch.setattr(pipeline, "settle_spend", lambda **k: settled.append(k))
    monkeypatch.setattr(pipeline, "release_job", lambda *a: None)
    monkeypatch.setattr(pipeline, "_drain_next_extract_for_user", lambda *a, **k: None)

    pipeline.run_extract_job(uuid4(), uuid4(), media.webpage_url, "tiktok:caption-vision", "en-US")

    assert audio_calls == [1]
    assert vision_calls == [1]
    assert settled[-1]["status"] == "settled"


def test_carousel_caption_complete_skips_ocr(monkeypatch):
    import app.pipeline as pipeline

    slides = SlideInfo(
        title="Full pasta recipe",
        description="Ingredients: pasta, salt. Steps: boil water, cook pasta, serve.",
        author="cook",
        image_urls=["https://cdn.example/1.jpg", "https://cdn.example/2.jpg"],
        total_image_count=2,
    )
    ocr_calls = []
    settled = []
    recipe_id = uuid4()

    monkeypatch.setattr(pipeline, "detect_platform", lambda url: Platform.tiktok)
    monkeypatch.setattr(pipeline, "fetch_tiktok_slides", lambda url: slides)
    monkeypatch.setattr(
        pipeline,
        "ocr_one_slide",
        lambda **kwargs: ocr_calls.append(kwargs) or pytest.fail("OCR must not run"),
    )
    monkeypatch.setattr(pipeline, "build_recipe", lambda **kwargs: _complete_recipe(**kwargs))
    monkeypatch.setattr(pipeline, "update_job", lambda *a, **k: None)
    monkeypatch.setattr(pipeline, "upsert_recipe", lambda *a, **k: {"id": str(recipe_id)})
    monkeypatch.setattr(pipeline, "save_user_recipe", lambda *a, **k: None)
    monkeypatch.setattr(pipeline, "record_usage", lambda **k: None)
    monkeypatch.setattr(pipeline, "settle_spend", lambda **k: settled.append(k))
    monkeypatch.setattr(pipeline, "release_job", lambda *a: None)
    monkeypatch.setattr(pipeline, "_drain_next_extract_for_user", lambda *a, **k: None)

    pipeline.run_extract_job(
        uuid4(),
        uuid4(),
        "https://www.tiktok.com/@cook/photo/1",
        "tiktok:photo:1",
        "en-US",
    )

    assert ocr_calls == []
    assert settled[-1]["actual_cents"] == 0


def test_carousel_link_in_bio_skips_ocr(monkeypatch):
    import app.pipeline as pipeline

    slides = SlideInfo(
        title="Yummy",
        description="Full recipe in bio!",
        author="cook",
        image_urls=["https://cdn.example/1.jpg"],
        total_image_count=1,
    )
    jobs = []
    ocr_calls = []
    settled = []

    monkeypatch.setattr(pipeline, "detect_platform", lambda url: Platform.tiktok)
    monkeypatch.setattr(pipeline, "fetch_tiktok_slides", lambda url: slides)
    monkeypatch.setattr(
        pipeline,
        "ocr_one_slide",
        lambda **kwargs: ocr_calls.append(kwargs) or "should not run",
    )
    monkeypatch.setattr(pipeline, "update_job", lambda *a, **k: jobs.append(k))
    monkeypatch.setattr(pipeline, "settle_spend", lambda **k: settled.append(k))
    monkeypatch.setattr(pipeline, "release_job", lambda *a: None)
    monkeypatch.setattr(pipeline, "_drain_next_extract_for_user", lambda *a, **k: None)

    pipeline.run_extract_job(
        uuid4(),
        uuid4(),
        "https://www.tiktok.com/@cook/photo/1",
        "tiktok:photo:bio",
        "en-US",
    )

    assert ocr_calls == []
    assert jobs[-1]["status"] == "failed"
    assert settled[-1]["actual_cents"] == 0


def test_f1_caption_rejects_before_openai_stt_ocr(monkeypatch):
    """Rich non-food title/desc → undetermined without paid stages."""
    import app.pipeline as pipeline
    from app.recipe_builder import RECIPE_UNDETERMINED_ERROR

    media = extract.MediaInfo(
        title="RUSSELL DA POR PERDIDO EL MUNDIAL DE FORMULA 1",
        description="Es realmente imposible? #f1 #formula1 #georgerussell #mercedes",
        author="pablonievest",
        thumbnail_url=None,
        duration_seconds=21,
        webpage_url="https://www.tiktok.com/@pablonievest/video/1",
        subtitles_text="George Russell habla del campeonato y de Mercedes en la F1.",
        audio_path=None,
        media_id="f1",
    )
    jobs = []
    settled = []
    builds = []

    monkeypatch.setattr(pipeline, "detect_platform", lambda url: Platform.tiktok)
    monkeypatch.setattr(pipeline, "fetch_tiktok_slides", lambda url: None)
    monkeypatch.setattr(pipeline, "fetch_media_info", lambda url: media)
    monkeypatch.setattr(
        pipeline,
        "build_recipe",
        lambda **k: builds.append(1) or pytest.fail("OpenAI build must not run"),
    )
    monkeypatch.setattr(
        pipeline,
        "download_audio",
        lambda *a, **k: pytest.fail("STT audio must not run"),
    )
    monkeypatch.setattr(
        pipeline,
        "download_video_frames",
        lambda *a, **k: pytest.fail("OCR vision must not run"),
    )
    monkeypatch.setattr(pipeline, "update_job", lambda *a, **k: jobs.append(k))
    monkeypatch.setattr(pipeline, "record_usage", lambda **k: None)
    monkeypatch.setattr(pipeline, "settle_spend", lambda **k: settled.append(k))
    monkeypatch.setattr(pipeline, "release_job", lambda *a: None)
    monkeypatch.setattr(pipeline, "_drain_next_extract_for_user", lambda *a, **k: None)

    pipeline.run_extract_job(uuid4(), uuid4(), media.webpage_url, "tiktok:f1", "en-US")

    assert builds == []
    assert jobs[-1]["status"] == "failed"
    assert jobs[-1]["error"] == RECIPE_UNDETERMINED_ERROR
    assert settled[-1]["actual_cents"] == 0


def test_undetermined_after_captions_skips_stt_and_vision(monkeypatch):
    """Stage-1 model undetermined with real captions → no STT/OCR escalate."""
    import app.pipeline as pipeline
    from app.recipe_builder import RECIPE_UNDETERMINED_ERROR

    media = extract.MediaInfo(
        title="Quick tip",
        description="Something about cars and racing weekend vibes today",
        author="cook",
        thumbnail_url=None,
        duration_seconds=20,
        webpage_url="https://www.tiktok.com/@x/video/2",
        # Keep under free-gate length without culinary words so stage-1 OpenAI runs.
        subtitles_text="hello",
        audio_path=None,
        media_id="2",
    )
    # Force free gate miss: short combined metadata by monkeypatching helper.
    monkeypatch.setattr(
        pipeline,
        "reject_clearly_non_recipe",
        lambda *a, **k: None,
    )

    def fake_build(**kwargs):
        reasons = kwargs.get("reject_reasons")
        if reasons is not None:
            reasons.clear()
            reasons.append(RECIPE_UNDETERMINED_ERROR)
        return None

    jobs = []
    settled = []

    monkeypatch.setattr(pipeline, "detect_platform", lambda url: Platform.tiktok)
    monkeypatch.setattr(pipeline, "fetch_tiktok_slides", lambda url: None)
    monkeypatch.setattr(pipeline, "fetch_media_info", lambda url: media)
    monkeypatch.setattr(pipeline, "build_recipe", fake_build)
    monkeypatch.setattr(
        pipeline,
        "download_audio",
        lambda *a, **k: pytest.fail("must not STT after undetermined"),
    )
    monkeypatch.setattr(
        pipeline,
        "download_video_frames",
        lambda *a, **k: pytest.fail("must not OCR after undetermined"),
    )
    monkeypatch.setattr(pipeline, "update_job", lambda *a, **k: jobs.append(k))
    monkeypatch.setattr(pipeline, "record_usage", lambda **k: None)
    monkeypatch.setattr(pipeline, "settle_spend", lambda **k: settled.append(k))
    monkeypatch.setattr(pipeline, "release_job", lambda *a: None)
    monkeypatch.setattr(pipeline, "_drain_next_extract_for_user", lambda *a, **k: None)

    pipeline.run_extract_job(uuid4(), uuid4(), media.webpage_url, "tiktok:2", "en-US")

    assert jobs[-1]["status"] == "failed"
    assert jobs[-1]["error"] == RECIPE_UNDETERMINED_ERROR


def test_reject_clearly_non_recipe_unit():
    from app.extract import ExtractError
    from app.recipe_builder import (
        RECIPE_UNDETERMINED_ERROR,
        caption_has_culinary_signal,
        reject_clearly_non_recipe,
    )

    assert not caption_has_culinary_signal(
        "RUSSELL DA POR PERDIDO EL MUNDIAL DE FORMULA 1",
        "#f1 #formula1 #georgerussell",
    )
    assert caption_has_culinary_signal("Pasta night", "Ingredients listed below")
    with pytest.raises(ExtractError, match=re.escape(RECIPE_UNDETERMINED_ERROR)):
        reject_clearly_non_recipe(
            "RUSSELL DA POR PERDIDO EL MUNDIAL DE FORMULA 1",
            "Es realmente imposible? #f1 #formula1 #georgerussell #mercedes",
        )
    # Thin marketing — do not reject (may still be spoken recipe).
    reject_clearly_non_recipe("Yum", "Watch till the end!")


def test_rotate_transcript_completes_recipe(monkeypatch, tmp_path):
    import app.pipeline as pipeline

    audio = tmp_path / "a.wav"
    audio.write_bytes(b"fake")
    media = extract.MediaInfo(
        title="Dinner",
        description="Short",
        author="cook",
        thumbnail_url=None,
        duration_seconds=40,
        webpage_url="https://www.tiktok.com/@cook/video/9",
        subtitles_text=None,
        audio_path=None,
        media_id="9",
    )
    settled = []
    recipe_id = uuid4()
    spoken = "Ingredients: eggs, salt. Steps: scramble and serve hot."

    def fake_build(**kwargs):
        if kwargs.get("transcript") and spoken in (kwargs.get("transcript") or ""):
            return _complete_recipe(**kwargs)
        return None

    monkeypatch.setattr(pipeline, "detect_platform", lambda url: Platform.tiktok)
    monkeypatch.setattr(pipeline, "fetch_tiktok_slides", lambda url: None)
    monkeypatch.setattr(pipeline, "fetch_media_info", lambda url: media)
    monkeypatch.setattr(pipeline, "download_audio", lambda *a, **k: audio)
    monkeypatch.setattr(pipeline, "rotate_transcript", lambda *a, **k: (spoken, "groq"))
    monkeypatch.setattr(
        pipeline,
        "download_video_frames",
        lambda *a, **k: pytest.fail("vision must not run"),
    )
    monkeypatch.setattr(pipeline, "build_recipe", fake_build)
    monkeypatch.setattr(pipeline, "choose_video_cover_url", lambda *a, **k: None)
    monkeypatch.setattr(pipeline, "update_job", lambda *a, **k: None)
    monkeypatch.setattr(pipeline, "upsert_recipe", lambda *a, **k: {"id": str(recipe_id)})
    monkeypatch.setattr(pipeline, "save_user_recipe", lambda *a, **k: None)
    monkeypatch.setattr(pipeline, "record_usage", lambda **k: None)
    monkeypatch.setattr(pipeline, "settle_spend", lambda **k: settled.append(k))
    monkeypatch.setattr(pipeline, "release_job", lambda *a: None)
    monkeypatch.setattr(pipeline, "_drain_next_extract_for_user", lambda *a, **k: None)

    pipeline.run_extract_job(uuid4(), uuid4(), media.webpage_url, "tiktok:9", "en-US")

    assert settled[-1]["status"] == "settled"


def test_recipe_model_stops_after_rate_limit(monkeypatch):
    import app.recipe_builder as recipe_builder

    calls = []

    class RateLimitError(Exception):
        pass

    class FakeCompletions:
        def create(self, **kwargs):
            calls.append(kwargs.get("max_completion_tokens"))
            raise RateLimitError("slow down")

    class FakeClient:
        def __init__(self, **kwargs):
            self.chat = type("C", (), {"completions": FakeCompletions()})()

    monkeypatch.setattr(recipe_builder, "OpenAI", FakeClient)
    monkeypatch.setattr(recipe_builder.settings, "openai_api_key", "test")

    with pytest.raises(extract.ExtractError, match="temporarily unavailable"):
        recipe_builder._request_structured_recipe(
            [{"role": "system", "content": "x"}, {"role": "user", "content": "y"}]
        )

    assert calls == [2400]
