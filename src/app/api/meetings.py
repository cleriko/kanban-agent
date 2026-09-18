from __future__ import annotations

import logging

from fastapi import APIRouter, Request, Response, status

from ..db.models import MeetingStatus
from ..services.errors import Invalid, ServiceError
from ..services.meeting_service import MeetingService, action_item_dto, meeting_dto
from .deps import Authenticated, QueueDep, SessionDep, StorageDep, http_error
from .schemas import ActionItemDTO, CreateMeetingBody, JobDTO, MeetingDTO, TranscriptDTO

log = logging.getLogger(__name__)

router = APIRouter(prefix="/meetings", tags=["meetings"], dependencies=[Authenticated])

# 4 hours of 64 kbps mono AAC is roughly 115 MB; anything larger is a mistake.
MAX_AUDIO_BYTES = 512 * 1024 * 1024


@router.get("", response_model=list[MeetingDTO])
async def list_meetings(session: SessionDep, storage: StorageDep,
                        limit: int = 100, q: str | None = None) -> list[MeetingDTO]:
    service = MeetingService(session, storage)
    meetings = await service.list(limit=limit, query=q)
    return [meeting_dto(m) for m in meetings]


@router.post("", response_model=MeetingDTO, status_code=status.HTTP_201_CREATED)
async def create_meeting(body: CreateMeetingBody, session: SessionDep,
                         storage: StorageDep) -> MeetingDTO:
    try:
        meeting = await MeetingService(session, storage).create(body)
    except ServiceError as error:
        raise http_error(error) from error
    return meeting_dto(meeting)


@router.get("/{meeting_id}", response_model=MeetingDTO)
async def get_meeting(meeting_id: str, session: SessionDep, storage: StorageDep,
                      includeTranscript: bool = True) -> MeetingDTO:
    service = MeetingService(session, storage)
    try:
        meeting = await service.get(meeting_id)
        job = await service.latest_job(meeting_id)
        transcript = None
        if includeTranscript:
            try:
                transcript = await service.transcript(meeting_id)
            except ServiceError:
                transcript = None
    except ServiceError as error:
        raise http_error(error) from error
    return meeting_dto(meeting, transcript=transcript, job=job, include_transcript=includeTranscript)


@router.delete("/{meeting_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_meeting(meeting_id: str, session: SessionDep, storage: StorageDep) -> Response:
    try:
        await MeetingService(session, storage).delete(meeting_id)
    except ServiceError as error:
        raise http_error(error) from error
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.put("/{meeting_id}/audio", response_model=MeetingDTO)
async def upload_audio(meeting_id: str, request: Request, session: SessionDep,
                       storage: StorageDep) -> MeetingDTO:
    """Streams the recording straight into object storage.

    The body is read in chunks rather than buffered, so a long meeting does not have
    to fit in the API process's memory.
    """
    content_type = request.headers.get("content-type", "audio/mp4")
    declared = int(request.headers.get("content-length") or 0)
    if declared > MAX_AUDIO_BYTES:
        raise http_error(Invalid(f"recording is too large ({declared} bytes)"))

    total = 0

    async def stream():
        nonlocal total
        async for chunk in request.stream():
            total += len(chunk)
            if total > MAX_AUDIO_BYTES:
                raise Invalid("recording exceeded the maximum size")
            yield chunk

    try:
        meeting = await MeetingService(session, storage).store_audio(meeting_id, stream(), content_type)
    except ServiceError as error:
        raise http_error(error) from error
    return meeting_dto(meeting)


@router.post("/{meeting_id}/process", response_model=JobDTO,
             status_code=status.HTTP_202_ACCEPTED)
async def process_meeting(meeting_id: str, session: SessionDep, storage: StorageDep,
                          queue: QueueDep) -> JobDTO:
    """Creates a job and returns immediately.

    Transcription and analysis take minutes; doing them inside a request would tie up
    a worker and time out the client.
    """
    service = MeetingService(session, storage)
    try:
        meeting = await service.get(meeting_id)
        if not meeting.audio_key:
            raise Invalid("no audio has been uploaded for this meeting")

        existing = await service.latest_job(meeting_id)
        if existing is not None and existing.status.value in ("queued", "running"):
            return JobDTO(id=existing.id, status=existing.status.value, stage=existing.stage,
                          progress=existing.progress, meetingId=meeting_id)

        job = await queue.enqueue(session, kind="process_meeting", meeting_id=meeting_id)
        await service.set_status(meeting_id, MeetingStatus.queued)
    except ServiceError as error:
        raise http_error(error) from error

    return JobDTO(id=job.id, status=job.status.value, stage=job.stage,
                  progress=job.progress, meetingId=meeting_id)


@router.get("/{meeting_id}/transcript", response_model=TranscriptDTO)
async def get_transcript(meeting_id: str, session: SessionDep, storage: StorageDep) -> TranscriptDTO:
    try:
        transcript = await MeetingService(session, storage).transcript(meeting_id)
    except ServiceError as error:
        raise http_error(error) from error
    from .schemas import TranscriptSegmentDTO
    return TranscriptDTO(
        segments=[TranscriptSegmentDTO(id=s.get("id"), start=float(s.get("start", 0)),
                                       end=float(s.get("end", 0)), speaker=s.get("speaker"),
                                       text=str(s.get("text", "")))
                  for s in (transcript.segments or [])],
        language=transcript.language,
        duration=transcript.duration)


@router.get("/{meeting_id}/action-items", response_model=list[ActionItemDTO])
async def get_action_items(meeting_id: str, session: SessionDep,
                           storage: StorageDep) -> list[ActionItemDTO]:
    try:
        items = await MeetingService(session, storage).action_items(meeting_id)
    except ServiceError as error:
        raise http_error(error) from error
    return [action_item_dto(i) for i in items]
