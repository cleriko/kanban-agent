from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any


@dataclass(slots=True)
class TranscriptSegment:
    start: float
    end: float
    text: str
    speaker: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {"start": round(self.start, 2), "end": round(self.end, 2),
                "text": self.text, "speaker": self.speaker}


@dataclass(slots=True)
class Transcript:
    text: str
    segments: list[TranscriptSegment] = field(default_factory=list)
    language: str | None = None
    duration: float = 0.0
    model: str | None = None


class TranscriptionProvider(ABC):
    """Speech to text. Implementations run on the VPS.

    The interface exists so the model can change — faster-whisper today, something
    else tomorrow — without the API, the workers or the Mac app knowing.
    """

    name: str = "base"

    @abstractmethod
    async def transcribe(self, audio_path: str, *, language: str | None = None,
                         on_progress=None) -> Transcript:
        """Transcribes a local audio file.

        `on_progress` is an optional async callable taking a 0..1 fraction; a long
        recording should report progress rather than going silent for ten minutes.
        """

    async def health(self) -> bool:
        return True


class LLMProvider(ABC):
    """Text generation. Implementations run on the VPS."""

    name: str = "base"

    @abstractmethod
    async def generate(self, prompt: str, *, system: str | None = None,
                       temperature: float | None = None) -> str: ...

    @abstractmethod
    async def generate_structured(self, prompt: str, schema: dict[str, Any], *,
                                  system: str | None = None,
                                  temperature: float | None = None) -> dict[str, Any]:
        """Returns JSON matching `schema`. Implementations are expected to enforce
        this at the model level where the runtime supports it, and to repair or raise
        rather than returning prose."""

    async def health(self) -> bool:
        return True


class ProviderError(RuntimeError):
    """Raised when a model is unreachable or returns something unusable."""
