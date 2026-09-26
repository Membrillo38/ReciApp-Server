import json
from types import SimpleNamespace
from pathlib import Path

import pytest

import app.recipe_builder as recipe_builder
import app.extract as extract
from app.costing import estimate_miss_cost_cents
from app.config import settings
from app.pipeline import _safe_job_error
from app.models import Platform
from app.tiktok_slides import (
    MAX_CAROUSEL_SLIDES,
    SlideInfo,
    _dig_item_struct,
    _image_urls,
    _slide_info_from_html,
    _photo_meta_fallback,
    fetch_tiktok_slides,
)


def test_tiktok_urls_are_bounded_deduplicated_and_use_largest_variant():
    images = [
        {"imageURL": {"urlList": ["https://cdn.example/1-small.jpg", "https://cdn.example/1-large.jpg"]}},
        {"imageURL": {"urlList": ["https://cdn.example/1-large.jpg", "https://cdn.example/2-large.jpg"]}},
    ]
    assert _image_urls(images) == [
        "https://cdn.example/1-large.jpg",
        "https://cdn.example/2-large.jpg",
    ]


def test_tiktok_photo_meta_fallback_preserves_public_image_order():
    html = '''
    <meta property="og:title" content="Pasta carousel">
    <meta property="og:description" content="Boil pasta">
    <meta property="og:image" content="https://cdn.example/slide-1.jpg">
    <meta property="og:image" content="https://cdn.example/slide-2.jpg">
    <meta name="twitter:image" content="https://cdn.example/slide-2.jpg">
    '''
    result = _photo_meta_fallback(
        html,
        "https://www.tiktok.com/@cook/photo/1",
    )
    assert result is not None
    assert result.image_urls == [
        "https://cdn.example/slide-1.jpg",
        "https://cdn.example/slide-2.jpg",
    ]
    assert result.incomplete_reason is not None


def test_tiktok_hydration_fallback_finds_nested_photo_payload():
    result = _dig_item_struct({
        "newRoot": {
            "item": {
                "imagePost": {
                    "images": [{"imageURL": {"urlList": ["https://cdn.example/slide.jpg"]}}]
                }
            }
        }
    })
    assert result and "imagePost" in result


def test_photo_post_retries_mobile_ssr_when_desktop_shell_has_no_images():
    mobile_html = (
        '<script id="__UNIVERSAL_DATA_FOR_REHYDRATION__">'
        + json.dumps({
            "__DEFAULT_SCOPE__": {
                "webapp.video-detail": {
                    "itemInfo": {
                        "itemStruct": {
                            "desc": "High protein pasta",
                            "author": {"uniqueId": "cook"},
                            "imagePost": {
                                "title": "High protein pasta",
                                "images": [
                                    {"imageURL": {"urlList": [f"https://cdn.example/slide-{i}.jpg"]}}
                                    for i in range(1, 4)
                                ],
                            },
                        }
                    }
                }
            }
        })
        + '</script>'
    )
    original_fetch = __import__("app.tiktok_slides", fromlist=["_fetch_html"])._fetch_html
    import app.tiktok_slides as slides
    calls = []

    def fake_fetch(url, *, user_agent=slides.UA):
        calls.append(user_agent)
        return mobile_html if len(calls) == 2 else "<html>desktop shell</html>"

    slides._fetch_html = fake_fetch
    try:
        result = fetch_tiktok_slides("https://www.tiktok.com/@cook/photo/1")
    finally:
        slides._fetch_html = original_fetch

    assert result is not None
    assert result.image_urls == [
        "https://cdn.example/slide-1.jpg",
        "https://cdn.example/slide-2.jpg",
        "https://cdn.example/slide-3.jpg",
    ]
    assert calls == [slides.MOBILE_UA, slides.UA]


def test_photo_post_retries_mobile_ssr_after_empty_first_fetch():
    import app.tiktok_slides as slides

    mobile_html = (
        '<script id="__UNIVERSAL_DATA_FOR_REHYDRATION__">'
        + json.dumps({
            "__DEFAULT_SCOPE__": {
                "webapp.video-detail": {
                    "itemInfo": {
                        "itemStruct": {
                            "desc": "Photo recipe",
                            "imagePost": {
                                "images": [{"imageURL": {"urlList": ["https://cdn.example/slide.jpg"]}}]
                            },
                        }
                    }
                }
            }
        })
        + '</script>'
    )
    original_fetch = slides._fetch_html
    calls = []

    def fake_fetch(url, *, user_agent=slides.UA):
        calls.append(user_agent)
        return mobile_html if len(calls) == 2 else None

    slides._fetch_html = fake_fetch
    try:
        result = fetch_tiktok_slides("https://www.tiktok.com/@cook/photo/2")
    finally:
        slides._fetch_html = original_fetch

    assert result is not None
    assert result.image_urls == ["https://cdn.example/slide.jpg"]
    assert calls == [slides.MOBILE_UA, slides.UA]


def test_video_url_skips_carousel_probe_and_leaves_media_to_ytdlp():
    import app.tiktok_slides as slides

    original_fetch = slides._fetch_html
    slides._fetch_html = lambda *args, **kwargs: (_ for _ in ()).throw(
        AssertionError("video must not probe carousel HTML")
    )
    try:
        result = fetch_tiktok_slides("https://www.tiktok.com/@cook/video/3")
    finally:
        slides._fetch_html = original_fetch

    assert result is None


def test_tiktok_html_retries_one_transient_fetch_failure():
    import app.tiktok_slides as slides

    calls = []
    original_open = slides.safe_urlopen_limited
    original_validate = slides.validate_public_url

    def fake_open(request, *, timeout, max_bytes):
        calls.append((request.full_url, timeout))
        if len(calls) == 1:
            raise TimeoutError("temporary")
        return b"<html>ok</html>"

    slides.safe_urlopen_limited = fake_open
    slides.validate_public_url = lambda *args, **kwargs: None
    try:
        assert slides._fetch_html("https://www.tiktok.com/@cook/photo/1") == "<html>ok</html>"
    finally:
        slides.safe_urlopen_limited = original_open
        slides.validate_public_url = original_validate

    assert len(calls) == 2


def test_tiktok_slide_download_retries_one_transient_fetch_failure():
    import app.tiktok_slides as slides

    calls = []
    original_open = slides.safe_urlopen_limited
    original_validate = slides.validate_public_url

    def fake_open(request, *, timeout, max_bytes):
        calls.append((request.full_url, timeout))
        if len(calls) == 1:
            raise TimeoutError("temporary")
        return b"x" * 500

    slides.safe_urlopen_limited = fake_open
    slides.validate_public_url = lambda *args, **kwargs: None
    try:
        result = slides.download_image_b64("https://cdn.example/slide.jpg")
    finally:
        slides.safe_urlopen_limited = original_open
        slides.validate_public_url = original_validate

    assert result
    assert len(calls) == 2


