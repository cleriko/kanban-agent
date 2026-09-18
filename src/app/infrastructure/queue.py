from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import Settings
from ..db.models import JobStatus, ProcessingJob, utcnow

log = logging.getLogger(__name__)


class JobQueue:
    """A queue built on the jobs table.

    Postgres-backed rather than Redis-backed on purpose: the durable record of a job
    has to exist anyway (the client polls it, the UI renders it), and reusing that row
    as the queue removes a whole service from the deployment. ``FOR UPDATE SKIP
    LOCKED`` makes claiming safe across any number of workers.
    """

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    async def enqueue(self, session: AsyncSession, *, kind: str,
                      meeting_id: str | None = None) -> ProcessingJob:
        job = ProcessingJob(kind=kind, meeting_id=meeting_id, status=JobStatus.queued, stage="queued")
        session.add(job)
        await session.flush()
        return job

    async def claim(self, session: AsyncSession, worker_id: str) -> ProcessingJob | None:
        """Takes the oldest runnable job, or returns None.

        A job is runnable when it is queued, or when it is running but its lease has
        expired — which is how a job survives a worker being killed mid-run.
        """
        lease_cutoff = datetime.now(timezone.utc) - timedelta(seconds=self._settings.job_lease_seconds)

        statement = (
            select(ProcessingJob)
            .where(
                (ProcessingJob.status == JobStatus.queued)
                | ((ProcessingJob.status == JobStatus.running)
                   & (ProcessingJob.locked_at < lease_cutoff))
            )
            .where(ProcessingJob.attempts < self._settings.job_max_attempts)
            .order_by(ProcessingJob.created_at)
            .limit(1)
        )
        if not self._settings.is_sqlite:
            statement = statement.with_for_update(skip_locked=True)

        job = (await session.execute(statement)).scalar_one_or_none()
        if job is None:
            return None

        if job.status == JobStatus.running:
            log.warning("reclaiming abandoned job %s (attempt %s)", job.id, job.attempts + 1)

        job.status = JobStatus.running
        job.locked_at = utcnow()
        job.locked_by = worker_id
        job.attempts += 1
        job.error = None
        await session.flush()
        return job

    async def set_stage(self, session: AsyncSession, job_id: str, stage: str,
                        progress: float | None = None) -> None:
        values: dict = {"stage": stage, "updated_at": utcnow()}
        if progress is not None:
            values["progress"] = max(0.0, min(1.0, progress))
        await session.execute(update(ProcessingJob).where(ProcessingJob.id == job_id).values(**values))

    async def complete(self, session: AsyncSession, job_id: str) -> None:
        await session.execute(
            update(ProcessingJob)
            .where(ProcessingJob.id == job_id)
            .values(status=JobStatus.completed, stage="completed", progress=1.0,
                    finished_at=utcnow(), updated_at=utcnow(), locked_at=None, locked_by=None))

    async def fail(self, session: AsyncSession, job_id: str, error: str, *, retry: bool) -> None:
        """A retryable failure goes back to queued; anything else is terminal."""
        job = await session.get(ProcessingJob, job_id)
        if job is None:
            return
        exhausted = job.attempts >= self._settings.job_max_attempts
        if retry and not exhausted:
            job.status = JobStatus.queued
            job.stage = "queued"
            job.locked_at = None
            job.locked_by = None
        else:
            job.status = JobStatus.failed
            job.stage = "failed"
            job.finished_at = utcnow()
            job.locked_at = None
            job.locked_by = None
        job.error = error[:2000]
        job.updated_at = utcnow()
        await session.flush()
