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

        A job is runnable when it is queued (and any retry backoff has elapsed), or
        when it is running but its lease has expired — which is how a job survives a
        worker being killed mid-run. Jobs whose final attempt was claimed by a dead
        worker are moved to failed first; otherwise they would block the queue as an
        invisible zombie that ``/process`` keeps reporting as running.
        """
        now = utcnow()
        lease_cutoff = now - timedelta(seconds=self._settings.job_lease_seconds)

        await session.execute(
            update(ProcessingJob)
            .where(
                ProcessingJob.status == JobStatus.running,
                ProcessingJob.locked_at < lease_cutoff,
                ProcessingJob.attempts >= self._settings.job_max_attempts,
            )
            .values(status=JobStatus.failed, stage="failed", error=(
                        "the worker stopped before finishing the final attempt"),
                    finished_at=now, updated_at=now, locked_at=None, locked_by=None)
        )

        statement = (
            select(ProcessingJob)
            .where(
                ((ProcessingJob.status == JobStatus.queued)
                 & ((ProcessingJob.locked_at.is_(None)) | (ProcessingJob.locked_at <= now)))
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
        # locked_at doubles as a heartbeat: progress reports prove the worker is
        # alive, so a long transcription is not reclaimed underneath it.
        values: dict = {"stage": stage, "updated_at": utcnow(), "locked_at": utcnow()}
        if progress is not None:
            values["progress"] = max(0.0, min(1.0, progress))
        await session.execute(update(ProcessingJob).where(ProcessingJob.id == job_id).values(**values))

    async def complete(self, session: AsyncSession, job_id: str,
                       worker_id: str | None = None) -> None:
        statement = update(ProcessingJob).where(ProcessingJob.id == job_id)
        if worker_id is not None:
            # A worker whose lease expired and was reclaimed must not overwrite the
            # new owner's result.
            statement = statement.where(ProcessingJob.locked_by == worker_id)
        result = await session.execute(
            statement.values(status=JobStatus.completed, stage="completed", progress=1.0,
                             finished_at=utcnow(), updated_at=utcnow(),
                             locked_at=None, locked_by=None))
        if result.rowcount == 0:
            log.warning("job %s is no longer owned by %s; not completing it", job_id, worker_id)

    async def fail(self, session: AsyncSession, job_id: str, error: str, *,
                   retry: bool, worker_id: str | None = None) -> None:
        """A retryable failure goes back to queued; anything else is terminal."""
        job = await session.get(ProcessingJob, job_id)
        if job is None:
            return
        if worker_id is not None and job.locked_by != worker_id:
            log.warning("job %s is owned by %s, not %s; not failing it",
                        job_id, job.locked_by, worker_id)
            return
        exhausted = job.attempts >= self._settings.job_max_attempts
        if retry and not exhausted:
            job.status = JobStatus.queued
            job.stage = "queued"
            job.progress = 0.0
            # Hold the retry back so a provider outage does not burn every attempt
            # inside a few seconds. claim() skips a queued job until this passes.
            job.locked_at = utcnow() + timedelta(seconds=30 * job.attempts)
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