def test_ocr_processes_all_bounded_slides_then_rejects_any_unreadable_slide():
    import app.transcript as transcript
    from app.tiktok_slides import MAX_CAROUSEL_SLIDES, SlideInfo

    requested: list[str] = []
    model_calls: list[int] = []

    def fake_download(url):
        requested.append(url)
        if url.endswith("slide-2.jpg"):
            raise RuntimeError("temporary CDN failure")
        return "ZmFrZQ=="

    class FakeCompletions:
        def create(self, **kwargs):
            model_calls.append(kwargs["messages"][0]["content"][0]["text"])
            if len(model_calls) == 3:
                return SimpleNamespace(choices=[])
            return SimpleNamespace(
                choices=[SimpleNamespace(message=SimpleNamespace(content="ingredient"))]
            )

    class FakeClient:
        def __init__(self, **kwargs):
            self.chat = SimpleNamespace(completions=FakeCompletions())

    original_download = transcript.download_image_b64
    original_client = transcript.OpenAI
    original_key = transcript.settings.openai_api_key
    transcript.download_image_b64 = fake_download
    transcript.OpenAI = FakeClient
    transcript.settings.openai_api_key = "test-key"
    try:
        with pytest.raises(extract.ExtractError, match="unreadable slides 2,4"):
            transcript.ocr_slides(
                SlideInfo(
                    title="Carousel",
                    description="",
                    author=None,
                    image_urls=[f"https://cdn.example/slide-{i}.jpg" for i in range(1, 13)],
                    total_image_count=MAX_CAROUSEL_SLIDES,
                )
            )
    finally:
        transcript.download_image_b64 = original_download
        transcript.OpenAI = original_client
        transcript.settings.openai_api_key = original_key

    assert len(requested) == MAX_CAROUSEL_SLIDES
    assert len(model_calls) == MAX_CAROUSEL_SLIDES - 1
    assert len(model_calls) == MAX_CAROUSEL_SLIDES - 1


def test_hydration_marks_carousel_over_bound_incomplete_without_hiding_count():
    html = (
        '<script id="SIGI_STATE">'
        + json.dumps({
            "ItemModule": {
                "1": {
                    "desc": "Recipe",
                    "imagePost": {
                        "images": [
                            {"imageURL": {"urlList": [f"https://cdn.example/{i}.jpg"]}}
                            for i in range(MAX_CAROUSEL_SLIDES + 1)
                        ]
                    },
                }
            }
        })
        + "</script>"
    )
    result = _slide_info_from_html(html)
    assert result is not None
    assert len(result.image_urls) == MAX_CAROUSEL_SLIDES
    assert result.total_image_count == MAX_CAROUSEL_SLIDES + 1
    assert result.incomplete_reason is not None


def test_hydration_marks_invalid_slide_reference_incomplete():
    html = (
        '<script id="SIGI_STATE">'
        + json.dumps({
            "ItemModule": {
                "1": {
                    "imagePost": {
                        "images": [
                            {"imageURL": {"urlList": ["https://cdn.example/1.jpg"]}},
                            {"imageURL": {"urlList": ["http://private.invalid/2.jpg"]}},
                        ]
                    }
                }
            }
        })
        + "</script>"
    )
    result = _slide_info_from_html(html)
    assert result is not None
    assert result.total_image_count == 2
    assert len(result.image_urls) == 1
    assert result.incomplete_reason is not None


def test_ocr_rejects_truncated_model_output():
    import app.transcript as transcript

    class FakeCompletions:
        def create(self, **kwargs):
            return SimpleNamespace(
                choices=[SimpleNamespace(
                    message=SimpleNamespace(content="partial"),
                    finish_reason="length",
                )]
            )

    class FakeClient:
        def __init__(self, **kwargs):
            self.chat = SimpleNamespace(completions=FakeCompletions())

    original_client = transcript.OpenAI
    original_download = transcript.download_image_b64
    original_key = transcript.settings.openai_api_key
    transcript.OpenAI = FakeClient
    transcript.download_image_b64 = lambda url: "ZmFrZQ=="
    transcript.settings.openai_api_key = "test-key"
    try:
        with pytest.raises(extract.ExtractError, match="unreadable slides 1"):
            transcript.ocr_slides(SlideInfo("x", "", None, ["https://cdn.example/1.jpg"], total_image_count=1))
    finally:
        transcript.OpenAI = original_client
        transcript.download_image_b64 = original_download
        transcript.settings.openai_api_key = original_key


def test_carousel_cost_accounts_for_all_bounded_ocr_slides():
    from app.tiktok_slides import MAX_CAROUSEL_SLIDES

    expected = round(
        settings.cost_text_cents_per_extract
        + MAX_CAROUSEL_SLIDES * settings.cost_ocr_cents_per_slide,
        4,
    )
    assert estimate_miss_cost_cents(slide_count=MAX_CAROUSEL_SLIDES) == expected


def test_video_frame_cost_is_bounded_to_overlay_attempts():
    expected = round(
        settings.cost_text_cents_per_extract
        + 80 * settings.cost_ocr_cents_per_slide,
        4,
    )
    assert estimate_miss_cost_cents(frame_count=80) == expected


def test_structured_output_retries_malformed_json():
    calls = []

    class FakeCompletions:
        def create(self, **kwargs):
            calls.append(kwargs)
            content = '{"title":"cut' if len(calls) == 1 else '{"title":"ok"}'
            return SimpleNamespace(
                choices=[
                    SimpleNamespace(
                        message=SimpleNamespace(content=content, refusal=None),
                        finish_reason="stop",
                    )
                ]
            )

    class FakeClient:
        def __init__(self, **kwargs):
            self.chat = SimpleNamespace(completions=FakeCompletions())

    original = recipe_builder.OpenAI
    recipe_builder.OpenAI = FakeClient
    try:
        result = recipe_builder._request_structured_recipe(
            [{"role": "system", "content": "x"}, {"role": "user", "content": "y"}]
        )
    finally:
        recipe_builder.OpenAI = original

    assert result["title"] == "ok"
    assert len(calls) == 2
    assert calls[0]["max_completion_tokens"] == 2400
    assert calls[1]["max_completion_tokens"] == 4000


def test_structured_output_retries_transient_provider_error():
    calls = []

    class FakeCompletions:
        def create(self, **kwargs):
            calls.append(kwargs)
            if len(calls) == 1:
                raise RuntimeError("temporary provider failure")
            return SimpleNamespace(
                choices=[
                    SimpleNamespace(
                        message=SimpleNamespace(content='{"title":"ok"}', refusal=None),
                        finish_reason="stop",
                    )
                ]
            )

    class FakeClient:
        def __init__(self, **kwargs):
            self.chat = SimpleNamespace(completions=FakeCompletions())

    original = recipe_builder.OpenAI
    recipe_builder.OpenAI = FakeClient
    try:
        result = recipe_builder._request_structured_recipe(
            [{"role": "system", "content": "x"}, {"role": "user", "content": "y"}]
        )
    finally:
        recipe_builder.OpenAI = original

    assert result["title"] == "ok"
    assert len(calls) == 2


