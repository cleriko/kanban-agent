from __future__ import annotations

import asyncio
import logging
import os
import signal
import socket
import sys

from sqlalchemy import update

from ..ai.gateway import get_gateway
from ..config import get_settings
from ..db.models import JobStatus, ProcessingJob, utcnow
from ..db.session import create_all, dispose, session_scope
from ..infrastructure import logging as log_config
from ..infrastructure.queue import JobQueue
from ..infrastructure.storage import get_storage
from ..services.analysis_service import sanitise_error
from ..services.errors import NotFound
from .meeting_worker import MeetingProcessor


def _host_of(url: str) -> str:
    """Host:port from a SQLAlchemy URL, with the password stripped."""
    try:
        return url.split("://", 1)[1].split("/", 1)[0].rsplit("@", 1)[-1] or "?"
    except (IndexError, AttributeError):
        return "?"

log = logging.getLogger("worker")


class Worker:
    """Claims jobs and runs them. Separate process from the API on purpose: model
    inference must never compete with request handling for the event loop."""

    def __init__(self) -> None:
        self._settings = get_settings()
        self._queue = JobQueue(self._settings)
        self._storage = get_storage()
        self._gateway = get_gateway()
        self._processor = MeetingProcessor(self._settings, self._gateway, self._storage, self._queue)
        self._id = f"{socket.gethostname()}:{os.getpid()}"
        self._stopping = asyncio.Event()

    def request_stop(self) -> None:
        log.info("shutdown requested; finishing the current job")
        self._stopping.set()

    async def run(self) -> None:
        log_config.configure(self._settings.log_level)
        if self._settings.is_sqlite:
            await create_all()

        requeued = await self._requeue_own_jobs()
        if requeued:
            log.info("requeued %d job(s) this worker left running before the restart",
                     requeued)

        log.info("worker %s ready · db=%s · stt=%s · llm=%s",
                 self._id, _host_of(self._settings.database_url),
                 self._settings.transcription_provider, self._settings.llm_provider)

        present = sorted(k for k in os.environ if k.startswith("WC_"))
        if present:
            log.info("WC_* variables present: %s", ", ".join(present))
        else:
            log.error("No WC_* environment variables are set in this container. The "
                      "environment is not reaching the process.")
        if _host_of(self._settings.database_url).startswith(("localhost", "127.0.0.1")):
            log.error("WC_DATABASE_URL is not set; the worker cannot reach the queue and "
                      "will never pick up a job.")

        try:
            while not self._stopping.is_set():
                try:
                    claimed = await self._tick()
                except Exception:  # noqa: BLE001
                    # A database blip, a pool timeout or a failure in the failure
                    # handler must not kill the process and strand the current job.
                    log.exception("worker tick failed; retrying after a short pause")
                    claimed = False
                if not claimed:
                    # Nothing to do: sleep rather than spin. This is why an idle
                    # worker costs effectively nothing.
                    try:
                        await asyncio.wait_for(self._stopping.wait(),
                                               timeout=self._settings.job_poll_seconds)
                    except asyncio.TimeoutError:
                        pass
        finally:
            await dispose()
            log.info("worker %s stopped", self._id)

    async def _tick(self) -> bool:
        async with session_scope() as session:
            job = await self._queue.claim(session, self._id)
            if job is None:
                return False
            job_id, kind = job.id, job.kind

        log.info("claimed job %s (%s)", job_id, kind)

        # Run in a fresh transaction so a long job never holds the claim's row lock.
        async with session_scope() as session:
            job = await session.get(ProcessingJob, job_id)
            if job is None:
                return True
            timeout = self._settings.job_timeout_seconds
            try:
                if kind == "process_meeting":
                    try:
                        await asyncio.wait_for(self._processor.run(session, job),
                                               timeout=timeout)
                    except asyncio.TimeoutError:
                        # wait_for has already cancelled the processor and awaited its
                        # cleanup; surface a retryable message instead of an empty one.
                        raise TimeoutError(
                            f"job exceeded its {timeout:g}s timeout and was cancelled"
                        ) from None
                else:
                    raise ValueError(f"unknown job kind '{kind}'")
                await self._queue.complete(session, job_id, self._id)
                log.info("job %s completed", job_id)
            except Exception as exc:  # noqa: BLE001
                # Provider messages can quote model output; the full traceback goes to
                # debug and only a short single line reaches the log and the job row.
                detail = sanitise_error(exc)
                log.debug("job %s failed", job_id, exc_info=True)
                log.error("job %s failed: %s", job_id, detail)
                # The failed statement may have aborted the transaction; roll back so
                # recording the failure runs on a clean connection.
                await session.rollback()
                # Model and network failures are worth another attempt; bad data or a
                # deleted meeting is not.
                retry = not isinstance(exc, (ValueError, FileNotFoundError, NotFound))
                try:
                    await self._queue.fail(session, job_id, detail, retry=retry,
                                           worker_id=self._id)
                except Exception:  # noqa: BLE001
                    log.exception("could not record failure for job %s", job_id)
                    await session.rollback()
        return True

    async def _requeue_own_jobs(self) -> int:
        """Takes back jobs left running by this same worker id.

        A redeploy reuses the container name, so waiting out the lease (~30 min)
        would idle work that no other worker will touch until it expires.
        """
        async with session_scope() as session:
            result = await session.execute(
                update(ProcessingJob)
                .where(ProcessingJob.status == JobStatus.running,
                       ProcessingJob.locked_by == self._id)
                .values(status=JobStatus.queued, stage="queued", progress=0.0,
                        locked_at=None, locked_by=None, updated_at=utcnow()))
            return int(result.rowcount or 0)


def main() -> int:
    worker = Worker()
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, worker.request_stop)
        except NotImplementedError:
            pass

    try:
        loop.run_until_complete(worker.run())
    except KeyboardInterrupt:
        pass
    finally:
        loop.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
