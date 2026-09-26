from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.costing import JobCostMeter, cost_meter_scope, estimate_miss_cost_cents
from app.extract import ExtractError
from app.models import Ingredient, IngredientSection, Platform, Recipe, Step
from app.recipe_builder import RECIPE_INCOMPLETE_ERROR, recipe_is_complete


def _complete_recipe(**overrides) -> Recipe:
    ingredient = Ingredient(name="egg", quantity="4", unit="large")
    recipe = Recipe(
        title="Eggs",
        ingredients=[ingredient],
        ingredient_sections=[IngredientSection(title="Ingredients", ingredients=[ingredient])],
        steps=[Step(order=1, text="Whisk the eggs until foamy")],
        confidence=0.9,
        source_url="https://www.tiktok.com/@cook/video/1",
        platform=Platform.tiktok,
    )
    for key, value in overrides.items():
        setattr(recipe, key, value)
    return recipe


def test_recipe_is_complete_rejects_blocking_gaps_and_bad_order():
    good = _complete_recipe()
    assert recipe_is_complete(good, is_complete=True, blocking_gaps=[])
    # Soft model complaints (servings/times) or is_complete=false alone must not reject.
    assert recipe_is_complete(good, is_complete=False, blocking_gaps=[])
    assert recipe_is_complete(
        good,
        is_complete=False,
        blocking_gaps=["Faltan los minutos de preparación y cocción.", "Falta el número de porciones."],
    )
    assert not recipe_is_complete(good, is_complete=True, blocking_gaps=["missing sauce step"])
    bad_order = _complete_recipe(
        steps=[
            Step(order=2, text="Cook eggs"),
            Step(order=1, text="Crack eggs"),
        ]
    )
    assert not recipe_is_complete(bad_order, is_complete=True, blocking_gaps=[])


def test_complete_description_skips_stt_and_vision(monkeypatch):
    import app.pipeline as pipeline
    from app.extract import MediaInfo

    media = MediaInfo(
        title="Classic omelette",
        description="Ingredients: 3 eggs, 1 tbsp butter. Steps: melt butter, whisk eggs, cook gently.",
        author="cook",
        thumbnail_url=None,
        duration_seconds=40,
        webpage_url="https://www.youtube.com/watch?v=abc",
        subtitles_text="Melt butter. Whisk three eggs. Cook gently until set.",
        audio_path=None,
        media_id="abc",
    )
    built = []
    jobs = []
    settled = []
    recipe_id = uuid4()

    def fake_build(**kwargs):
        built.append(kwargs)
        return _complete_recipe(source_url=kwargs["source_url"], platform=Platform.youtube)

    monkeypatch.setattr(pipeline, "detect_platform", lambda url: Platform.youtube)
    monkeypatch.setattr(pipeline, "fetch_tiktok_slides", lambda url: None)
    monkeypatch.setattr(pipeline, "fetch_media_info", lambda url: media)
    monkeypatch.setattr(pipeline, "youtube_transcript", lambda url: pytest.fail("should use existing captions"))
    monkeypatch.setattr(pipeline, "download_audio", lambda *a, **k: pytest.fail("no audio when complete"))
    monkeypatch.setattr(pipeline, "local_transcript", lambda *a, **k: pytest.fail("no local stt"))
    monkeypatch.setattr(pipeline, "rotate_transcript", lambda *a, **k: pytest.fail("no openai stt"))
    monkeypatch.setattr(pipeline, "download_video_frames", lambda *a, **k: pytest.fail("no vision"))
    monkeypatch.setattr(pipeline, "build_recipe", fake_build)
    monkeypatch.setattr(pipeline, "choose_video_cover_url", lambda *a, **k: None)
    monkeypatch.setattr(pipeline, "update_job", lambda *a, **k: jobs.append(k))
    monkeypatch.setattr(pipeline, "upsert_recipe", lambda *a, **k: {"id": str(recipe_id)})
    monkeypatch.setattr(pipeline, "save_user_recipe", lambda *a, **k: None)
    monkeypatch.setattr(pipeline, "record_usage", lambda **k: None)
    monkeypatch.setattr(pipeline, "settle_spend", lambda **k: settled.append(k))
    monkeypatch.setattr(pipeline, "release_job", lambda *a: None)
    monkeypatch.setattr(pipeline, "_drain_next_extract_for_user", lambda *a, **k: None)

    pipeline.run_extract_job(uuid4(), uuid4(), media.webpage_url, "yt:abc", "en-US")

    assert len(built) == 1
    assert jobs[-1]["status"] == "completed"
    assert settled[-1]["status"] == "settled"