def test_structured_output_retries_empty_model_response():
    calls = []

    class FakeCompletions:
        def create(self, **kwargs):
            calls.append(kwargs)
            if len(calls) == 1:
                return SimpleNamespace(choices=[])
            return SimpleNamespace(
                choices=[
                    SimpleNamespace(
                        message=SimpleNamespace(content='{"title":"ok"}', refusal=None),
                        finish_reason="stop",
                    )
                ]
            )

    class FakeClient:
        def __init__(self, **kwargs):
            self.chat = SimpleNamespace(completions=FakeCompletions())

    original = recipe_builder.OpenAI
    recipe_builder.OpenAI = FakeClient
    try:
        result = recipe_builder._request_structured_recipe(
            [{"role": "system", "content": "x"}, {"role": "user", "content": "y"}]
        )
    finally:
        recipe_builder.OpenAI = original

    assert result["title"] == "ok"
    assert len(calls) == 2


def test_pipeline_logs_stage_without_source_payload_logging():
    source = Path("app/pipeline.py").read_text(encoding="utf-8")
    assert 'extract stage=media' in source
    assert 'extract stage=ocr_video_frame' not in source
    assert 'extract stage=stt_fallback' in source
    assert 'extract stage=persisted' in source
    assert 'logger.warning("recipe_model attempt=%d error_type=%s"' not in source
    assert 'f"Unexpected error: {exc}"' not in source
    assert "_RETRYABLE_EXTRACTION_ERROR" in source


def test_video_frame_ocr_preserves_order_and_counts_attempts(tmp_path):
    import app.transcript as transcript

    frames = []
    for index in range(1, 4):
        path = tmp_path / f"frame-{index}.jpg"
        path.write_bytes(f"frame-{index}".encode())
        frames.append(path)
    attempts = []

    class FakeCompletions:
        def create(self, **kwargs):
            label = kwargs["messages"][0]["content"][0]["text"].split(".", 1)[0]
            return SimpleNamespace(
                choices=[SimpleNamespace(
                    message=SimpleNamespace(content=label),
                    finish_reason="stop",
                )]
            )

    class FakeClient:
        def __init__(self, **kwargs):
            self.chat = SimpleNamespace(completions=FakeCompletions())

    original_client = transcript.OpenAI
    original_key = transcript.settings.openai_api_key
    transcript.OpenAI = FakeClient
    transcript.settings.openai_api_key = "test-key"
    try:
        result = transcript.ocr_video_frames(frames, on_attempt=lambda: attempts.append(1))
    finally:
        transcript.OpenAI = original_client
        transcript.settings.openai_api_key = original_key

    assert result == "Visual frames 1-3 of one cooking video"
    assert len(attempts) == 3


def test_video_frame_sampler_bounds_tools_and_removes_download(tmp_path):
    original_ytdlp = extract._run_ytdlp
    original_run = extract.subprocess.run
    original_which = extract.shutil.which
    original_validate = extract.validate_public_url
    calls = []

    def fake_ytdlp(args, timeout):
        output = Path(args[args.index("-o") + 1])
        output.write_bytes(b"v" * 1_000)
        calls.append((args, timeout, output))
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    def fake_run(command, **kwargs):
        if command[0].endswith("ffprobe"):
            calls.append((command, kwargs))
            return SimpleNamespace(returncode=0, stdout="20.0\n", stderr="")
        output = Path(command[-1])
        if output.name == "thumbs.raw":
            # Distinct 9x8 greyscale frames so dHash keeps several timestamps.
            blob = bytearray()
            for index in range(10):
                blob.extend(bytes(((index * 17 + col) % 256) for col in range(72)))
            output.write_bytes(blob)
        else:
            output.write_bytes(b"j" * 600)
        calls.append((command, kwargs))
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    extract._run_ytdlp = fake_ytdlp
    extract.subprocess.run = fake_run
    extract.shutil.which = lambda name: f"/usr/local/bin/{name}"
    extract.validate_public_url = lambda *args, **kwargs: None
    try:
        result = extract.download_tiktok_video_frames(
            "https://www.tiktok.com/@cook/video/1",
            media_id="../../unsafe",
            duration_seconds=20,
        )
        assert 1 <= len(result.paths) <= extract.MAX_UNIQUE_VISION_FRAMES
        assert all(path.is_file() for path in result.paths)
        assert not calls[0][2].exists()
        assert calls[0][1] == extract.VIDEO_DOWNLOAD_TIMEOUT_SECONDS
        assert calls[1][1]["timeout"] == extract.VIDEO_PROBE_TIMEOUT_SECONDS
        assert calls[2][1]["timeout"] == extract.FRAME_EXTRACT_TIMEOUT_SECONDS
    finally:
        extract._run_ytdlp = original_ytdlp
        extract.subprocess.run = original_run
        extract.shutil.which = original_which
        extract.validate_public_url = original_validate
        if "result" in locals():
            extract.shutil.rmtree(result.directory, ignore_errors=True)


def test_video_frame_sampler_cleans_temporary_media_on_failure():
    original_ytdlp = extract._run_ytdlp
    original_which = extract.shutil.which
    original_validate = extract.validate_public_url
    directory = None

    def fake_ytdlp(args, timeout):
        nonlocal directory
        output = Path(args[args.index("-o") + 1])
        directory = output.parent
        output.write_bytes(b"v" * 1_000)
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    extract._run_ytdlp = fake_ytdlp
    extract.shutil.which = lambda name: None
    extract.validate_public_url = lambda *args, **kwargs: None
    try:
        with pytest.raises(extract.ExtractError, match="ffmpeg and ffprobe"):
            extract.download_tiktok_video_frames(
                "https://www.tiktok.com/@cook/video/1",
                media_id="1",
                duration_seconds=20,
            )
    finally:
        extract._run_ytdlp = original_ytdlp
        extract.shutil.which = original_which
        extract.validate_public_url = original_validate

    assert directory is not None and not directory.exists()


def test_upload_cover_jpeg_returns_public_url(monkeypatch, tmp_path):
    from app import store

    monkeypatch.setenv("COVER_STORAGE_DIR", str(tmp_path))
    monkeypatch.setattr(store.settings, "cover_public_base_url", "https://cdn.example/covers")
    jpeg = b"\xff\xd8\xff\xe0" + b"\x00" * 16
    url = store.upload_cover_jpeg(jpeg, key="abcd.jpg")
    assert url == "https://cdn.example/covers/abcd.jpg"
    assert (tmp_path / "abcd.jpg").read_bytes() == jpeg


