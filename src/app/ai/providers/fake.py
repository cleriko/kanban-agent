from __future__ import annotations

import json
from typing import Any

from .base import LLMProvider, Transcript, TranscriptionProvider, TranscriptSegment


class FakeTranscriptionProvider(TranscriptionProvider):
    """Deterministic transcript for tests and for bringing the stack up before a
    model is installed. Never used unless explicitly configured."""

    name = "fake"

    def __init__(self, segments: list[tuple[float, float, str, str | None]] | None = None) -> None:
        self._segments = segments or [
            (0.0, 6.0, "We should launch the new onboarding next week.", "Alex"),
            (6.0, 12.0, "I can finish the new screens by Wednesday.", "Sarah"),
            (12.0, 18.0, "Let's decide to ship on Friday, and someone needs to review the API changes.", "Alex"),
        ]

    async def transcribe(self, audio_path: str, *, language: str | None = None,
                         on_progress=None) -> Transcript:
        if on_progress:
            await on_progress(1.0)
        segments = [TranscriptSegment(start=s, end=e, text=t, speaker=sp)
                    for s, e, t, sp in self._segments]
        return Transcript(
            text=" ".join(s.text for s in segments),
            segments=segments,
            language=language or "en",
            duration=segments[-1].end if segments else 0.0,
            model="fake",
        )


class FakeLLMProvider(LLMProvider):
    """Returns a fixed, schema-valid analysis. Lets the whole pipeline be tested
    without a model, which is the only way these tests can run in CI."""

    name = "fake"

    def __init__(self, response: dict[str, Any] | None = None) -> None:
        self.response = response or {
            "summary": "The team discussed the onboarding launch and remaining design work.",
            "keyPoints": ["Onboarding launch is close", "Design work finishes Wednesday"],
            "decisions": ["Ship on Friday"],
            "actionItems": [
                {"title": "Finish onboarding screens", "assignee": "Sarah",
                 "dueDate": None, "confidence": 0.91},
                {"title": "Review API changes", "assignee": None,
                 "dueDate": None, "confidence": 0.72},
            ],
            "questions": ["Who signs off on the launch?"],
            "topics": ["onboarding", "api"],
            "participants": ["Alex", "Sarah"],
        }
        self.calls: list[str] = []

    async def generate(self, prompt: str, *, system: str | None = None,
                       temperature: float | None = None) -> str:
        self.calls.append(prompt)
        return json.dumps(self.response)

    async def generate_structured(self, prompt: str, schema: dict[str, Any], *,
                                  system: str | None = None,
                                  temperature: float | None = None) -> dict[str, Any]:
        self.calls.append(prompt)
        return self.response
