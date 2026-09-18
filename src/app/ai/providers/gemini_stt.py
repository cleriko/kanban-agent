from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path
from typing import Any

import httpx

from .base import ProviderError, Transcript, TranscriptionProvider, TranscriptSegment

log = logging.getLogger(__name__)

API_ROOT = "https://generativelanguage.googleapis.com"

# Gemini's responseSchema is the OpenAPI subset, with uppercase type names.
TRANSCRIPT_SCHEMA: dict[str, Any] = {
    "type": "OBJECT",
    "properties": {
        "language": {"type": "STRING"},
        "segments": {
            "type": "ARRAY",
            "items": {
                "type": "OBJECT",
                "properties": {
                    "start": {"type": "NUMBER", "description": "Start time in seconds."},
                    "end": {"type": "NUMBER", "description": "End time in seconds."},
                    "speaker": {"type": "STRING", "description": "Speaker name or label."},
                    "text": {"type": "STRING"},
                },
                "required": ["start", "end", "text"],
            },
        },
    },
    "required": ["segments"],
}

PROMPT = """\
Transcribe this meeting recording.

Return every spoken segment with its start and end time in seconds. Break on natural
pauses and speaker changes, not mid-sentence.

Label speakers when you can tell them apart. Use their name if it is said in the
recording, otherwise "Speaker 1", "Speaker 2" and so on. Leave speaker empty if you
genuinely cannot tell.

Transcribe what was actually said. Do not summarise, do not clean up grammar, do not
add commentary. If a passage is inaudible, leave it out rather than guessing.
"""


class GeminiTranscriptionProvider(TranscriptionProvider):
    """Speech-to-text via the Gemini API.

    Uploads the recording through the Files API and asks for a timestamped,
    speaker-labelled transcript as structured JSON.

    This sends meeting audio to Google. It is opt-in for that reason — but when it
    is selected the VPS needs no Whisper, no ffmpeg and no model weights at all,
    which is the difference between a 4 GB box and a 512 MB one.
    """

    name = "gemini"

    def __init__(self, api_key: str, model: str = "gemini-2.5-flash",
                 *, timeout: float = 900.0) -> None:
        if not api_key:
            raise ProviderError("WC_GEMINI_API_KEY is required for the gemini transcription provider")
        self._key = api_key
        self._model = model
        self._timeout = timeout

    # --- Files API --------------------------------------------------------

    async def _upload(self, client: httpx.AsyncClient, path: Path, mime: str) -> dict[str, Any]:
        size = path.stat().st_size

        start = await client.post(
            f"{API_ROOT}/upload/v1beta/files",
            params={"key": self._key},
            headers={
                "X-Goog-Upload-Protocol": "resumable",
                "X-Goog-Upload-Command": "start",
                "X-Goog-Upload-Header-Content-Length": str(size),
                "X-Goog-Upload-Header-Content-Type": mime,
                "Content-Type": "application/json",
            },
            json={"file": {"display_name": path.name}},
        )
        if start.status_code >= 400:
            raise ProviderError(f"gemini upload could not start ({start.status_code}): {start.text[:300]}")

        upload_url = start.headers.get("x-goog-upload-url")
        if not upload_url:
            raise ProviderError("gemini did not return an upload URL")

        # Stream from disk: a long meeting should not be held in memory twice.
        # This must be an *async* generator — httpx refuses a sync one on an
        # AsyncClient, which is exactly how this failed the first time.
        async def chunks():
            with path.open("rb") as handle:
                while True:
                    block = await asyncio.to_thread(handle.read, 1024 * 1024)
                    if not block:
                        break
                    yield block

        finalize = await client.post(
            upload_url,
            params={"key": self._key},
            headers={
                "X-Goog-Upload-Command": "upload, finalize",
                "X-Goog-Upload-Offset": "0",
                "Content-Length": str(size),
            },
            content=chunks(),
        )
        if finalize.status_code >= 400:
            raise ProviderError(f"gemini upload failed ({finalize.status_code}): {finalize.text[:300]}")

        file = finalize.json().get("file") or {}
        if not file.get("uri"):
            raise ProviderError("gemini upload returned no file URI")
        return file

    async def _await_active(self, client: httpx.AsyncClient, file: dict[str, Any],
                            on_progress=None) -> dict[str, Any]:
        """Gemini processes audio before it can be used. Poll until it is ACTIVE."""
        name = file.get("name")
        deadline = asyncio.get_running_loop().time() + self._timeout
        delay = 2.0

        while file.get("state") == "PROCESSING":
            if asyncio.get_running_loop().time() > deadline:
                raise ProviderError("gemini took too long to process the audio")
            await asyncio.sleep(delay)
            delay = min(delay * 1.5, 15)
            if on_progress:
                await on_progress(0.35)
            response = await client.get(f"{API_ROOT}/v1beta/{name}", params={"key": self._key})
            if response.status_code >= 400:
                raise ProviderError(f"gemini file check failed: {response.text[:200]}")
            file = response.json()

        if file.get("state") == "FAILED":
            raise ProviderError("gemini could not process the audio file")
        return file

    async def _delete(self, client: httpx.AsyncClient, name: str | None) -> None:
        if not name:
            return
        try:
            await client.delete(f"{API_ROOT}/v1beta/{name}", params={"key": self._key})
        except httpx.HTTPError:
            # Gemini expires uploads after 48h anyway; a failure here is not worth
            # failing the job over.
            log.warning("could not delete the uploaded audio %s", name)

    # --- Transcription ----------------------------------------------------

    async def transcribe(self, audio_path: str, *, language: str | None = None,
                         on_progress=None) -> Transcript:
        path = Path(audio_path)
        if not path.exists():
            raise ProviderError(f"audio file not found: {audio_path}")

        mime = _mime_for(path)
        file_name: str | None = None

        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                if on_progress:
                    await on_progress(0.1)
                file = await self._upload(client, path, mime)
                file_name = file.get("name")

                if on_progress:
                    await on_progress(0.3)
                file = await self._await_active(client, file, on_progress)

                if on_progress:
                    await on_progress(0.5)

                prompt = PROMPT
                if language:
                    prompt += f"\nThe recording is in {language}."

                response = await client.post(
                    f"{API_ROOT}/v1beta/models/{self._model}:generateContent",
                    params={"key": self._key},
                    json={
                        "contents": [{
                            "parts": [
                                {"file_data": {"mime_type": mime, "file_uri": file["uri"]}},
                                {"text": prompt},
                            ]
                        }],
                        "generationConfig": {
                            "responseMimeType": "application/json",
                            "responseSchema": TRANSCRIPT_SCHEMA,
                            "temperature": 0.0,
                        },
                    },
                )
                if response.status_code >= 400:
                    raise ProviderError(
                        f"gemini transcription failed ({response.status_code}): {response.text[:300]}")

                payload = _first_json_part(response.json())
                await self._delete(client, file_name)

        except httpx.HTTPError as exc:
            raise ProviderError(f"cannot reach the Gemini API: {exc}") from exc

        if on_progress:
            await on_progress(1.0)

        segments = _segments(payload.get("segments") or [])
        return Transcript(
            text=" ".join(s.text for s in segments),
            segments=segments,
            language=payload.get("language") or language,
            duration=segments[-1].end if segments else 0.0,
            model=self._model,
        )

    async def health(self) -> bool:
        if not self._key:
            return False
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                response = await client.get(f"{API_ROOT}/v1beta/models/{self._model}",
                                            params={"key": self._key})
            return response.status_code == 200
        except httpx.HTTPError:
            return False