def test_upload_cover_jpeg_rejects_non_jpeg(monkeypatch, tmp_path):
    from app import store

    monkeypatch.setenv("COVER_STORAGE_DIR", str(tmp_path))
    monkeypatch.setattr(store.settings, "cover_public_base_url", "https://cdn.example/covers")
    assert store.upload_cover_jpeg(b"not-a-jpeg", key="abcd.jpg") is None
    assert store.upload_cover_jpeg(b"\xff\xd8\xff" + b"x" * 3_000_000, key="abcd.jpg") is None


def test_cover_sample_times_are_five_frames_in_first_two_point_five_seconds():
    times = extract.cover_sample_times(20)
    assert times == [0.25, 0.75, 1.25, 1.75, 2.25]
    short = extract.cover_sample_times(1)
    assert len(short) == 5
    assert short[0] >= 0
    assert short[-1] < 1.0


def test_select_best_cover_prefers_high_variance_frame(tmp_path):
    flat = bytes([128] * (extract.COVER_GRAY_SIZE ** 2))
    varied = bytes((index * 17) % 256 for index in range(extract.COVER_GRAY_SIZE ** 2))
    (tmp_path / "cover.raw").write_bytes(flat + varied)
    dull = tmp_path / "cover-01.jpg"
    sharp = tmp_path / "cover-02.jpg"
    dull.write_bytes(b"dull")
    sharp.write_bytes(b"sharp")
    assert extract.select_best_cover_path([dull, sharp], tmp_path) == sharp


