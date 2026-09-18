from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from app.ai.providers.base import ProviderError
from app.ai.providers.gemini_stt import (
    GeminiTranscriptionProvider, _first_json_part, _mime_for, _segments,
)

API = "https://generativelanguage.googleapis.com"


def transcript_body(segments: list[dict]) -> dict:
    return {"candidates": [{"content": {"parts": [
        {"text": json.dumps({"language": "en", "segments": segments})}]}}]}


def mock_transport(*, segments: list[dict] | None = None,
                   processing_rounds: int = 0,
                   generate_status: int = 200) -> httpx.MockTransport:
    """Stands in for the whole Files-API + generateContent dance."""
    state = {"checks": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)

        if url.startswith(f"{API}/upload/v1beta/files"):
            if request.headers.get("X-Goog-Upload-Command") == "start":
                return httpx.Response(200, headers={"x-goog-upload-url": f"{API}/upload-session"})
            return httpx.Response(200, json={})

        if url.startswith(f"{API}/upload-session"):
            state_value = "PROCESSING" if processing_rounds else "ACTIVE"
            return httpx.Response(200, json={"file": {
                "name": "files/abc", "uri": f"{API}/v1beta/files/abc", "state": state_value}})

        if request.method == "GET" and "/v1beta/files/abc" in url:
            state["checks"] += 1
            done = state["checks"] >= processing_rounds
            return httpx.Response(200, json={
                "name": "files/abc", "uri": f"{API}/v1beta/files/abc",
                "state": "ACTIVE" if done else "PROCESSING"})

        if request.method == "DELETE":
            return httpx.Response(200, json={})

        if ":generateContent" in url:
            if generate_status >= 400:
                return httpx.Response(generate_status, text="quota exceeded")
            return httpx.Response(200, json=transcript_body(segments or []))

        return httpx.Response(404, text=f"unexpected {request.method} {url}")

    return httpx.MockTransport(handler)


@pytest.fixture
def audio(tmp_path: Path) -> Path:
    path = tmp_path / "meeting.m4a"
    path.write_bytes(b"\x00\x01" * 2048)
    return path


def patch_client(monkeypatch, transport: httpx.MockTransport) -> None:
    original = httpx.AsyncClient

    def factory(*args, **kwargs):
        kwargs["transport"] = transport
        return original(*args, **kwargs)

    monkeypatch.setattr("app.ai.providers.gemini_stt.httpx.AsyncClient", factory)


# --- Happy path -------------------------------------------------------------


@pytest.mark.asyncio
async def test_transcribes_with_timestamps_and_speakers(monkeypatch, audio):
    patch_client(monkeypatch, mock_transport(segments=[
        {"start": 0.0, "end": 6.2, "speaker": "Alex", "text": "We should launch onboarding."},
        {"start": 6.2, "end": 12.0, "speaker": "Sarah", "text": "I can finish the screens."},
    ]))

    provider = GeminiTranscriptionProvider(api_key="test")
    seen: list[float] = []

    async def progress(fraction: float) -> None:
        seen.append(fraction)

    result = await provider.transcribe(str(audio), on_progress=progress)

    assert len(result.segments) == 2
    assert result.segments[0].speaker == "Alex"
    assert result.segments[0].start == 0.0
    assert result.segments[1].text == "I can finish the screens."
    assert result.text == "We should launch onboarding. I can finish the screens."
    assert result.duration == 12.0
    assert result.language == "en"
    assert seen and seen[-1] == 1.0, "progress must reach 1.0"


@pytest.mark.asyncio
async def test_waits_for_gemini_to_finish_processing(monkeypatch, audio):
    # The Files API returns PROCESSING first; we must poll rather than proceed.
    patch_client(monkeypatch, mock_transport(
        segments=[{"start": 0, "end": 1, "text": "hello"}], processing_rounds=2))

    provider = GeminiTranscriptionProvider(api_key="test", timeout=30)
    result = await provider.transcribe(str(audio))
    assert result.segments[0].text == "hello"


