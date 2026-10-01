"""Speech transcription must use OpenAI only."""

from pathlib import Path
from types import SimpleNamespace

import pytest

import app.stt as stt
from app.extract import ExtractError


def test_transcription_uses_openai_and_passes_language(monkeypatch, tmp_path):
    audio = tmp_path / "a.wav"
    audio.write_bytes(b"RIFF")
    calls = []

    class FakeTranscriptions:
        def create(self, **kwargs):
            calls.append(kwargs)
            return SimpleNamespace(text="OpenAI transcript")

    class FakeClient:
        def __init__(self, **kwargs):
            self.audio = SimpleNamespace(transcriptions=FakeTranscriptions())

    monkeypatch.setattr(stt, "OpenAI", FakeClient)
    monkeypatch.setattr(stt.settings, "openai_api_key", "test")
    monkeypatch.setattr(stt, "record_transcription_usage", lambda *a, **k: None)
    text, provider = stt.rotate_transcript(audio, language_code="es-ES")

    assert (text, provider) == ("OpenAI transcript", "openai")
    assert calls[0]["language"] == "es"
    assert calls[0]["model"] == stt.settings.transcribe_model


def test_transcription_without_openai_key_fails_closed(monkeypatch, tmp_path):
    audio = tmp_path / "a.wav"
    audio.write_bytes(b"RIFF")
    monkeypatch.setattr(stt.settings, "openai_api_key", "")
    with pytest.raises(ExtractError, match="OPENAI_API_KEY"):
        stt.rotate_transcript(audio, local_fn=lambda _path: "local transcript")