# --- Helpers ----------------------------------------------------------------


def _mime_for(path: Path) -> str:
    return {
        ".m4a": "audio/mp4", ".mp4": "audio/mp4", ".aac": "audio/aac",
        ".mp3": "audio/mpeg", ".wav": "audio/wav", ".flac": "audio/flac",
        ".ogg": "audio/ogg", ".opus": "audio/ogg", ".webm": "audio/webm",
    }.get(path.suffix.lower(), "audio/mp4")


def _first_json_part(body: dict[str, Any]) -> dict[str, Any]:
    import json

    candidates = body.get("candidates") or []
    if not candidates:
        reason = (body.get("promptFeedback") or {}).get("blockReason")
        raise ProviderError(f"gemini returned no transcript{f' ({reason})' if reason else ''}")

    for part in (candidates[0].get("content") or {}).get("parts") or []:
        text = part.get("text")
        if not text:
            continue
        try:
            value = json.loads(text)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            return value
    raise ProviderError("gemini returned no usable JSON transcript")


def _segments(raw: Any) -> list[TranscriptSegment]:
    """Coerces the model's output. Bad rows are dropped rather than failing the job."""
    out: list[TranscriptSegment] = []
    for item in raw if isinstance(raw, list) else []:
        if not isinstance(item, dict):
            continue
        text = str(item.get("text") or "").strip()
        if not text:
            continue
        try:
            start = float(item.get("start") or 0.0)
            end = float(item.get("end") or start)
        except (TypeError, ValueError):
            continue
        speaker = str(item.get("speaker") or "").strip() or None
        out.append(TranscriptSegment(start=start, end=max(end, start), text=text, speaker=speaker))
    out.sort(key=lambda s: s.start)
    return out
