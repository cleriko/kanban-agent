from __future__ import annotations

import logging
import math
import uuid
from collections.abc import AsyncIterator
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..api.schemas import (
    ActionItemDTO, CreateMeetingBody, MeetingDTO, TranscriptDTO, TranscriptSegmentDTO,
)
from ..db.models import (
    ActionItemState, Meeting, MeetingActionItem, MeetingDecision, MeetingStatus,
    MeetingTranscript, ProcessingJob, Task, utcnow,
)
from ..infrastructure.storage import ObjectStorage
from . import dates
from .errors import Conflict, Invalid, NotFound

log = logging.getLogger(__name__)


class MeetingService:
    """Meeting records, audio objects and analysis results.

    Audio is written to object storage and referenced by key; the database only ever
    holds metadata. Deleting a meeting removes both.
    """

    def __init__(self, session: AsyncSession, storage: ObjectStorage) -> None:
        self._session = session
        self._storage = storage

    # --- Reads ------------------------------------------------------------

    async def get(self, meeting_id: str) -> Meeting:
        meeting = await self._session.get(Meeting, meeting_id)
        if meeting is None:
            raise NotFound(f"no meeting with id {meeting_id}")
        return meeting

    async def lock(self, meeting_id: str) -> Meeting:
        """Loads a meeting while holding its row, serialising PROCESS.

        Two quick presses (or a client retry) must enqueue one job, not two. SQLite
        ignores FOR UPDATE, but its single-writer model makes the same guarantee.
        """
        statement = select(Meeting).where(Meeting.id == meeting_id).with_for_update()
        meeting = (await self._session.execute(statement)).scalar_one_or_none()
        if meeting is None:
            raise NotFound(f"no meeting with id {meeting_id}")
        return meeting

    async def list(self, *, limit: int = 100, query: str | None = None) -> list[Meeting]:
        statement = select(Meeting)
        if query:
            # Filter in SQL before LIMIT; filtering after it silently returned nothing
            # when the newest rows did not match.
            escaped = query.lower().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            pattern = f"%{escaped}%"
            statement = statement.where(
                func.lower(Meeting.title).like(pattern, escape="\\")
                | func.lower(Meeting.summary).like(pattern, escape="\\"))
        statement = statement.order_by(Meeting.started_at.desc()).limit(max(1, limit))
        return list((await self._session.execute(statement)).scalars().all())

    async def transcript(self, meeting_id: str) -> MeetingTranscript:
        statement = select(MeetingTranscript).where(MeetingTranscript.meeting_id == meeting_id)
        transcript = (await self._session.execute(statement)).scalar_one_or_none()
        if transcript is None:
            raise NotFound(f"no transcript for meeting {meeting_id}")
        return transcript

    async def action_items(self, meeting_id: str) -> list[MeetingActionItem]:
        await self.get(meeting_id)
        statement = (select(MeetingActionItem)
                     .where(MeetingActionItem.meeting_id == meeting_id)
                     .order_by(MeetingActionItem.position))
        return list((await self._session.execute(statement)).scalars().all())

    # --- Writes -----------------------------------------------------------

    async def create(self, body: CreateMeetingBody) -> Meeting:
        meeting_id = body.id or str(uuid.uuid4())
        if body.id:
            try:
                uuid.UUID(meeting_id)
            except ValueError:
                # The id becomes part of an object-storage key; only accept UUIDs.
                raise Invalid("meeting id must be a UUID") from None
        existing = await self._session.get(Meeting, meeting_id)
        if existing is not None:
            # Retried upload after a dropped connection: reuse the record, and accept
            # corrected metadata from the replay so a retry also fixes the clock.
            existing.title = body.title or existing.title
            if body.duration:
                existing.duration = body.duration
            if body.startedAt:
                started = dates.parse(body.startedAt)
                if started is not None:
                    existing.started_at = started
            await self._session.flush()
            await self._session.refresh(existing)
            return existing

        started = dates.parse(body.startedAt) if body.startedAt else None
        meeting = Meeting(
            id=meeting_id,
            title=(body.title or "Meeting").strip(),
            started_at=started or datetime.now(timezone.utc),
            duration=body.duration or 0.0,
            status=MeetingStatus.created,
        )
        self._session.add(meeting)
        await self._session.flush()
        # Relationships are not loaded on a freshly flushed object; serialising one
        # would otherwise trigger lazy IO outside the async context.
        await self._session.refresh(meeting)
        return meeting

    async def store_audio(self, meeting_id: str, stream: AsyncIterator[bytes],
                          content_type: str = "audio/mp4") -> Meeting:
        meeting = await self.get(meeting_id)
        if meeting.status in (MeetingStatus.transcribing, MeetingStatus.analyzing):
            raise Conflict("this meeting is already being processed")

        key = ObjectStorage.audio_key(meeting_id)
        # Release the request's database connection while the body streams. A slow
        # upload otherwise pins a pool slot for its whole duration, and five of them
        # starve every other endpoint.
        await self._session.commit()
        written = await self._storage.put(key, stream, content_type)
        if written == 0:
            # Do not leave a zero-byte object behind: a retry would overwrite it, but
            # a failed first upload should leave no trace at all.
            try:
                await self._storage.delete(key)
            except Exception:  # noqa: BLE001
                log.warning("could not remove empty upload for meeting %s", meeting_id)
            raise Invalid("the uploaded recording was empty")

        meeting = await self.get(meeting_id)
        meeting.audio_key = key
        meeting.audio_bytes = written
        meeting.audio_content_type = content_type
        meeting.error = None
        await self._session.flush()
        await self._session.refresh(meeting)
        log.info("stored %s bytes of audio for meeting %s", written, meeting_id)
        return meeting

    async def set_status(self, meeting_id: str, status: MeetingStatus,
                         error: str | None = None) -> Meeting:
        meeting = await self.get(meeting_id)
        meeting.status = status
        meeting.error = error
        meeting.updated_at = utcnow()
        if status == MeetingStatus.completed:
            meeting.processed_at = utcnow()
        await self._session.flush()
        await self._session.refresh(meeting)
        return meeting

    async def save_transcript(self, meeting_id: str, *, text: str, segments: list[dict[str, Any]],
                              language: str | None, duration: float,
                              provider: str, model: str | None) -> MeetingTranscript:
        await self.get(meeting_id)
        existing = (await self._session.execute(
            select(MeetingTranscript).where(MeetingTranscript.meeting_id == meeting_id)
        )).scalar_one_or_none()

        if existing is None:
            existing = MeetingTranscript(meeting_id=meeting_id)
            self._session.add(existing)

        existing.text = text
        existing.segments = segments
        existing.language = language
        existing.duration = duration
        existing.provider = provider
        existing.model = model
        await self._session.flush()
        return existing

    async def save_analysis(self, meeting_id: str, analysis: dict[str, Any]) -> Meeting:
        """Replaces generated content, and deliberately leaves user decisions alone:
        an action item the user already accepted or dismissed keeps its state."""
        meeting = await self.get(meeting_id)

        meeting.summary = str(analysis.get("summary") or "")
        meeting.key_points = _string_list(analysis.get("keyPoints"))
        meeting.decisions = _string_list(analysis.get("decisions"))
        meeting.questions = _string_list(analysis.get("questions"))
        meeting.topics = _string_list(analysis.get("topics"))
        meeting.participants = _string_list(analysis.get("participants"))

        # Decisions also get rows, so the agent can retrieve them individually.
        for row in list(meeting.meeting_decisions):
            await self._session.delete(row)
        for index, text in enumerate(meeting.decisions):
            self._session.add(MeetingDecision(meeting_id=meeting_id, text=text, position=index))

        decided = {item.title.strip().lower(): item
                   for item in meeting.action_items if item.state != ActionItemState.suggested}
        next_position = max((item.position for item in meeting.action_items), default=-1.0) + 1
        for item in list(meeting.action_items):
            if item.state == ActionItemState.suggested:
                await self._session.delete(item)

        for index, raw in enumerate(analysis.get("actionItems") or []):
            if not isinstance(raw, dict):
                continue
            title = str(raw.get("title") or "").strip()
            if not title:
                continue
            if title.lower() in decided:
                continue   # already accepted or dismissed by the user
            assignee = raw.get("assignee")
            try:
                confidence = float(raw.get("confidence") or 0.0)
            except (TypeError, ValueError):
                confidence = 0.0
            if not math.isfinite(confidence):
                confidence = 0.0
            confidence = max(0.0, min(1.0, confidence))
            due_raw = raw.get("dueDate")
            self._session.add(MeetingActionItem(
                meeting_id=meeting_id,
                title=title[:500],
                assignee=(str(assignee)[:200] if assignee else None),
                due_date=dates.parse(due_raw) if isinstance(due_raw, str) else None,
                confidence=confidence,
                state=ActionItemState.suggested,
                position=next_position + index,
            ))

        meeting.updated_at = utcnow()
        await self._session.flush()
        await self._session.refresh(meeting)
        return meeting

    async def set_action_item_state(self, item_id: str, state: ActionItemState,
                                    task_id: str | None = None) -> MeetingActionItem:
        item = await self._session.get(MeetingActionItem, item_id)
        if item is None:
            raise NotFound(f"no action item with id {item_id}")
        item.state = state
        item.task_id = task_id
        await self._session.flush()
        return item

    async def delete(self, meeting_id: str) -> None:
        meeting = await self.get(meeting_id)
        key_prefix = ObjectStorage.meeting_prefix(meeting_id)
        await self._session.delete(meeting)
        # Commit before touching storage. A commit failure afterwards would otherwise
        # leave a row pointing at audio that no longer exists.
        await self._session.commit()
        # Storage last: a failure here leaves an orphan object, not a broken database.
        try:
            await self._storage.delete_prefix(key_prefix)
        except Exception:  # noqa: BLE001
            log.warning("could not remove stored audio for meeting %s", meeting_id, exc_info=True)

    async def discard_audio(self, meeting_id: str) -> None:
        """Audio is a transport artefact; drop it once results are stored."""
        meeting = await self.get(meeting_id)
        if not meeting.audio_key:
            return
        try:
            await self._storage.delete(meeting.audio_key)
        except Exception:  # noqa: BLE001
            # Keep the key: clearing it while the object still exists would leave an
            # orphan that nothing can find again.
            log.warning("could not delete audio for meeting %s; keeping its key",
                        meeting_id, exc_info=True)
            return
        meeting.audio_key = None
        await self._session.flush()

    async def latest_job(self, meeting_id: str) -> ProcessingJob | None:
        statement = (select(ProcessingJob)
                     .where(ProcessingJob.meeting_id == meeting_id)
                     .order_by(ProcessingJob.created_at.desc())
                     .limit(1))
        return (await self._session.execute(statement)).scalar_one_or_none()