def test_local_stt_success_skips_openai_transcribe_and_vision(monkeypatch, tmp_path):
    import app.pipeline as pipeline
    from app.extract import MediaInfo

    audio = tmp_path / "a.mp3"
    audio.write_bytes(b"audio")
    media = MediaInfo(
        title="Short caption",
        description="watch till end",
        author="cook",
        thumbnail_url=None,
        duration_seconds=30,
        webpage_url="https://www.tiktok.com/@cook/video/2",
        subtitles_text=None,
        audio_path=None,
        media_id="2",
    )
    calls = {"local": 0, "openai": 0, "vision": 0, "build": 0}
    recipe_id = uuid4()

    def fake_build(**kwargs):
        calls["build"] += 1
        if kwargs.get("transcript") and "whisk three eggs" in kwargs["transcript"]:
            return _complete_recipe(source_url=kwargs["source_url"])
        return None

    monkeypatch.setattr(pipeline, "detect_platform", lambda url: Platform.tiktok)
    monkeypatch.setattr(pipeline, "fetch_tiktok_slides", lambda url: None)
    monkeypatch.setattr(pipeline, "fetch_media_info", lambda url: media)
    monkeypatch.setattr(pipeline, "download_audio", lambda *a, **k: audio)

    def fake_local(path):
        calls["local"] += 1
        return "whisk three eggs with butter then cook gently"

    def fake_rotate(path, *, local_fn, **kwargs):
        return local_fn(path), "local"

    def fake_frames(*a, **k):
        calls["vision"] += 1
        raise AssertionError("vision should not run")

    monkeypatch.setattr(pipeline, "rotate_transcript", fake_rotate)
    monkeypatch.setattr(pipeline, "local_transcript", fake_local)
    monkeypatch.setattr(pipeline, "download_video_frames", fake_frames)
    monkeypatch.setattr(pipeline, "build_recipe", fake_build)
    monkeypatch.setattr(pipeline, "choose_video_cover_url", lambda *a, **k: None)
    monkeypatch.setattr(pipeline, "update_job", lambda *a, **k: None)
    monkeypatch.setattr(pipeline, "upsert_recipe", lambda *a, **k: {"id": str(recipe_id)})
    monkeypatch.setattr(pipeline, "save_user_recipe", lambda *a, **k: None)
    monkeypatch.setattr(pipeline, "record_usage", lambda **k: None)
    monkeypatch.setattr(pipeline, "settle_spend", lambda **k: None)
    monkeypatch.setattr(pipeline, "release_job", lambda *a: None)
    monkeypatch.setattr(pipeline, "_drain_next_extract_for_user", lambda *a, **k: None)

    pipeline.run_extract_job(uuid4(), uuid4(), media.webpage_url, "tiktok:2", "en-US")

    assert calls["local"] == 1
    assert calls["openai"] == 0
    assert calls["vision"] == 0
    assert calls["build"] >= 2


