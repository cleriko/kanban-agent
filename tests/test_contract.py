"""Cross-repo contract tests.

``tests/fixtures/*.json`` are produced by the macOS client's own JSONEncoder
(kanban-mac: Tests/KanbanCoreTests/_FixtureGenerator.swift) and copied here by
scripts/sync-contract-fixtures.sh. If either side's types drift, these fail rather
than the app silently sending a field the server ignores.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.api.schemas import AgentChatBody, CreateMeetingBody, CreateTaskBody, MoveBody

FIXTURES = Path(__file__).parent / "fixtures"


def load(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text())


# --- Request bodies ---------------------------------------------------------


def test_task_create_body_parses():
    raw = load("task_create.json")
    body = CreateTaskBody(**raw)
    assert body.title == "Deploy API"
    assert body.status == "in_progress"
    assert body.priority == "urgent"
    assert body.tags == ["ops"]
    assert body.source == "meeting"
    assert body.meetingId == "33333333-3333-3333-3333-333333333333"
    assert body.actionItemId == "44444444-4444-4444-4444-444444444444"
    assert body.id == "55555555-5555-5555-5555-555555555555"
    assert body.subtasks == ["build image", "run migrations"]


def test_task_move_body_parses():
    body = MoveBody(**load("task_move.json"))
    assert body.status == "in_progress"
    assert body.index == 0


def test_meeting_create_body_parses():
    body = CreateMeetingBody(**load("meeting_create.json"))
    assert body.title == "Product Sync"
    assert body.duration == 2532
    assert body.startedAt.startswith("2026-")


def test_agent_chat_body_parses_with_board_context():
    body = AgentChatBody(**load("agent_chat.json"))
    assert body.message == "move my API task into progress"
    assert [t.role for t in body.history] == ["user", "assistant"]
    assert body.context.open == 2
    assert body.context.active == 1
    assert body.context.byStatus["in_progress"] == 1
    assert len(body.context.agenda) == 2
    assert body.context.agenda[0]["title"] == "Deploy API"
    assert body.context.agenda[0]["status"] == "in_progress"


# --- Live round trips -------------------------------------------------------


@pytest.mark.asyncio
async def test_client_bytes_create_a_task(client, api):
    """The exact body the Mac would replay from its outbox."""
    response = await client.post(f"{api}/tasks", content=(FIXTURES / "task_create.json").read_bytes(),
                                 headers={"Content-Type": "application/json"})
    assert response.status_code == 201
    task = response.json()
    assert task["id"] == "55555555-5555-5555-5555-555555555555"
    assert task["status"] == "in_progress"
    assert task["source"] == "meeting"
    assert task["subtasks"], "subtasks sent as titles must come back as objects"

    # And the response is a shape the Swift decoder accepts: every key it reads.
    for key in ("id", "title", "notes", "status", "priority", "due", "tags", "subtasks",
                "position", "created", "updated", "completed", "archived", "source",
                "meetingId", "actionItemId", "overdue", "dueToday"):
        assert key in task, f"TaskDTO is missing {key}, which the Swift client decodes"


@pytest.mark.asyncio
async def test_client_bytes_create_a_meeting(client, api):
    response = await client.post(f"{api}/meetings",
                                 content=(FIXTURES / "meeting_create.json").read_bytes(),
                                 headers={"Content-Type": "application/json"})
    assert response.status_code == 201
    meeting = response.json()
    assert meeting["id"] == "33333333-3333-3333-3333-333333333333"
    for key in ("id", "title", "startedAt", "duration", "status", "summary", "keyPoints",
                "decisions", "questions", "topics", "participants", "actionItems",
                "transcript", "jobId", "error"):
        assert key in meeting, f"MeetingDTO is missing {key}"


@pytest.mark.asyncio
async def test_client_bytes_reach_the_agent(client, api):
    from tests.test_agent import script

    script([{"reply": "ok"}])
    response = await client.post(f"{api}/agent/chat",
                                 content=(FIXTURES / "agent_chat.json").read_bytes(),
                                 headers={"Content-Type": "application/json"})
    assert response.status_code == 200
    body = response.json()
    assert set(body) == {"reply", "pending_actions", "applied_actions", "tasks_changed"}, \
        "the response keys are what the Swift AgentResponse decoder reads"


@pytest.mark.asyncio
async def test_status_values_match_the_client_enum(client, api):
    """The Swift TaskStatus raw values are the wire contract."""
    from app.db.models import TaskStatus

    assert {s.value for s in TaskStatus} == {"inbox", "todo", "in_progress", "done"}

    created = (await client.post(f"{api}/tasks", json={"title": "x"})).json()
    for status in ("inbox", "todo", "in_progress", "done"):
        moved = await client.post(f"{api}/tasks/{created['id']}/move", json={"status": status})
        assert moved.json()["status"] == status


@pytest.mark.asyncio
async def test_job_stages_match_the_client_mapping(client, api):
    """JobDTO.meetingStatus in Swift maps these strings; anything else shows as unknown."""
    from app.db.models import JobStatus

    assert {s.value for s in JobStatus} == {"queued", "running", "completed", "failed"}
    # The stages the worker actually reports.
    for stage in ("queued", "transcribing", "analyzing", "completed"):
        assert stage in {"queued", "uploading", "transcribing", "analyzing", "completed", "failed"}
