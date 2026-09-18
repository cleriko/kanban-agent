from __future__ import annotations

import json

import pytest

from app.ai.gateway import get_gateway
from app.ai.providers.base import ProviderError
from app.ai.providers.fake import FakeLLMProvider, FakeTranscriptionProvider
from app.config import get_settings
from app.db.models import ActionItemState, JobStatus, MeetingStatus, ProcessingJob
from app.db.session import session_scope
from app.infrastructure.queue import JobQueue
from app.infrastructure.storage import get_storage
from app.workers.meeting_worker import MeetingProcessor

pytestmark = pytest.mark.asyncio

AUDIO = b"\x00\x01" * 4096   # stand-in bytes; the fake provider never decodes them


async def _run_worker() -> ProcessingJob:
    """Runs exactly one job, the same way the worker process does."""
    settings = get_settings()
    queue = JobQueue(settings)
    processor = MeetingProcessor(settings, get_gateway(), get_storage(), queue)

    async with session_scope() as session:
        job = await queue.claim(session, "test-worker")
        assert job is not None, "expected a queued job"
        job_id = job.id

    async with session_scope() as session:
        job = await session.get(ProcessingJob, job_id)
        try:
            await processor.run(session, job)
            await queue.complete(session, job_id)
        except Exception as exc:  # noqa: BLE001
            await queue.fail(session, job_id, str(exc), retry=False)

    async with session_scope() as session:
        return await session.get(ProcessingJob, job_id)


async def _upload(client, api, *, title="Product Sync") -> str:
    created = await client.post(f"{api}/meetings", json={
        "title": title, "startedAt": "2026-09-18T10:30:00Z", "duration": 2532})
    meeting_id = created.json()["id"]
    uploaded = await client.put(f"{api}/meetings/{meeting_id}/audio", content=AUDIO,
                                headers={"Content-Type": "audio/mp4"})
    assert uploaded.status_code == 200
    return meeting_id


# --- Pipeline ---------------------------------------------------------------


async def test_full_meeting_pipeline(client, api, fake_llm, storage):
    meeting_id = await _upload(client, api)

    # Audio is in object storage, not the database.
    assert await storage.exists(f"meetings/{meeting_id}/original.m4a")

    accepted = await client.post(f"{api}/meetings/{meeting_id}/process")
    assert accepted.status_code == 202
    job = accepted.json()
    assert job["status"] == "queued"

    finished = await _run_worker()
    assert finished.status == JobStatus.completed, finished.error
    assert finished.progress == 1.0

    meeting = (await client.get(f"{api}/meetings/{meeting_id}")).json()
    assert meeting["status"] == "completed"
    assert meeting["summary"]
    assert meeting["keyPoints"]
    assert meeting["decisions"] == ["Ship on Friday"]
    assert meeting["questions"]
    assert [i["title"] for i in meeting["actionItems"]] == [
        "Finish onboarding screens", "Review API changes"]
    assert all(i["state"] == "suggested" for i in meeting["actionItems"]), \
        "extracted action items must never arrive pre-accepted"

    transcript = (await client.get(f"{api}/meetings/{meeting_id}/transcript")).json()
    assert len(transcript["segments"]) == 3
    assert transcript["segments"][0]["speaker"] == "Alex"

    # The recording was a transport artefact; it goes once results exist.
    assert not await storage.exists(f"meetings/{meeting_id}/original.m4a")


async def test_job_progress_reaches_terminal_state(client, api, fake_llm):
    meeting_id = await _upload(client, api)
    job_id = (await client.post(f"{api}/meetings/{meeting_id}/process")).json()["id"]

    during = (await client.get(f"{api}/jobs/{job_id}")).json()
    assert during["status"] == "queued"
    assert during["meetingId"] == meeting_id

    await _run_worker()
    after = (await client.get(f"{api}/jobs/{job_id}")).json()
    assert after["status"] == "completed"
    assert after["stage"] == "completed"
    assert after["progress"] == 1.0


async def test_job_events_stream_emits_and_closes(client, api, fake_llm):
    meeting_id = await _upload(client, api)
    job_id = (await client.post(f"{api}/meetings/{meeting_id}/process")).json()["id"]
    await _run_worker()

    async with client.stream("GET", f"{api}/jobs/{job_id}/events") as response:
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")
        payloads = []
        async for line in response.aiter_lines():
            if line.startswith("data:"):
                payloads.append(json.loads(line.removeprefix("data:").strip()))
                break

    assert payloads[0]["status"] == "completed", "a finished job emits once and the stream ends"


async def test_processing_without_audio_is_rejected(client, api):
    created = await client.post(f"{api}/meetings", json={"title": "Empty"})
    response = await client.post(f"{api}/meetings/{created.json()['id']}/process")
    assert response.status_code == 400


async def test_empty_upload_is_rejected(client, api):
    created = await client.post(f"{api}/meetings", json={"title": "Empty"})
    response = await client.put(f"{api}/meetings/{created.json()['id']}/audio", content=b"")
    assert response.status_code == 400


async def test_reprocessing_reuses_the_running_job(client, api, fake_llm):
    meeting_id = await _upload(client, api)
    first = (await client.post(f"{api}/meetings/{meeting_id}/process")).json()
    second = (await client.post(f"{api}/meetings/{meeting_id}/process")).json()
    assert first["id"] == second["id"], "a second PROCESS must not queue duplicate work"


async def test_upload_is_idempotent_for_the_same_meeting_id(client, api):
    body = {"id": "22222222-2222-2222-2222-222222222222", "title": "Retry"}
    first = await client.post(f"{api}/meetings", json=body)
    second = await client.post(f"{api}/meetings", json=body)
    assert first.json()["id"] == second.json()["id"]
    assert len((await client.get(f"{api}/meetings")).json()) == 1