# --- Serialisation ----------------------------------------------------------


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(v).strip() for v in value if str(v).strip()]


def action_item_dto(item: MeetingActionItem) -> ActionItemDTO:
    return ActionItemDTO(
        id=item.id,
        title=item.title,
        assignee=item.assignee,
        dueDate=dates.iso(item.due_date),
        confidence=item.confidence,
        state=item.state.value,
        taskId=item.task_id,
    )


def meeting_dto(meeting: Meeting, *, transcript: MeetingTranscript | None = None,
                job: ProcessingJob | None = None, include_transcript: bool = False) -> MeetingDTO:
    transcript_dto: TranscriptDTO | None = None
    if include_transcript and transcript is not None:
        transcript_dto = TranscriptDTO(
            segments=[TranscriptSegmentDTO(
                id=segment.get("id"),
                start=float(segment.get("start", 0)),
                end=float(segment.get("end", 0)),
                speaker=segment.get("speaker"),
                text=str(segment.get("text", "")),
            ) for segment in (transcript.segments or [])],
            language=transcript.language,
            duration=transcript.duration,
        )

    return MeetingDTO(
        id=meeting.id,
        title=meeting.title,
        startedAt=dates.iso(meeting.started_at) or "",
        duration=meeting.duration,
        status=meeting.status.value,
        summary=meeting.summary or "",
        keyPoints=[str(x) for x in (meeting.key_points or [])],
        decisions=[str(x) for x in (meeting.decisions or [])],
        questions=[str(x) for x in (meeting.questions or [])],
        topics=[str(x) for x in (meeting.topics or [])],
        participants=[str(x) for x in (meeting.participants or [])],
        actionItems=[action_item_dto(i) for i in meeting.action_items],
        transcript=transcript_dto,
        jobId=job.id if job else None,
        error=meeting.error,
    )
