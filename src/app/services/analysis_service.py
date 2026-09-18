from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from ..agent.prompts.analysis import (
    ANALYSIS_PROMPT, ANALYSIS_SCHEMA, ANALYSIS_SYSTEM, CHUNK_PROMPT, CHUNK_SYSTEM,
)
from ..ai.gateway import AIGateway
from ..ai.providers.base import ProviderError
from ..config import Settings

log = logging.getLogger(__name__)


class AnalysisService:
    """Transcript in, structured meeting intelligence out.

    Long transcripts are condensed in parts before the final pass, so an hour-long
    meeting does not silently fall out of the model's context window and produce a
    summary of only its last ten minutes.
    """

    def __init__(self, gateway: AIGateway, settings: Settings) -> None:
        self._gateway = gateway
        self._settings = settings

    async def analyse(self, *, title: str, started_at: datetime, duration: float,
                      transcript: str, on_progress=None) -> dict[str, Any]:
        text = (transcript or "").strip()
        if not text:
            # Nothing was said, or speech recognition found nothing. Say so rather
            # than asking a model to invent a summary of silence.
            return _empty_analysis("No speech was detected in this recording.")

        chunk_size = self._settings.analysis_chunk_chars
        if len(text) > chunk_size:
            text = await self._condense(title, text, chunk_size, on_progress)

        prompt = ANALYSIS_PROMPT.format(
            title=title,
            date=started_at.strftime("%A %d %B %Y"),
            duration=_duration_label(duration),
            transcript=text,
        )

        try:
            result = await self._gateway.llm.generate_structured(
                prompt, ANALYSIS_SCHEMA, system=ANALYSIS_SYSTEM)
        except ProviderError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise ProviderError(f"analysis failed: {exc}") from exc

        return _normalise(result)

    async def _condense(self, title: str, text: str, chunk_size: int, on_progress) -> str:
        chunks = _split(text, chunk_size)
        log.info("condensing a long transcript into %s parts", len(chunks))
        condensed: list[str] = []
        for index, chunk in enumerate(chunks, start=1):
            summary = await self._gateway.llm.generate(
                CHUNK_PROMPT.format(index=index, total=len(chunks), title=title, chunk=chunk),
                system=CHUNK_SYSTEM)
            condensed.append(summary.strip())
            if on_progress:
                await on_progress(index / (len(chunks) + 1))
        return "\n\n".join(condensed)


def _split(text: str, size: int) -> list[str]:
    """Splits on paragraph then sentence boundaries, so a chunk never ends mid-sentence."""
    if len(text) <= size:
        return [text]
    chunks: list[str] = []
    remaining = text
    while len(remaining) > size:
        window = remaining[:size]
        cut = max(window.rfind("\n\n"), window.rfind(". "), window.rfind("? "), window.rfind("! "))
        if cut < size // 2:
            cut = size
        chunks.append(remaining[:cut].strip())
        remaining = remaining[cut:].strip()
    if remaining:
        chunks.append(remaining)
    return chunks


def _duration_label(seconds: float) -> str:
    minutes = int(seconds // 60)
    if minutes < 60:
        return f"{minutes} minutes"
    return f"{minutes // 60}h {minutes % 60:02d}m"


def _empty_analysis(summary: str) -> dict[str, Any]:
    return {"summary": summary, "keyPoints": [], "decisions": [],
            "actionItems": [], "questions": [], "topics": [], "participants": []}


def _normalise(raw: Any) -> dict[str, Any]:
    """Models produce near-miss shapes; coerce rather than failing the whole job."""
    if not isinstance(raw, dict):
        return _empty_analysis("The analysis model returned an unexpected response.")

    def strings(key: str) -> list[str]:
        value = raw.get(key)
        if isinstance(value, str):
            return [value.strip()] if value.strip() else []
        if not isinstance(value, list):
            return []
        return [str(v).strip() for v in value if str(v).strip()]

    items: list[dict[str, Any]] = []
    for entry in (raw.get("actionItems") or []):
        if isinstance(entry, str):
            entry = {"title": entry}
        if not isinstance(entry, dict):
            continue
        title = str(entry.get("title") or "").strip()
        if not title:
            continue
        confidence = entry.get("confidence", 0.0)
        try:
            confidence = max(0.0, min(1.0, float(confidence)))
        except (TypeError, ValueError):
            confidence = 0.0
        assignee = entry.get("assignee")
        items.append({
            "title": title,
            "assignee": str(assignee).strip() if assignee else None,
            "dueDate": entry.get("dueDate") or None,
            "confidence": confidence,
        })

    return {
        "summary": str(raw.get("summary") or "").strip(),
        "keyPoints": strings("keyPoints"),
        "decisions": strings("decisions"),
        "actionItems": items,
        "questions": strings("questions"),
        "topics": strings("topics"),
        "participants": strings("participants"),
    }
