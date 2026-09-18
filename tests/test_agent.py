from __future__ import annotations

import json

import pytest

from app.agent.tools import DESTRUCTIVE, READ_ONLY, TOOLS, TOOLS_BY_NAME
from app.ai.gateway import get_gateway
from app.ai.providers.base import LLMProvider
from app.db.models import AgentAction
from app.db.session import session_scope

pytestmark = pytest.mark.asyncio


class ScriptedLLM(LLMProvider):
    """Plays a fixed sequence of agent decisions, so the loop is tested without a model."""

    name = "scripted"

    def __init__(self, decisions: list[dict]) -> None:
        self._decisions = list(decisions)
        self.prompts: list[str] = []

    async def generate(self, prompt: str, *, system=None, temperature=None) -> str:
        self.prompts.append(prompt)
        return json.dumps(self._next())

    async def generate_structured(self, prompt: str, schema, *, system=None, temperature=None):
        self.prompts.append(prompt)
        if system:
            self.prompts.append(f"__system__:{system}")
        return self._next()

    def _next(self) -> dict:
        if not self._decisions:
            return {"reply": "Done."}
        return self._decisions.pop(0)


def script(decisions: list[dict]) -> ScriptedLLM:
    llm = ScriptedLLM(decisions)
    get_gateway().override(llm=llm)
    return llm


async def chat(client, api, message: str, context: dict | None = None):
    return await client.post(f"{api}/agent/chat", json={
        "message": message, "history": [], "clientVersion": "1.0",
        "context": context or {"open": 2, "active": 1, "dueToday": 1, "overdue": 0,
                               "doneToday": 0, "byStatus": {"todo": 2},
                               "agenda": [], "generatedAt": ""}})


# --- Catalogue --------------------------------------------------------------


async def test_tool_catalogue_is_coherent():
    names = {t.name for t in TOOLS}
    assert names == set(TOOLS_BY_NAME)
    assert DESTRUCTIVE == {"delete_task", "archive_task"}
    # Every read-only tool must actually be read-only.
    assert READ_ONLY <= names
    assert "create_task" not in READ_ONLY
    for tool in TOOLS:
        assert tool.parameters["type"] == "object"
        assert "properties" in tool.parameters


# --- Questions answer directly ----------------------------------------------


async def test_read_only_question_answers_without_proposing_anything(client, api):
    script([
        {"tool": "search_tasks", "arguments": {"due": "today"}},
        {"reply": "You have one thing due today: Deploy API."},
    ])
    await client.post(f"{api}/tasks", json={"title": "Deploy API", "due": "today"})

    body = (await chat(client, api, "what do I need to do today?")).json()
    assert body["reply"] == "You have one thing due today: Deploy API."
    assert body["pending_actions"] is None, "a read must not require confirmation"


async def test_board_context_reaches_the_prompt(client, api):
    llm = script([{"reply": "ok"}])
    await chat(client, api, "status?", context={
        "open": 12, "active": 3, "dueToday": 5, "overdue": 1, "doneToday": 2,
        "byStatus": {"inbox": 4, "todo": 5, "in_progress": 3},
        "agenda": [{"id": "abc", "title": "Deploy API", "status": "todo",
                    "priority": "urgent", "due": "2026-09-18T17:00:00Z", "overdue": True}],
        "generatedAt": ""})

    system = next(p for p in llm.prompts if p.startswith("__system__:"))
    assert "open=12" in system
    assert "Deploy API" in system
    assert "OVERDUE" in system
    assert "id=abc" in system


# --- Writes are proposed, not executed --------------------------------------


async def test_write_is_proposed_and_requires_execute(client, api):
    created = (await client.post(f"{api}/tasks", json={
        "title": "Fix API authentication", "status": "todo"})).json()

    script([
        {"tool": "move_task", "arguments": {"selector": "API authentication",
                                            "status": "in_progress"}},
    ])
    body = (await chat(client, api, "move my API task into progress")).json()

    pending = body["pending_actions"]
    assert pending and len(pending) == 1
    action = pending[0]
    assert action["tool"] == "move_task"
    assert "TODO" in action["summary"] and "IN_PROGRESS" in action["summary"]
    assert action["destructive"] is False
    assert action["applied"] is False

    # Nothing has changed yet.
    assert (await client.get(f"{api}/tasks/{created['id']}")).json()["status"] == "todo"

    executed = await client.post(f"{api}/agent/actions/{action['id']}/execute")
    assert executed.status_code == 200
    assert executed.json()["tasks_changed"] is True
    assert (await client.get(f"{api}/tasks/{created['id']}")).json()["status"] == "in_progress"