def test_openai_stt_used_when_local_empty_then_no_vision_if_complete(monkeypatch, tmp_path):
    import app.pipeline as pipeline
    from app.extract import MediaInfo

    audio = tmp_path / "a.mp3"
    audio.write_bytes(b"audio")
    media = MediaInfo(
        title="Short",
        description="short",
        author="cook",
        thumbnail_url=None,
        duration_seconds=20,
        webpage_url="https://www.youtube.com/watch?v=xyz",
        subtitles_text=None,
        audio_path=None,
        media_id="xyz",
    )
    calls = {"openai": 0, "vision": 0}
    recipe_id = uuid4()

    def fake_build(**kwargs):
        if kwargs.get("transcript") and "fold in cream" in kwargs["transcript"]:
            return _complete_recipe(source_url=kwargs["source_url"], platform=Platform.youtube)
        return None

    monkeypatch.setattr(pipeline, "detect_platform", lambda url: Platform.youtube)
    monkeypatch.setattr(pipeline, "fetch_tiktok_slides", lambda url: None)
    monkeypatch.setattr(pipeline, "fetch_media_info", lambda url: media)
    monkeypatch.setattr(pipeline, "youtube_transcript", lambda url: None)
    monkeypatch.setattr(pipeline, "download_audio", lambda *a, **k: audio)
    monkeypatch.setattr(pipeline, "local_transcript", lambda *a, **k: None)

    def fake_openai(*a, **k):
        calls["openai"] += 1
        return "crack eggs fold in cream cook low heat"

    monkeypatch.setattr(pipeline, "rotate_transcript", lambda *a, **k: (fake_openai(), "openai"))
    monkeypatch.setattr(
        pipeline,
        "download_video_frames",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("no vision")),
    )
    monkeypatch.setattr(pipeline, "build_recipe", fake_build)
    monkeypatch.setattr(pipeline, "choose_video_cover_url", lambda *a, **k: None)
    monkeypatch.setattr(pipeline, "update_job", lambda *a, **k: None)
    monkeypatch.setattr(pipeline, "upsert_recipe", lambda *a, **k: {"id": str(recipe_id)})
    monkeypatch.setattr(pipeline, "save_user_recipe", lambda *a, **k: None)
    monkeypatch.setattr(pipeline, "record_usage", lambda **k: None)
    monkeypatch.setattr(pipeline, "settle_spend", lambda **k: None)
    monkeypatch.setattr(pipeline, "release_job", lambda *a: None)
    monkeypatch.setattr(pipeline, "_drain_next_extract_for_user", lambda *a, **k: None)

    pipeline.run_extract_job(uuid4(), uuid4(), media.webpage_url, "yt:xyz", "en-US")
    assert calls["openai"] == 1


def test_video_cover_replaces_source_thumbnail(monkeypatch):
    import app.pipeline as pipeline
    from app.extract import MediaInfo

    media = MediaInfo(
        title="Classic omelette",
        description="Ingredients: 3 eggs, 1 tbsp butter. Steps: melt butter, whisk eggs, cook gently.",
        author="cook",
        thumbnail_url="https://cdn.example/og.jpg",
        duration_seconds=40,
        webpage_url="https://www.youtube.com/watch?v=cover",
        subtitles_text="Melt butter. Whisk three eggs. Cook gently until set.",
        audio_path=None,
        media_id="abc",
    )
    saved = []
    recipe_id = uuid4()

    monkeypatch.setattr(pipeline, "detect_platform", lambda url: Platform.youtube)
    monkeypatch.setattr(pipeline, "fetch_tiktok_slides", lambda url: None)
    monkeypatch.setattr(pipeline, "fetch_media_info", lambda url: media)
    monkeypatch.setattr(pipeline, "youtube_transcript", lambda url: pytest.fail("captions exist"))
    monkeypatch.setattr(pipeline, "download_audio", lambda *a, **k: pytest.fail("no audio"))
    monkeypatch.setattr(pipeline, "download_video_frames", lambda *a, **k: pytest.fail("no vision"))
    monkeypatch.setattr(pipeline, "build_recipe", lambda **k: _complete_recipe(
        source_url=k["source_url"],
        platform=Platform.youtube,
        thumbnail_url=k["thumbnail_url"],
    ))
    monkeypatch.setattr(
        pipeline,
        "choose_video_cover_url",
        lambda current: "https://cdn.example/best-frame.jpg",
    )
    monkeypatch.setattr(pipeline, "update_job", lambda *a, **k: None)
    monkeypatch.setattr(pipeline, "upsert_recipe", lambda recipe, **k: saved.append(recipe) or {"id": str(recipe_id)})
    monkeypatch.setattr(pipeline, "save_user_recipe", lambda *a, **k: None)
    monkeypatch.setattr(pipeline, "record_usage", lambda **k: None)
    monkeypatch.setattr(pipeline, "settle_spend", lambda **k: None)
    monkeypatch.setattr(pipeline, "release_job", lambda *a: None)
    monkeypatch.setattr(pipeline, "_drain_next_extract_for_user", lambda *a, **k: None)

    pipeline.run_extract_job(uuid4(), uuid4(), media.webpage_url, "yt:cover", "en-US")
    assert saved[0].thumbnail_url == "https://cdn.example/best-frame.jpg"


