from __future__ import annotations

import asyncio
import logging
import os
import signal
import socket
import sys

from ..ai.gateway import get_gateway
from ..config import get_settings
from ..db.session import create_all, dispose, session_scope
from ..infrastructure import logging as log_config
from ..infrastructure.queue import JobQueue
from ..infrastructure.storage import get_storage
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
                claimed = await self._tick()
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
            from ..db.models import ProcessingJob
            job = await session.get(ProcessingJob, job_id)
            if job is None:
                return True
            try:
                if kind == "process_meeting":
                    await self._processor.run(session, job)
                else:
                    raise ValueError(f"unknown job kind '{kind}'")
                await self._queue.complete(session, job_id)
                log.info("job %s completed", job_id)
            except Exception as exc:  # noqa: BLE001
                log.exception("job %s failed", job_id)
                # Model and network failures are worth another attempt; bad data is not.
                retry = not isinstance(exc, (ValueError, FileNotFoundError))
                await self._queue.fail(session, job_id, str(exc), retry=retry)
        return True


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
