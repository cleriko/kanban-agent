from __future__ import annotations

import logging
from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncSession

from ..ai.gateway import AIGateway
from ..ai.providers.base import ProviderError
from ..config import Settings
from ..db.models import MeetingStatus, ProcessingJob
from ..infrastructure.queue import JobQueue
from ..infrastructure.storage import ObjectStorage
from ..services.analysis_service import AnalysisService
from ..services.meeting_service import MeetingService

log = logging.getLogger(__name__)


class MeetingProcessor:
    """Audio → transcript → analysis → database.

    Stages are reported to the job row as they happen, which is what the Mac renders
    as its progress checklist. A failure in analysis keeps the transcript: the useful
    half of the work is not thrown away because the second half failed.
    """

    def __init__(self, settings: Settings, gateway: AIGateway,
                 storage: ObjectStorage, queue: JobQueue) -> None:
        self._settings = settings
        self._gateway = gateway
        self._storage = storage
        self._queue = queue
        self._analysis = AnalysisService(gateway, settings)

    async def run(self, session: AsyncSession, job: ProcessingJob) -> None:
        if not job.meeting_id:
            raise ValueError("process_meeting job has no meeting")

        meetings = MeetingService(session, self._storage)
        meeting = await meetings.get(job.meeting_id)
        if not meeting.audio_key:
            raise ValueError("meeting has no stored audio")

        # --- Transcription ------------------------------------------------
        await self._stage(session, job, "transcribing", 0.05, MeetingStatus.transcribing, meetings)

        audio_path = await self._storage.get_path(meeting.audio_key)
        is_temporary = not str(audio_path).startswith(str(self._settings.storage_path))

        try:
            async def progress(fraction: float) -> None:
                await self._queue.set_stage(session, job.id, "transcribing", 0.05 + fraction * 0.55)
                await session.commit()

            transcript = await self._gateway.transcription.transcribe(
                audio_path, language=self._settings.whisper_language or None, on_progress=progress)
        finally:
            if is_temporary:
                Path(audio_path).unlink(missing_ok=True)

        segments = [segment.to_dict() for segment in transcript.segments]
        await meetings.save_transcript(
            meeting.id,
            text=transcript.text,
            segments=segments,
            language=transcript.language,
            duration=transcript.duration,
            provider=self._gateway.transcription.name,
            model=transcript.model)

        # Trust the measured duration over whatever the client reported.
        if transcript.duration:
            meeting.duration = transcript.duration
        await session.commit()
        log.info("meeting %s transcribed: %s segments", meeting.id, len(segments))

        # --- Analysis -----------------------------------------------------
        await self._stage(session, job, "analyzing", 0.65, MeetingStatus.analyzing, meetings)

        try:
            async def analysis_progress(fraction: float) -> None:
                await self._queue.set_stage(session, job.id, "analyzing", 0.65 + fraction * 0.3)
                await session.commit()

            analysis = await self._analysis.analyse(
                title=meeting.title,
                started_at=meeting.started_at,
                duration=meeting.duration,
                transcript=transcript.text,
                on_progress=analysis_progress)
        except ProviderError as exc:
            # Keep the transcript; surface the analysis failure.
            await meetings.set_status(meeting.id, MeetingStatus.failed, str(exc))
            await session.commit()
            raise

        await meetings.save_analysis(meeting.id, analysis)
        await meetings.set_status(meeting.id, MeetingStatus.completed)

        # The recording was a transport artefact; the results replace it.
        await meetings.discard_audio(meeting.id)

        await self._queue.set_stage(session, job.id, "completed", 1.0)
        await session.commit()
        log.info("meeting %s analysed: %s action item(s)",
                 meeting.id, len(analysis.get("actionItems") or []))

    async def _stage(self, session: AsyncSession, job: ProcessingJob, stage: str,
                     progress: float, status: MeetingStatus, meetings: MeetingService) -> None:
        await self._queue.set_stage(session, job.id, stage, progress)
        await meetings.set_status(job.meeting_id or "", status)
        await session.commit()