def test_video_cover_keeps_source_thumbnail_when_selection_fails(monkeypatch):
    import app.pipeline as pipeline
    from app.extract import MediaInfo

    media = MediaInfo(
        title="Classic omelette",
        description="Ingredients: 3 eggs, 1 tbsp butter. Steps: melt butter, whisk eggs, cook gently.",
        author="cook",
        thumbnail_url="https://cdn.example/og.jpg",
        duration_seconds=40,
        webpage_url="https://www.youtube.com/watch?v=cover-miss",
        subtitles_text="Melt butter. Whisk three eggs. Cook gently until set.",
        audio_path=None,
        media_id="abc",
    )
    saved = []
    recipe_id = uuid4()

    monkeypatch.setattr(pipeline, "detect_platform", lambda url: Platform.youtube)
    monkeypatch.setattr(pipeline, "fetch_tiktok_slides", lambda url: None)
    monkeypatch.setattr(pipeline, "fetch_media_info", lambda url: media)
    monkeypatch.setattr(pipeline, "youtube_transcript", lambda url: pytest.fail("captions exist"))
    monkeypatch.setattr(pipeline, "download_audio", lambda *a, **k: pytest.fail("no audio"))
    monkeypatch.setattr(pipeline, "download_video_frames", lambda *a, **k: pytest.fail("no vision"))
    monkeypatch.setattr(pipeline, "build_recipe", lambda **k: _complete_recipe(
        source_url=k["source_url"],
        platform=Platform.youtube,
        thumbnail_url=k["thumbnail_url"],
    ))
    monkeypatch.setattr(pipeline, "choose_video_cover_url", lambda current: None)
    monkeypatch.setattr(pipeline, "update_job", lambda *a, **k: None)
    monkeypatch.setattr(pipeline, "upsert_recipe", lambda recipe, **k: saved.append(recipe) or {"id": str(recipe_id)})
    monkeypatch.setattr(pipeline, "save_user_recipe", lambda *a, **k: None)
    monkeypatch.setattr(pipeline, "record_usage", lambda **k: None)
    monkeypatch.setattr(pipeline, "settle_spend", lambda **k: None)
    monkeypatch.setattr(pipeline, "release_job", lambda *a: None)
    monkeypatch.setattr(pipeline, "_drain_next_extract_for_user", lambda *a, **k: None)

    pipeline.run_extract_job(uuid4(), uuid4(), media.webpage_url, "yt:cover-miss", "en-US")
    assert saved[0].thumbnail_url == "https://cdn.example/og.jpg"


def test_link_in_bio_caption_fails_before_audio(monkeypatch):
    import app.pipeline as pipeline
    from app.extract import MediaInfo
    from app.recipe_builder import LINK_IN_BIO_ERROR

    media = MediaInfo(
        title="Pasta night",
        description="Full recipe link in bio enjoyyyyy",
        author="cook",
        thumbnail_url=None,
        duration_seconds=15,
        webpage_url="https://www.tiktok.com/@cook/video/9",
        subtitles_text=None,
        audio_path=None,
        media_id="9",
    )
    jobs = []

    monkeypatch.setattr(pipeline, "detect_platform", lambda url: Platform.tiktok)
    monkeypatch.setattr(pipeline, "fetch_tiktok_slides", lambda url: None)
    monkeypatch.setattr(pipeline, "fetch_media_info", lambda url: media)
    monkeypatch.setattr(pipeline, "download_audio", lambda *a, **k: pytest.fail("no audio after bio gate"))
    monkeypatch.setattr(pipeline, "update_job", lambda *a, **k: jobs.append(k))
    monkeypatch.setattr(pipeline, "record_usage", lambda **k: None)
    monkeypatch.setattr(pipeline, "settle_spend", lambda **k: None)
    monkeypatch.setattr(pipeline, "release_job", lambda *a: None)
    monkeypatch.setattr(pipeline, "_drain_next_extract_for_user", lambda *a, **k: None)

    pipeline.run_extract_job(uuid4(), uuid4(), media.webpage_url, "tiktok:9", "en-US")

    assert jobs[-1]["status"] == "failed"
    assert jobs[-1]["error"] == LINK_IN_BIO_ERROR


