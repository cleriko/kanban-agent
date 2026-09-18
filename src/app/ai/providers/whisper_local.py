from __future__ import annotations

import asyncio
import logging
from typing import Any

from .base import ProviderError, Transcript, TranscriptionProvider, TranscriptSegment

log = logging.getLogger(__name__)


class FasterWhisperProvider(TranscriptionProvider):
    """Local speech-to-text with faster-whisper (CTranslate2).

    Runs entirely on the VPS — no audio is sent anywhere. The model is loaded once
    and reused; loading is done in a thread so the first request does not block the
    event loop, and transcription itself is a generator we drain in a thread too.
    """

    name = "faster_whisper"

    def __init__(self, model_size: str = "base.en", device: str = "auto",
                 compute_type: str = "int8", vad_filter: bool = True) -> None:
        self._model_size = model_size
        self._device = device
        self._compute_type = compute_type
        self._vad_filter = vad_filter
        self._model: Any = None
        self._lock = asyncio.Lock()

    async def _load(self) -> Any:
        if self._model is not None:
            return self._model
        # One loader at a time: two concurrent jobs must not each load a model.
        async with self._lock:
            if self._model is not None:
                return self._model
            try:
                from faster_whisper import WhisperModel
            except ImportError as exc:
                raise ProviderError(
                    "faster-whisper is not installed. Install the 'whisper' extra on the VPS: "
                    "pip install '.[whisper]'"
                ) from exc

            log.info("loading whisper model %s (device=%s compute=%s)",
                     self._model_size, self._device, self._compute_type)
            self._model = await asyncio.to_thread(
                WhisperModel, self._model_size, device=self._device, compute_type=self._compute_type)
            log.info("whisper model ready")
        return self._model

    async def transcribe(self, audio_path: str, *, language: str | None = None,
                         on_progress=None) -> Transcript:
        model = await self._load()

        def run() -> tuple[list[TranscriptSegment], Any]:
            segments_iter, info = model.transcribe(
                audio_path,
                language=language,
                vad_filter=self._vad_filter,
                # Sensible speech defaults; these cut hallucinated repeats on silence.
                beam_size=5,
                condition_on_previous_text=False,
            )
            collected: list[TranscriptSegment] = []
            for segment in segments_iter:
                text = (segment.text or "").strip()
                if text:
                    collected.append(TranscriptSegment(
                        start=float(segment.start), end=float(segment.end), text=text))
            return collected, info

        loop = asyncio.get_running_loop()
        # faster-whisper yields lazily, so progress is reported after the fact; the
        # worker's stage updates carry the user-visible progress.
        if on_progress:
            await on_progress(0.05)
        segments, info = await loop.run_in_executor(None, run)
        if on_progress:
            await on_progress(1.0)

        duration = float(getattr(info, "duration", 0.0) or (segments[-1].end if segments else 0.0))
        return Transcript(
            text=" ".join(s.text for s in segments),
            segments=segments,
            language=getattr(info, "language", language),
            duration=duration,
            model=self._model_size,
        )

    async def health(self) -> bool:
        try:
            import faster_whisper  # noqa: F401
            return True
        except ImportError:
            return False