# --- Failures ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_api_failure_surfaces_as_provider_error(monkeypatch, audio):
    patch_client(monkeypatch, mock_transport(generate_status=429))
    provider = GeminiTranscriptionProvider(api_key="test")
    with pytest.raises(ProviderError, match="429"):
        await provider.transcribe(str(audio))


@pytest.mark.asyncio
async def test_missing_file_is_reported_clearly():
    provider = GeminiTranscriptionProvider(api_key="test")
    with pytest.raises(ProviderError, match="audio file not found"):
        await provider.transcribe("/nope/missing.m4a")


@pytest.mark.asyncio
async def test_missing_key_is_rejected_at_construction():
    with pytest.raises(ProviderError, match="WC_GEMINI_API_KEY"):
        GeminiTranscriptionProvider(api_key="")


@pytest.mark.asyncio
async def test_blocked_response_is_explained(monkeypatch, audio):
    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if request.headers.get("X-Goog-Upload-Command") == "start":
            return httpx.Response(200, headers={"x-goog-upload-url": f"{API}/upload-session"})
        if url.startswith(f"{API}/upload-session"):
            return httpx.Response(200, json={"file": {
                "name": "files/abc", "uri": "u", "state": "ACTIVE"}})
        if ":generateContent" in url:
            return httpx.Response(200, json={"promptFeedback": {"blockReason": "SAFETY"}})
        return httpx.Response(200, json={})

    patch_client(monkeypatch, httpx.MockTransport(handler))
    provider = GeminiTranscriptionProvider(api_key="test")
    with pytest.raises(ProviderError, match="SAFETY"):
        await provider.transcribe(str(audio))


# --- Coercion ---------------------------------------------------------------


def test_segments_drop_unusable_rows_rather_than_failing():
    segments = _segments([
        {"start": 5, "end": 6, "text": "second"},
        {"start": 0, "end": 1, "text": "first", "speaker": "  "},
        {"start": 2, "end": 3, "text": "   "},          # empty text
        {"start": "x", "end": 1, "text": "bad start"},   # unparseable
        "not an object",
        {"start": 9, "end": 8, "text": "end before start"},
    ])
    assert [s.text for s in segments] == ["first", "second", "end before start"]
    assert segments[0].speaker is None, "a blank speaker becomes None"
    assert segments[-1].end >= segments[-1].start, "end is clamped to start"


def test_mime_types_cover_what_the_mac_app_records():
    assert _mime_for(Path("a.m4a")) == "audio/mp4"
    assert _mime_for(Path("a.wav")) == "audio/wav"
    assert _mime_for(Path("a.unknown")) == "audio/mp4", "falls back to the recorder's format"


def test_first_json_part_skips_prose():
    body = {"candidates": [{"content": {"parts": [
        {"text": "Here is the transcript:"},
        {"text": json.dumps({"segments": [{"start": 0, "end": 1, "text": "hi"}]})},
    ]}}]}
    assert _first_json_part(body)["segments"][0]["text"] == "hi"


# --- Gateway selection ------------------------------------------------------


@pytest.mark.asyncio
async def test_gateway_selects_gemini_and_skips_ollama(monkeypatch):
    from app.ai.gateway import AIGateway
    from app.config import Settings

    settings = Settings(transcription_provider="gemini", llm_provider="gemini",
                        gemini_api_key="test")
    gateway = AIGateway(settings)

    assert gateway.transcription.name == "gemini"
    assert gateway.llm.name == "gemini"
    assert settings.needs_ollama is False
    assert settings.uses_gemini is True


@pytest.mark.asyncio
async def test_local_stack_does_not_report_using_gemini():
    from app.config import Settings

    settings = Settings(transcription_provider="faster_whisper", llm_provider="ollama")
    assert settings.uses_gemini is False
    assert settings.needs_ollama is True