async def test_destructive_action_is_flagged(client, api):
    await client.post(f"{api}/tasks", json={"title": "Old thing"})
    script([{"tool": "delete_task", "arguments": {"selector": "Old thing"}}])

    body = (await chat(client, api, "delete the old thing")).json()
    action = body["pending_actions"][0]
    assert action["destructive"] is True
    assert action["summary"].startswith("DELETE ·")


async def test_action_cannot_be_executed_twice(client, api):
    await client.post(f"{api}/tasks", json={"title": "Once"})
    script([{"tool": "complete_task", "arguments": {"selector": "Once"}}])
    action = (await chat(client, api, "complete once")).json()["pending_actions"][0]

    assert (await client.post(f"{api}/agent/actions/{action['id']}/execute")).status_code == 200
    second = await client.post(f"{api}/agent/actions/{action['id']}/execute")
    assert second.status_code == 409


async def test_unknown_action_id_is_404(client, api):
    assert (await client.post(f"{api}/agent/actions/nope/execute")).status_code == 404


async def test_expired_action_is_rejected(client, api, monkeypatch):
    from app.config import get_settings
    await client.post(f"{api}/tasks", json={"title": "Stale"})
    script([{"tool": "complete_task", "arguments": {"selector": "Stale"}}])
    action = (await chat(client, api, "complete stale")).json()["pending_actions"][0]

    monkeypatch.setattr(get_settings(), "agent_action_ttl_seconds", -1)
    response = await client.post(f"{api}/agent/actions/{action['id']}/execute")
    assert response.status_code == 409
    assert "expired" in response.json()["detail"]


async def test_proposal_pins_the_resolved_id(client, api):
    """A selector is resolved when the action is proposed, so the user confirms the
    task they were shown — not whatever the selector matches later."""
    first = (await client.post(f"{api}/tasks", json={"title": "Deploy API"})).json()
    script([{"tool": "complete_task", "arguments": {"selector": "Deploy"}}])
    action_id = (await chat(client, api, "complete deploy")).json()["pending_actions"][0]["id"]

    # A better match appears afterwards.
    await client.post(f"{api}/tasks", json={"title": "Deploy"})

    await client.post(f"{api}/agent/actions/{action_id}/execute")
    async with session_scope() as session:
        action = await session.get(AgentAction, action_id)
        assert action.arguments["id"] == first["id"]
    assert (await client.get(f"{api}/tasks/{first['id']}")).json()["status"] == "done"


async def test_undo_state_is_captured(client, api):
    created = (await client.post(f"{api}/tasks", json={
        "title": "Reversible", "status": "todo", "priority": "high"})).json()
    script([{"tool": "delete_task", "arguments": {"selector": "Reversible"}}])
    action_id = (await chat(client, api, "delete reversible")).json()["pending_actions"][0]["id"]

    async with session_scope() as session:
        action = await session.get(AgentAction, action_id)
        assert action.undo_state["title"] == "Reversible"
        assert action.undo_state["priority"] == "high"
        assert action.undo_state["id"] == created["id"]


async def test_unresolvable_proposal_is_dropped(client, api):
    script([{"tool": "complete_task", "arguments": {"selector": "does not exist"}}])
    body = (await chat(client, api, "complete the ghost task")).json()
    assert body["pending_actions"] is None, \
        "the user should never be offered an operation that cannot run"


async def test_unknown_tool_is_ignored(client, api):
    script([{"tool": "launch_missiles", "arguments": {}}, {"reply": "I cannot do that."}])
    body = (await chat(client, api, "launch the missiles")).json()
    assert body["pending_actions"] is None
    assert body["reply"] == "I cannot do that."


async def test_iteration_cap_stops_a_loop(client, api, monkeypatch):
    from app.config import get_settings
    monkeypatch.setattr(get_settings(), "agent_max_iterations", 2)
    # A model that only ever wants to call a read tool would otherwise spin forever.
    script([{"tool": "search_tasks", "arguments": {}}] * 10)

    body = (await chat(client, api, "search endlessly")).json()
    assert body is not None, "the turn must terminate"