# --- Failure handling -------------------------------------------------------


async def test_analysis_failure_keeps_the_transcript(client, api):
    class BrokenLLM(FakeLLMProvider):
        async def generate_structured(self, *args, **kwargs):
            raise ProviderError("the local model is not running")

    get_gateway().override(transcription=FakeTranscriptionProvider(), llm=BrokenLLM())

    meeting_id = await _upload(client, api)
    await client.post(f"{api}/meetings/{meeting_id}/process")
    job = await _run_worker()

    assert job.status == JobStatus.failed
    assert "not running" in (job.error or "")

    meeting = (await client.get(f"{api}/meetings/{meeting_id}")).json()
    assert meeting["status"] == "failed"
    assert meeting["error"]
    transcript = (await client.get(f"{api}/meetings/{meeting_id}/transcript")).json()
    assert len(transcript["segments"]) == 3, "the transcript survives an analysis failure"


async def test_silent_recording_produces_an_honest_summary(client, api):
    class SilentTranscription(FakeTranscriptionProvider):
        async def transcribe(self, *args, **kwargs):
            from app.ai.providers.base import Transcript
            return Transcript(text="", segments=[], language="en", duration=12.0, model="fake")

    get_gateway().override(transcription=SilentTranscription(), llm=FakeLLMProvider())

    meeting_id = await _upload(client, api)
    await client.post(f"{api}/meetings/{meeting_id}/process")
    job = await _run_worker()
    assert job.status == JobStatus.completed

    meeting = (await client.get(f"{api}/meetings/{meeting_id}")).json()
    assert "No speech was detected" in meeting["summary"]
    assert meeting["actionItems"] == [], "silence must not produce invented action items"


async def test_abandoned_job_is_reclaimed(client, api, fake_llm, monkeypatch):
    from datetime import datetime, timedelta, timezone

    meeting_id = await _upload(client, api)
    job_id = (await client.post(f"{api}/meetings/{meeting_id}/process")).json()["id"]

    settings = get_settings()
    queue = JobQueue(settings)

    async with session_scope() as session:
        job = await queue.claim(session, "worker-that-died")
        assert job is not None
        # Simulate the worker dying with the lease still held.
        job.locked_at = datetime.now(timezone.utc) - timedelta(seconds=settings.job_lease_seconds + 60)

    async with session_scope() as session:
        reclaimed = await queue.claim(session, "healthy-worker")
        assert reclaimed is not None, "an expired lease must be reclaimable"
        assert reclaimed.id == job_id
        assert reclaimed.attempts == 2


# --- Suggested tasks --------------------------------------------------------


async def test_action_items_become_tasks_only_on_request(client, api, fake_llm):
    meeting_id = await _upload(client, api)
    await client.post(f"{api}/meetings/{meeting_id}/process")
    await _run_worker()

    assert (await client.get(f"{api}/tasks")).json() == [], \
        "processing a meeting must never create tasks by itself"

    items = (await client.get(f"{api}/meetings/{meeting_id}/action-items")).json()
    chosen = items[0]

    created = await client.post(f"{api}/tasks", json={
        "title": chosen["title"], "status": "todo", "source": "meeting",
        "meetingId": meeting_id, "actionItemId": chosen["id"]})
    assert created.status_code == 201
    task = created.json()
    assert task["source"] == "meeting"
    assert task["meetingId"] == meeting_id

    # The suggestion is now marked as accepted and linked to the task.
    items = (await client.get(f"{api}/meetings/{meeting_id}/action-items")).json()
    accepted = next(i for i in items if i["id"] == chosen["id"])
    assert accepted["state"] == "added"
    assert accepted["taskId"] == task["id"]

    # Deleting the task releases the suggestion again.
    await client.delete(f"{api}/tasks/{task['id']}?hard=true")
    items = (await client.get(f"{api}/meetings/{meeting_id}/action-items")).json()
    assert next(i for i in items if i["id"] == chosen["id"])["state"] == "suggested"


async def test_reanalysis_preserves_user_decisions(client, api, fake_llm, session):
    from app.services.meeting_service import MeetingService

    meeting_id = await _upload(client, api)
    await client.post(f"{api}/meetings/{meeting_id}/process")
    await _run_worker()

    items = (await client.get(f"{api}/meetings/{meeting_id}/action-items")).json()
    kept = items[0]["id"]

    async with session_scope() as s:
        service = MeetingService(s, get_storage())
        await service.set_action_item_state(kept, ActionItemState.added, task_id="task-1")
        # Re-run the analysis over the same meeting.
        await service.save_analysis(meeting_id, {
            "summary": "second pass",
            "keyPoints": [], "decisions": [], "questions": [], "topics": [], "participants": [],
            "actionItems": [{"title": "Finish onboarding screens", "confidence": 0.9},
                            {"title": "Something new", "confidence": 0.5}],
        })

    items = (await client.get(f"{api}/meetings/{meeting_id}/action-items")).json()
    states = {i["title"]: i["state"] for i in items}
    assert states["Finish onboarding screens"] == "added", "an accepted item stays accepted"
    assert states["Something new"] == "suggested"


async def test_deleting_a_meeting_removes_its_objects(client, api, storage):
    meeting_id = await _upload(client, api)
    assert await storage.exists(f"meetings/{meeting_id}/original.m4a")

    response = await client.delete(f"{api}/meetings/{meeting_id}")
    assert response.status_code == 204
    assert (await client.get(f"{api}/meetings/{meeting_id}")).status_code == 404
    assert not await storage.exists(f"meetings/{meeting_id}/original.m4a")