def test_incomplete_after_all_stages_errors_without_upsert(monkeypatch, tmp_path):
    import app.pipeline as pipeline
    from app.extract import MediaInfo, VideoFrames

    audio = tmp_path / "a.mp3"
    audio.write_bytes(b"audio")
    frame_dir = tmp_path / "frames"
    frame_dir.mkdir()
    frame = frame_dir / "1.jpg"
    frame.write_bytes(b"jpeg")
    media = MediaInfo(
        title="Marketing only",
        description="follow for more tips",
        author="cook",
        thumbnail_url=None,
        duration_seconds=15,
        webpage_url="https://www.tiktok.com/@cook/video/9",
        subtitles_text=None,
        audio_path=None,
        media_id="9",
    )
    upserts = []
    jobs = []
    usage = []

    monkeypatch.setattr(pipeline, "detect_platform", lambda url: Platform.tiktok)
    monkeypatch.setattr(pipeline, "fetch_tiktok_slides", lambda url: None)
    monkeypatch.setattr(pipeline, "fetch_media_info", lambda url: media)
    monkeypatch.setattr(pipeline, "download_audio", lambda *a, **k: audio)
    monkeypatch.setattr(pipeline, "local_transcript", lambda *a, **k: None)
    monkeypatch.setattr(pipeline, "rotate_transcript", lambda *a, **k: ("follow for more", "openai"))
    monkeypatch.setattr(
        pipeline,
        "download_video_frames",
        lambda *a, **k: VideoFrames([frame], frame_dir),
    )
    monkeypatch.setattr(pipeline, "ocr_video_frames", lambda *a, **k: "like and subscribe")
    monkeypatch.setattr(pipeline, "build_recipe", lambda **k: None)
    monkeypatch.setattr(pipeline, "update_job", lambda *a, **k: jobs.append(k))
    monkeypatch.setattr(pipeline, "upsert_recipe", lambda *a, **k: upserts.append(1) or {"id": str(uuid4())})
    monkeypatch.setattr(pipeline, "record_usage", lambda **k: usage.append(k))
    monkeypatch.setattr(pipeline, "settle_spend", lambda **k: None)
    monkeypatch.setattr(pipeline, "release_job", lambda *a: None)
    monkeypatch.setattr(pipeline, "_drain_next_extract_for_user", lambda *a, **k: None)

    pipeline.run_extract_job(uuid4(), uuid4(), media.webpage_url, "tiktok:9", "en-US")

    assert not upserts
    assert jobs[-1]["status"] == "failed"
    assert jobs[-1]["error"] == RECIPE_INCOMPLETE_ERROR


def test_failed_job_records_real_usage(monkeypatch):
    import app.pipeline as pipeline
    from app.tiktok_slides import SlideInfo

    slides = SlideInfo("Recipe", "", None, ["https://cdn.example/1.jpg"], total_image_count=1)
    usage = []
    settled = []
    jobs = []

    def fail_ocr(*, image_url, slide_index, on_attempt):
        on_attempt()
        from app.costing import get_cost_meter

        meter = get_cost_meter()
        assert meter is not None
        meter.add_fallback(0.25, kind="ocr")
        raise ExtractError("Incomplete TikTok carousel: unreadable slides 1")

    monkeypatch.setattr(pipeline, "detect_platform", lambda url: Platform.tiktok)
    monkeypatch.setattr(pipeline, "fetch_tiktok_slides", lambda url: slides)
    monkeypatch.setattr(pipeline, "ocr_one_slide", fail_ocr)
    monkeypatch.setattr(pipeline, "build_recipe", lambda **kwargs: None)
    monkeypatch.setattr(pipeline, "update_job", lambda *a, **k: jobs.append(k))
    monkeypatch.setattr(pipeline, "record_usage", lambda **k: usage.append(k))
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

    assert usage and usage[0]["cost_cents"] == 0.25
    assert settled[-1]["actual_cents"] == 0.25
    assert jobs[-1]["cost_cents"] == 0.25


def test_chat_usage_uses_list_price_not_estimate():
    from app.costing import cost_meter_scope

    usage = SimpleNamespace(
        prompt_tokens=1_000_000,
        completion_tokens=1_000_000,
        prompt_tokens_details=SimpleNamespace(cached_tokens=0),
    )
    response = SimpleNamespace(usage=usage)
    with cost_meter_scope() as meter:
        meter.add_chat(response)
    # $0.10 input + $0.50 output = $0.60 = 60 cents
    assert meter.cents == 60.0
    assert meter.cents != estimate_miss_cost_cents()