async def test_llm_failure_surfaces_as_conflict(client, api):
    from app.ai.providers.base import ProviderError

    class Broken(LLMProvider):
        name = "broken"

        async def generate(self, *a, **k):
            raise ProviderError("ollama is not running")

        async def generate_structured(self, *a, **k):
            raise ProviderError("ollama is not running")

    get_gateway().override(llm=Broken())
    response = await chat(client, api, "hello")
    assert response.status_code == 409
    assert "ollama is not running" in response.json()["detail"]


# --- Meeting awareness ------------------------------------------------------


async def test_agent_reads_meeting_notes(client, api, fake_llm):
    from tests.test_meetings_pipeline import _run_worker

    created = await client.post(f"{api}/meetings", json={"title": "Product Sync"})
    meeting_id = created.json()["id"]
    await client.put(f"{api}/meetings/{meeting_id}/audio", content=b"\x00" * 1024,
                     headers={"Content-Type": "audio/mp4"})
    await client.post(f"{api}/meetings/{meeting_id}/process")
    await _run_worker()

    script([
        {"tool": "get_meeting", "arguments": {"selector": "Product Sync"}},
        {"reply": "You decided to ship on Friday."},
    ])
    body = (await chat(client, api, "what did we decide about onboarding?")).json()
    assert body["reply"] == "You decided to ship on Friday."
    assert body["pending_actions"] is None


async def test_suggest_tasks_never_creates_tasks(client, api, fake_llm):
    from tests.test_meetings_pipeline import _run_worker

    created = await client.post(f"{api}/meetings", json={"title": "Client Call"})
    meeting_id = created.json()["id"]
    await client.put(f"{api}/meetings/{meeting_id}/audio", content=b"\x00" * 1024,
                     headers={"Content-Type": "audio/mp4"})
    await client.post(f"{api}/meetings/{meeting_id}/process")
    await _run_worker()

    script([
        {"tool": "suggest_tasks", "arguments": {"selector": "Client Call"}},
        {"reply": "There are two suggestions waiting for you to review."},
    ])
    body = (await chat(client, api,
                       "create tasks for the action items from the client call")).json()

    assert body["pending_actions"] is None
    assert (await client.get(f"{api}/tasks")).json() == [], \
        "the agent must not be able to turn meeting action items into tasks"


async def test_transcript_tool_is_capped_and_filterable(client, api, fake_llm, session):
    from app.agent.tools import ToolExecutor
    from app.infrastructure.storage import get_storage
    from app.services.meeting_service import MeetingService
    from app.api.schemas import CreateMeetingBody

    async with session_scope() as s:
        service = MeetingService(s, get_storage())
        meeting = await service.create(CreateMeetingBody(title="Long one"))
        await service.save_transcript(
            meeting.id,
            text="x",
            segments=[{"start": i, "end": i + 1, "speaker": "A", "text": f"line {i}"}
                      for i in range(300)],
            language="en", duration=300, provider="fake", model="fake")
        meeting_id = meeting.id

    async with session_scope() as s:
        executor = ToolExecutor(s, get_storage())
        outcome = await executor.execute("get_transcript", {"id": meeting_id})
        assert len(outcome.result["segments"]) == 120, "transcripts must be capped"
        assert outcome.result["truncated"] == 180

        filtered = await executor.execute("get_transcript", {"id": meeting_id, "query": "line 7"})
        assert all("line 7" in s["text"] for s in filtered.result["segments"])


async def test_agent_actions_are_listable_for_audit(client, api):
    await client.post(f"{api}/tasks", json={"title": "Audited"})
    script([{"tool": "complete_task", "arguments": {"selector": "Audited"}}])
    action = (await chat(client, api, "complete audited")).json()["pending_actions"][0]

    pending = (await client.get(f"{api}/agent/actions")).json()
    assert [a["id"] for a in pending] == [action["id"]]

    await client.post(f"{api}/agent/actions/{action['id']}/execute")
    assert (await client.get(f"{api}/agent/actions")).json() == []
    history = (await client.get(f"{api}/agent/actions?history=true")).json()
    assert history[0]["applied"] is True