def test_cover_frame_sampler_uses_two_point_five_second_section(tmp_path):
    original_ytdlp = extract._run_ytdlp
    original_run = extract.subprocess.run
    original_which = extract.shutil.which
    original_validate = extract.validate_public_url
    calls = []

    def fake_ytdlp(args, timeout):
        output = Path(args[args.index("-o") + 1])
        output.write_bytes(b"v" * 1_000)
        calls.append(args)
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    def fake_run(command, **kwargs):
        if command[0].endswith("ffprobe"):
            return SimpleNamespace(returncode=0, stdout="12.0\n", stderr="")
        dest = Path(command[-1])
        if dest.name == "cover.raw":
            dest.write_bytes(bytes(range(256)) * ((extract.COVER_GRAY_SIZE ** 2) * 5 // 256))
        elif "%02d" in dest.name:
            for index in range(1, 6):
                (dest.parent / f"cover-{index:02d}.jpg").write_bytes(b"j" * 400)
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    extract._run_ytdlp = fake_ytdlp
    extract.subprocess.run = fake_run
    extract.shutil.which = lambda name: f"/usr/local/bin/{name}"
    extract.validate_public_url = lambda *args, **kwargs: None
    try:
        result = extract.download_cover_frames(
            "https://www.tiktok.com/@cook/video/1",
            media_id="1",
            duration_seconds=20,
        )
        assert len(result.paths) == 5
        assert all(path.is_file() for path in result.paths)
        assert extract.COVER_DOWNLOAD_SECTION in calls[0]
        assert extract.select_best_cover_path(result.paths, result.directory) in result.paths
    finally:
        extract._run_ytdlp = original_ytdlp
        extract.subprocess.run = original_run
        extract.shutil.which = original_which
        extract.validate_public_url = original_validate
        if "result" in locals():
            extract.shutil.rmtree(result.directory, ignore_errors=True)


def test_bounded_recipe_source_never_slices_ordered_ocr_json():
    final_marker = "FINAL-SLIDE-12"
    payload = {
        "platform": "tiktok",
        "title": "title" * 1_000,
        "description": "description" * 2_000,
        "author": "cook",
        "transcript": "spoken" * 3_000,
        "slide_text": "slide\n" * 2_000 + final_marker,
    }
    encoded = recipe_builder._bounded_source_json(payload)
    decoded = json.loads(encoded)
    assert decoded["slide_text"].endswith(final_marker)
    assert len(encoded) <= 24_000


def test_failed_carousel_ocr_settles_every_attempted_visual_cost(monkeypatch):
    from uuid import uuid4
    import app.pipeline as pipeline
    from app.costing import get_cost_meter

    settled = []
    jobs = []
    slides = SlideInfo("Recipe", "", None, ["https://cdn.example/1.jpg"], total_image_count=1)
    monkeypatch.setattr(pipeline, "detect_platform", lambda url: Platform.tiktok)
    monkeypatch.setattr(pipeline, "fetch_tiktok_slides", lambda url: slides)
    monkeypatch.setattr(pipeline, "build_recipe", lambda **kwargs: None)

    def fail_ocr(*, image_url, slide_index, on_attempt):
        on_attempt()
        meter = get_cost_meter()
        if meter is not None:
            meter.add_fallback(settings.cost_ocr_cents_per_slide, kind="ocr")
        raise extract.ExtractError(f"Incomplete TikTok carousel: unreadable slides {slide_index}")

    monkeypatch.setattr(pipeline, "ocr_one_slide", fail_ocr)
    monkeypatch.setattr(pipeline, "update_job", lambda *args, **kwargs: jobs.append(kwargs))
    monkeypatch.setattr(pipeline, "settle_spend", lambda **kwargs: settled.append(kwargs))
    monkeypatch.setattr(pipeline, "record_usage", lambda **kwargs: None)
    monkeypatch.setattr(pipeline, "release_job", lambda *args: None)
    monkeypatch.setattr(pipeline, "_drain_next_extract_for_user", lambda *a, **k: None)

    pipeline.run_extract_job(uuid4(), uuid4(), "https://www.tiktok.com/@cook/photo/1", "tiktok:1", "en-US")

    assert jobs[-1]["status"] == "failed"
    assert jobs[-1]["error"] == (
        "This TikTok carousel is incomplete. Try another link or a full recipe video."
    )
    assert settled[-1]["actual_cents"] == settings.cost_ocr_cents_per_slide


def test_photo_without_complete_slide_hydration_never_falls_back_to_video_metadata(monkeypatch):
    from uuid import uuid4
    import app.pipeline as pipeline

    jobs = []
    settled = []
    monkeypatch.setattr(pipeline, "detect_platform", lambda url: Platform.tiktok)
    monkeypatch.setattr(pipeline, "fetch_tiktok_slides", lambda url: None)
    monkeypatch.setattr(
        pipeline,
        "fetch_media_info",
        lambda url: pytest.fail("photo carousel must not enter video metadata pipeline"),
    )
    monkeypatch.setattr(pipeline, "update_job", lambda *args, **kwargs: jobs.append(kwargs))
    monkeypatch.setattr(pipeline, "settle_spend", lambda **kwargs: settled.append(kwargs))
    monkeypatch.setattr(pipeline, "release_job", lambda *args: None)
    monkeypatch.setattr(pipeline, "_drain_next_extract_for_user", lambda *a, **k: None)

    pipeline.run_extract_job(
        uuid4(),
        uuid4(),
        "https://www.tiktok.com/@cook/photo/1",
        "tiktok:photo:1",
        "en-US",
    )

    assert jobs[-1]["status"] == "failed"
    assert jobs[-1]["error"] == (
        "This TikTok carousel is incomplete. Try another link or a full recipe video."
    )
    assert settled[-1]["actual_cents"] == 0


def test_tiktok_video_without_caption_uses_three_bounded_frames(monkeypatch, tmp_path):
    from uuid import uuid4
    import app.pipeline as pipeline
    from app.costing import get_cost_meter
    from app.models import Ingredient, IngredientSection, Recipe, Step

    frame_dir = tmp_path / "frames"
    frame_dir.mkdir()
    paths = []
    for index in range(3):
        path = frame_dir / f"{index}.jpg"
        path.write_bytes(b"frame")
        paths.append(path)
    media = extract.MediaInfo(
        title="Quick dinner",
        description="Short caption",
        author="cook",
        thumbnail_url=None,
        duration_seconds=30,
        webpage_url="https://www.tiktok.com/@cook/video/1",
        subtitles_text="mix eggs",
        audio_path=None,
        media_id="1",
    )
    built = []
    settled = []
    recipe_id = uuid4()
    monkeypatch.setattr(pipeline, "detect_platform", lambda url: Platform.tiktok)
    monkeypatch.setattr(pipeline, "fetch_tiktok_slides", lambda url: None)
    monkeypatch.setattr(pipeline, "fetch_media_info", lambda url: media)
    monkeypatch.setattr(pipeline, "download_audio", lambda *a, **k: None)
    monkeypatch.setattr(pipeline, "local_transcript", lambda *a, **k: None)
    monkeypatch.setattr(
        pipeline,
        "download_video_frames",
        lambda *args, **kwargs: extract.VideoFrames(paths, frame_dir),
    )

    def fake_ocr(frame_paths, *, on_attempt):
        labels = []
        for frame_path in frame_paths:
            on_attempt()
            meter = get_cost_meter()
            if meter is not None:
                meter.add_fallback(settings.cost_ocr_cents_per_slide, kind="ocr")
            labels.append({
                "0.jpg": "Frame 1 ingredients",
                "1.jpg": "Frame 2 method",
                "2.jpg": "Frame 3 serving",
            }[frame_path.name])
        return "\n\n".join(labels)

    def fake_build(**kwargs):
        built.append(kwargs)
        slide = kwargs.get("slide_text") or ""
        if not all(label in slide for label in (
            "Frame 1 ingredients", "Frame 2 method", "Frame 3 serving"
        )):
            return None
        ingredient = Ingredient(name="egg")
        return Recipe(
            title="Eggs",
            ingredients=[ingredient],
            ingredient_sections=[IngredientSection(title="Ingredients", ingredients=[ingredient])],
            steps=[Step(order=1, text="Cook egg")],
            confidence=0.9,
            source_url=kwargs["source_url"],
            platform=Platform.tiktok,
        )

    monkeypatch.setattr(pipeline, "ocr_video_frames", fake_ocr)
    monkeypatch.setattr(pipeline, "build_recipe", fake_build)
    monkeypatch.setattr(pipeline, "choose_video_cover_url", lambda *a, **k: None)
    monkeypatch.setattr(pipeline, "update_job", lambda *args, **kwargs: None)
    monkeypatch.setattr(pipeline, "upsert_recipe", lambda *args, **kwargs: {"id": str(recipe_id)})
    monkeypatch.setattr(pipeline, "save_user_recipe", lambda *args, **kwargs: None)
    monkeypatch.setattr(pipeline, "record_usage", lambda **kwargs: None)
    monkeypatch.setattr(pipeline, "settle_spend", lambda **kwargs: settled.append(kwargs))
    monkeypatch.setattr(pipeline, "release_job", lambda *args: None)
    monkeypatch.setattr(pipeline, "_drain_next_extract_for_user", lambda *a, **k: None)

    pipeline.run_extract_job(uuid4(), uuid4(), media.webpage_url, "tiktok:video:1", "en-US")

    assert any((b.get("slide_text") or "").endswith("Frame 3 serving") for b in built)
    assert settled[-1]["actual_cents"] == round(3 * settings.cost_ocr_cents_per_slide, 4)
    assert not frame_dir.exists()


def test_tiktok_caption_survives_when_frame_download_fails(monkeypatch):
    from uuid import uuid4
    import app.pipeline as pipeline
    from app.models import Ingredient, IngredientSection, Recipe, Step

    media = extract.MediaInfo(
        title="Bakery chocolate chip cookies with butter and sugar",
        description="Bakery chocolate chip cookies with butter and sugar. Mix butter and sugar, add flour, bake.",
        author="baker",
        thumbnail_url=None,
        duration_seconds=60,
        webpage_url="https://vm.tiktok.com/ZGdQ6Hwqc/",
        subtitles_text="Cream butter and sugar. Add flour. Bake cookies.",
        audio_path=None,
        media_id="1",
    )
    built = []
    settled = []
    recipe_id = uuid4()
    monkeypatch.setattr(pipeline, "detect_platform", lambda url: Platform.tiktok)
    monkeypatch.setattr(pipeline, "fetch_tiktok_slides", lambda url: None)
    monkeypatch.setattr(pipeline, "fetch_media_info", lambda url: media)
    monkeypatch.setattr(
        pipeline,
        "download_video_frames",
        lambda *args, **kwargs: (_ for _ in ()).throw(extract.ExtractError("login wall")),
    )

    def fake_build(**kwargs):
        built.append(kwargs)
        ingredient = Ingredient(name="butter")
        return Recipe(
            title="Cookies",
            ingredients=[ingredient],
            ingredient_sections=[IngredientSection(title="Ingredients", ingredients=[ingredient])],
            steps=[Step(order=1, text="Bake butter cookies")],
            confidence=0.9,
            source_url=kwargs["source_url"],
            platform=Platform.tiktok,
        )

    monkeypatch.setattr(pipeline, "build_recipe", fake_build)
    monkeypatch.setattr(pipeline, "choose_video_cover_url", lambda *a, **k: None)
    monkeypatch.setattr(pipeline, "update_job", lambda *args, **kwargs: None)
    monkeypatch.setattr(pipeline, "upsert_recipe", lambda *args, **kwargs: {"id": str(recipe_id)})
    monkeypatch.setattr(pipeline, "save_user_recipe", lambda *args, **kwargs: None)
    monkeypatch.setattr(pipeline, "record_usage", lambda **kwargs: None)
    monkeypatch.setattr(pipeline, "settle_spend", lambda **kwargs: settled.append(kwargs))
    monkeypatch.setattr(pipeline, "release_job", lambda *args: None)
    monkeypatch.setattr(pipeline, "_drain_next_extract_for_user", lambda *a, **k: None)

    pipeline.run_extract_job(uuid4(), uuid4(), media.webpage_url, "tiktok:vm:1", "en-US")

    assert built[0]["title"].startswith("Bakery chocolate chip")
    assert settled[-1]["status"] == "settled"


def test_tiktok_short_link_oembed_is_not_skipped():
    original_open = extract.safe_urlopen_limited
    seen = []

    def fake_open(request, *, timeout, max_bytes):
        seen.append(request.full_url)
        return json.dumps(
            {"type": "video", "title": "Tiramisu from a vm link", "author_name": "baker"}
        ).encode()

    extract.safe_urlopen_limited = fake_open
    try:
        result = extract._fetch_tiktok_oembed("https://vm.tiktok.com/ZGdQ62tdt/")
    finally:
        extract.safe_urlopen_limited = original_open
    assert result is not None
    assert result.title == "Tiramisu from a vm link"
    assert result.description == "Tiramisu from a vm link"
    assert seen and "vm.tiktok.com" in seen[0]


def test_instagram_oembed_fallback_uses_canonical_reel_url():
    original_run = extract._run_ytdlp
    original_open = extract.safe_urlopen_limited
    seen = []

    def fake_open(request, *, timeout, max_bytes):
        seen.append(request.full_url)
        return json.dumps(
            {"title": "Pollo crispy con salsa", "author_name": "connieiscooking"}
        ).encode()

    original_validate = extract.validate_public_url
    extract._run_ytdlp = lambda *args, **kwargs: SimpleNamespace(
        returncode=1, stdout="", stderr="login required"
    )
    extract.validate_public_url = lambda *args, **kwargs: None
    extract.safe_urlopen_limited = fake_open
    try:
        result = extract.fetch_media_info(
            "https://www.instagram.com/reel/Dcq5Bx9NB4-/?stkn=token"
        )
    finally:
        extract._run_ytdlp = original_run
        extract.validate_public_url = original_validate
        extract.safe_urlopen_limited = original_open
    assert result.title.startswith("Pollo crispy")
    assert "instagram.com/api/v1/oembed/" in seen[-1]
    assert "Dcq5Bx9NB4-" in seen[-1]
    assert "stkn" not in seen[-1]


def test_tiktok_metadata_fallback_is_available_when_ytdlp_fails():
    original_run = extract._run_ytdlp
    original_oembed = extract._fetch_tiktok_oembed
    original_validate = extract.validate_public_url
    original_merge = extract._with_tiktok_page_evidence
    extract._run_ytdlp = lambda *args, **kwargs: (_ for _ in ()).throw(extract.ExtractError("yt-dlp unavailable"))
    extract.validate_public_url = lambda *args, **kwargs: None
    extract._with_tiktok_page_evidence = lambda url, media: media
    extract._fetch_tiktok_oembed = lambda url: extract.MediaInfo(
        title="Crispy chicken",
        description="",
        author="cook",
        thumbnail_url=None,
        duration_seconds=None,
        webpage_url=url,
        subtitles_text=None,
        audio_path=None,
    )
    try:
        result = extract.fetch_media_info("https://www.tiktok.com/@cook/video/1")
    finally:
        extract._run_ytdlp = original_run
        extract._fetch_tiktok_oembed = original_oembed
        extract.validate_public_url = original_validate
        extract._with_tiktok_page_evidence = original_merge
    assert result.title == "Crispy chicken"


def test_ytdlp_runs_from_active_python_environment():
    original_run = extract.subprocess.run
    calls = []

    def fake_run(command, **kwargs):
        calls.append((command, kwargs))
        return SimpleNamespace(returncode=0, stdout="{}", stderr="")

    extract.subprocess.run = fake_run
    try:
        extract._run_ytdlp(["--version"], timeout=7)
    finally:
        extract.subprocess.run = original_run

    assert calls[0][0][:3] == [extract.sys.executable, "-m", "yt_dlp"]
    assert calls[0][1]["timeout"] == 7


def test_provider_error_payload_is_not_saved_as_job_error():
    error = extract.ExtractError(
        "Error code: 401 - Incorrect API key provided: sk-proj-secret"
    )
    assert _safe_job_error(error) == "Extraction temporarily failed. Retry the import."


def test_supported_source_bound_is_an_actionable_job_error():
    error = extract.ExtractError("Recipe source text exceeds supported bound")
    assert _safe_job_error(error) == "This recipe source is too large to process."


def test_tiktok_metadata_fallback_is_available_when_ytdlp_json_is_invalid():
    original_run = extract._run_ytdlp
    original_oembed = extract._fetch_tiktok_oembed
    original_validate = extract.validate_public_url
    original_merge = extract._with_tiktok_page_evidence
    extract._run_ytdlp = lambda *args, **kwargs: SimpleNamespace(
        returncode=0,
        stdout="{invalid",
        stderr="",
    )
    extract.validate_public_url = lambda *args, **kwargs: None
    extract._with_tiktok_page_evidence = lambda url, media: media
    extract._fetch_tiktok_oembed = lambda url: extract.MediaInfo(
        title="Pasta bake",
        description="",
        author="cook",
        thumbnail_url=None,
        duration_seconds=None,
        webpage_url=url,
        subtitles_text=None,
        audio_path=None,
    )
    try:
        result = extract.fetch_media_info("https://www.tiktok.com/@cook/video/2")
    finally:
        extract._run_ytdlp = original_run
        extract._fetch_tiktok_oembed = original_oembed
        extract.validate_public_url = original_validate
        extract._with_tiktok_page_evidence = original_merge
    assert result.title == "Pasta bake"


def test_audio_download_failure_does_not_discard_valid_metadata():
    original_run = extract._run_ytdlp
    original_download = extract._download_audio
    original_validate = extract.validate_public_url
    extract._run_ytdlp = lambda *args, **kwargs: SimpleNamespace(
        returncode=0,
        stdout='{"id":"video-1","title":"Quick eggs","description":"Pan recipe"}',
        stderr="",
    )
    extract._download_audio = lambda *args, **kwargs: (_ for _ in ()).throw(
        extract.ExtractError("audio timeout")
    )
    extract.validate_public_url = lambda *args, **kwargs: None
    try:
        result = extract.fetch_media_info("https://www.youtube.com/watch?v=video-1")
    finally:
        extract._run_ytdlp = original_run
        extract._download_audio = original_download
        extract.validate_public_url = original_validate
    assert result.title == "Quick eggs"
    assert result.audio_path is None


def test_recipe_builder_preserves_ordered_carousel_metadata():
    original_client = recipe_builder.OpenAI
    original_key = recipe_builder.settings.openai_api_key

    class FakeCompletions:
        def create(self, **kwargs):
            return SimpleNamespace(
                choices=[
                    SimpleNamespace(
                        message=SimpleNamespace(
                            content=(
                                '{"title":"Pasta","description":"Fast dinner",'
                                '"ingredient_sections":[{"title":"Ingredients",'
                                '"ingredients":[{"name":"pasta","quantity":"250","unit":"g"}]}],'
                                '"tips":[],"steps":[{"order":1,"text":"Boil","duration_minutes":10}],'
                                '"servings":2,"prep_minutes":5,"cook_minutes":10,"tags":[],'
                                '"confidence":0.9,"missing_fields":[],'
                                '"is_complete":true,"blocking_gaps":[]}'
                            ),
                            refusal=None,
                        ),
                        finish_reason="stop",
                    )
                ]
            )

    class FakeClient:
        def __init__(self, **kwargs):
            self.chat = SimpleNamespace(completions=FakeCompletions())

    recipe_builder.OpenAI = FakeClient
    recipe_builder.settings.openai_api_key = "test-key"
    try:
        result = recipe_builder.build_recipe(
            platform=Platform.tiktok,
            source_url="https://www.tiktok.com/@cook/photo/1",
            title="Pasta",
            description="Fast dinner",
            author="cook",
            thumbnail_url="https://cdn.example/slide-1.jpg",
            carousel_image_urls=[
                "https://cdn.example/slide-1.jpg",
                "https://cdn.example/slide-2.jpg",
            ],
            transcript=None,
            slide_text="Boil pasta",
        )
    finally:
        recipe_builder.OpenAI = original_client
        recipe_builder.settings.openai_api_key = original_key

    assert result.carousel_image_urls == [
        "https://cdn.example/slide-1.jpg",
        "https://cdn.example/slide-2.jpg",
    ]


def _recipe_model_payload(**overrides) -> str:
    data = {
        "title": "Pasta",
        "description": "Fast dinner",
        "ingredient_sections": [
            {
                "title": "Ingredients",
                "ingredients": [{"name": "pasta", "quantity": "250", "unit": "g"}],
            }
        ],
        "tips": [],
        "steps": [{"order": 1, "text": "Boil", "duration_minutes": 10}],
        "servings": 2,
        "prep_minutes": 5,
        "cook_minutes": 10,
        "tags": [],
        "confidence": 0.9,
        "missing_fields": [],
        "is_complete": True,
        "blocking_gaps": [],
    }
    data.update(overrides)
    return json.dumps(data)


def _build_recipe_from_model_payload(payload: str):
    original_client = recipe_builder.OpenAI
    original_key = recipe_builder.settings.openai_api_key

    class FakeCompletions:
        def create(self, **kwargs):
            return SimpleNamespace(
                choices=[
                    SimpleNamespace(
                        message=SimpleNamespace(content=payload, refusal=None),
                        finish_reason="stop",
                    )
                ]
            )

    class FakeClient:
        def __init__(self, **kwargs):
            self.chat = SimpleNamespace(completions=FakeCompletions())

    recipe_builder.OpenAI = FakeClient
    recipe_builder.settings.openai_api_key = "test-key"
    try:
        return recipe_builder.build_recipe(
            platform=Platform.tiktok,
            source_url="https://www.tiktok.com/@cook/video/1",
            title="Pasta",
            description="Fast dinner",
            author="cook",
            thumbnail_url=None,
            transcript=None,
            slide_text="Boil pasta",
        )
    finally:
        recipe_builder.OpenAI = original_client
        recipe_builder.settings.openai_api_key = original_key


def test_recipe_builder_rejects_empty_or_unknown_output():
    cases = [
        {"ingredient_sections": [], "confidence": 0.9},
        {
            "ingredient_sections": [
                {"title": "Ingredients", "ingredients": [{"name": "", "quantity": None, "unit": None}]}
            ],
            "confidence": 0.9,
        },
        {
            "ingredient_sections": [
                {"title": "Ingredients", "ingredients": [{"name": "null", "quantity": None, "unit": None}]}
            ],
            "confidence": 0.9,
        },
        {"steps": [], "confidence": 0.9},
        {"steps": [{"order": 1, "text": "null", "duration_minutes": None}], "confidence": 0.9},
        {"confidence": 0.2},
        {"is_complete": False, "blocking_gaps": ["missing method"]},
    ]
    for overrides in cases:
        assert _build_recipe_from_model_payload(_recipe_model_payload(**overrides)) is None


def test_recipe_builder_keeps_named_ingredient_with_null_quantity():
    recipe = _build_recipe_from_model_payload(
        _recipe_model_payload(
            ingredient_sections=[
                {
                    "title": "Ingredients",
                    "ingredients": [{"name": "pasta", "quantity": None, "unit": None}],
                }
            ],
            steps=[{"order": 1, "text": "Boil the pasta until al dente", "duration_minutes": 10}],
        )
    )
    assert recipe is not None
    assert [(item.name, item.quantity) for item in recipe.ingredients] == [("pasta", "to taste")]


def test_undetermined_recipe_is_an_actionable_job_error():
    error = extract.ExtractError("Could not determine a recipe from this video.")
    assert _safe_job_error(error) == "Could not determine a recipe from this video."


def test_blank_quantity_placeholders_become_to_taste():
    from app.recipe_builder import _usable_ingredient_sections

    sections = _usable_ingredient_sections(
        [
            {
                "title": "Ingredients",
                "ingredients": [
                    {"name": "salt", "quantity": "—", "unit": None},
                    {"name": "paprika", "quantity": "-", "unit": None},
                    {"name": "flour", "quantity": "1", "unit": "cup"},
                ],
            }
        ],
        language_code="es-ES",
    )
    by_name = {item.name: item for section in sections for item in section.ingredients}
    assert by_name["salt"].quantity == "al gusto"
    assert by_name["paprika"].quantity == "al gusto"
    assert by_name["flour"].quantity == "1"
    assert by_name["flour"].unit == "cup"

def test_overlay_sample_times_cover_sequential_ingredient_cards():
    times = extract.overlay_sample_times(148.3)
    assert 24 <= len(times) <= extract.MAX_VIDEO_FRAMES
    for beat in (21.0, 34.5, 55.0, 90.0):
        assert min(abs(stamp - beat) for stamp in times) <= 2.4


def test_tiktok_caption_urls_prefer_original_track():
    from app.tiktok_slides import tiktok_caption_urls, tiktok_play_urls

    item = {
        "video": {
            "playAddr": "https://v16-webapp-prime.tiktok.com/play.mp4",
            "claInfo": {
                "captionInfos": [
                    {"url": "https://cdn.example/other.vtt", "isOriginalCaption": False},
                    {"url": "https://cdn.example/orig.vtt", "isOriginalCaption": True},
                ]
            },
            "subtitleInfos": [{"Url": "https://cdn.example/sub.vtt"}],
        }
    }
    assert tiktok_caption_urls(item)[0] == "https://cdn.example/orig.vtt"
    assert tiktok_play_urls(item) == ["https://v16-webapp-prime.tiktok.com/play.mp4"]


def test_tiktok_page_captions_replace_empty_ytdlp_subtitles(monkeypatch):
    import app.tiktok_slides as slides

    monkeypatch.setattr(
        slides,
        "fetch_tiktok_item",
        lambda url: {
            "desc": "Tiramisu",
            "author": {"uniqueId": "bromabakery"},
            "video": {
                "duration": 148,
                "playAddr": "https://v16-webapp-prime.tiktok.com/play.mp4",
                "claInfo": {
                    "captionInfos": [
                        {"url": "https://cdn.example/cap.vtt", "isOriginalCaption": True}
                    ]
                },
            },
        },
    )
    monkeypatch.setattr(extract, "_download_subtitle_url", lambda url: "four eggs and mascarpone")
    media = extract.MediaInfo(
        title="Anyone Can Bake",
        description="find the full recipe",
        author=None,
        thumbnail_url=None,
        duration_seconds=None,
        webpage_url="https://vm.tiktok.com/ZGdQ62tdt/",
        subtitles_text=None,
        audio_path=None,
    )
    result = extract._with_tiktok_page_evidence("https://vm.tiktok.com/ZGdQ62tdt/", media)
    assert result.subtitles_text == "four eggs and mascarpone"
    assert result.duration_seconds == 148
    assert result.author == "bromabakery"
    assert result.play_urls == ["https://v16-webapp-prime.tiktok.com/play.mp4"]


def test_overlay_ocr_skips_empty_marker_without_failing(tmp_path):
    import app.transcript as transcript

    path = tmp_path / "frame-001.jpg"
    path.write_bytes(b"frame")

    class FakeCompletions:
        def create(self, **kwargs):
            return SimpleNamespace(
                choices=[SimpleNamespace(
                    message=SimpleNamespace(content="NO_RECIPE_TEXT"),
                    finish_reason="stop",
                )]
            )

    class FakeClient:
        def __init__(self, **kwargs):
            self.chat = SimpleNamespace(completions=FakeCompletions())

    original_client = transcript.OpenAI
    original_key = transcript.settings.openai_api_key
    transcript.OpenAI = FakeClient
    transcript.settings.openai_api_key = "test-key"
    try:
        assert transcript.ocr_video_frames([path]) == ""
    finally:
        transcript.OpenAI = original_client
        transcript.settings.openai_api_key = original_key


def test_tiktok_spoken_caption_still_ocrs_on_screen_overlays(monkeypatch, tmp_path):
    from uuid import uuid4
    import app.pipeline as pipeline
    from app.models import Ingredient, IngredientSection, Recipe, Step

    frame_dir = tmp_path / "frames"
    frame_dir.mkdir()
    paths = []
    for index in range(3):
        path = frame_dir / f"{index}.jpg"
        path.write_bytes(b"frame")
        paths.append(path)
    media = extract.MediaInfo(
        title="Anyone Can Bake Ep. 4: Tiramisu marketing caption with many filler words "
        "about dinner parties silky mascarpone cream and bakery layers",
        description="find the full recipe bromabakery.com under Classic Tiramisu enjoyyyyy",
        author="Broma Bakery",
        thumbnail_url=None,
        duration_seconds=148,
        webpage_url="https://vm.tiktok.com/ZGdQ62tdt/",
        subtitles_text=" ".join(["spoken"] * 100),
        audio_path=None,
        media_id="1",
    )
    built = []
    downloads = []
    recipe_id = uuid4()
    monkeypatch.setattr(pipeline, "detect_platform", lambda url: Platform.tiktok)
    monkeypatch.setattr(pipeline, "fetch_tiktok_slides", lambda url: None)
    monkeypatch.setattr(pipeline, "fetch_media_info", lambda url: media)
    monkeypatch.setattr(pipeline, "download_audio", lambda *a, **k: None)
    monkeypatch.setattr(pipeline, "local_transcript", lambda *a, **k: None)

    def fake_download(*args, **kwargs):
        downloads.append(kwargs)
        return extract.VideoFrames(paths, frame_dir)

    def fake_ocr(frame_paths, *, on_attempt):
        for _ in frame_paths:
            on_attempt()
        return "4 large eggs, separated\n\n1/3 cup (67g) granulated sugar"

    def fake_build(**kwargs):
        built.append(kwargs)
        slide = kwargs.get("slide_text") or ""
        if "1/3 cup (67g) granulated sugar" not in slide:
            return None
        ingredient = Ingredient(name="egg", quantity="4", unit="large")
        return Recipe(
            title="Tiramisu",
            ingredients=[ingredient],
            ingredient_sections=[IngredientSection(title="Ingredients", ingredients=[ingredient])],
            steps=[Step(order=1, text="Mix eggs with sugar")],
            confidence=0.9,
            source_url=kwargs["source_url"],
            platform=Platform.tiktok,
        )

    monkeypatch.setattr(pipeline, "download_video_frames", fake_download)
    monkeypatch.setattr(pipeline, "ocr_video_frames", fake_ocr)
    monkeypatch.setattr(pipeline, "build_recipe", fake_build)
    monkeypatch.setattr(pipeline, "choose_video_cover_url", lambda *a, **k: None)
    monkeypatch.setattr(pipeline, "update_job", lambda *args, **kwargs: None)
    monkeypatch.setattr(pipeline, "upsert_recipe", lambda *args, **kwargs: {"id": str(recipe_id)})
    monkeypatch.setattr(pipeline, "save_user_recipe", lambda *args, **kwargs: None)
    monkeypatch.setattr(pipeline, "record_usage", lambda **kwargs: None)
    monkeypatch.setattr(pipeline, "settle_spend", lambda **kwargs: None)
    monkeypatch.setattr(pipeline, "release_job", lambda *args: None)
    monkeypatch.setattr(pipeline, "_drain_next_extract_for_user", lambda *a, **k: None)

    pipeline.run_extract_job(uuid4(), uuid4(), media.webpage_url, "tiktok:tiramisu:1", "en-US")

    assert downloads
    assert built[0]["transcript"].startswith("spoken")
    assert any("1/3 cup (67g) granulated sugar" in (b.get("slide_text") or "") for b in built)


def test_tiktok_play_url_is_used_when_ytdlp_download_fails(tmp_path):
    output = tmp_path / "video.mp4"
    original_ytdlp = extract._run_ytdlp
    original_open = extract.safe_urlopen
    original_validate = extract.validate_public_url

    class FakeResponse:
        def __init__(self):
            self._sent = False

        def read(self, _n):
            if self._sent:
                return b""
            self._sent = True
            return b"v" * 1000

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    extract._run_ytdlp = lambda args, timeout: SimpleNamespace(returncode=1, stdout="", stderr="blocked")
    extract.validate_public_url = lambda *args, **kwargs: None
    extract.safe_urlopen = lambda request, timeout=20: FakeResponse()
    try:
        extract._download_tiktok_media(
            "https://www.tiktok.com/@cook/video/1",
            output,
            ["https://v16-webapp-prime.tiktok.com/play.mp4"],
        )
        assert output.read_bytes() == b"v" * 1000
    finally:
        extract._run_ytdlp = original_ytdlp
        extract.safe_urlopen = original_open
        extract.validate_public_url = original_validate
