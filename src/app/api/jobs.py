from __future__ import annotations

from fastapi import APIRouter
from fastapi.responses import StreamingResponse

from ..db.models import JobStatus, ProcessingJob
from ..db.session import session_scope
from ..infrastructure.sse import poll_stream
from ..services.errors import NotFound
from .deps import Authenticated, SessionDep, http_error
from .schemas import JobDTO

router = APIRouter(prefix="/jobs", tags=["jobs"], dependencies=[Authenticated])


def _to_dto(job: ProcessingJob) -> JobDTO:
    return JobDTO(id=job.id, status=job.status.value, stage=job.stage,
                  progress=job.progress, error=job.error, meetingId=job.meeting_id)


@router.get("/{job_id}", response_model=JobDTO)
async def get_job(job_id: str, session: SessionDep) -> JobDTO:
    job = await session.get(ProcessingJob, job_id)
    if job is None:
        raise http_error(NotFound(f"no job with id {job_id}"))
    return _to_dto(job)


@router.get("/{job_id}/events")
async def job_events(job_id: str) -> StreamingResponse:
    """Progress as server-sent events.

    One short-lived stream per running job, closed as soon as the job reaches a
    terminal state. The client falls back to polling this same resource if the
    stream cannot be opened.
    """

    async def fetch() -> dict | None:
        # Its own session per tick: this outlives the request's transaction.
        async with session_scope() as session:
            job = await session.get(ProcessingJob, job_id)
            return _to_dto(job).model_dump() if job else None

    def is_terminal(payload: dict) -> bool:
        return payload.get("status") in (JobStatus.completed.value, JobStatus.failed.value)

    return StreamingResponse(
        poll_stream(fetch, is_terminal=is_terminal, interval=1.0),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no", "Connection": "keep-alive"},
    )
