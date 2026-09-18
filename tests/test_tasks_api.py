from __future__ import annotations

import pytest

pytestmark = pytest.mark.asyncio


async def test_health_is_open_and_reports_providers(client, api):
    response = await client.get(f"{api}/health")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["database"] is True
    assert body["transcription"] == "fake"
    assert body["llm"] == "fake"


async def test_task_lifecycle(client, api):
    created = await client.post(f"{api}/tasks", json={
        "title": "Deploy API", "priority": "high", "due": "today", "tags": ["Ops", "#ops"]})
    assert created.status_code == 201
    task = created.json()
    assert task["status"] == "inbox"
    assert task["priority"] == "high"
    assert task["tags"] == ["ops"], "tags are normalised and deduped"
    assert task["dueToday"] is True
    task_id = task["id"]

    patched = await client.patch(f"{api}/tasks/{task_id}", json={"title": "Deploy API v2"})
    assert patched.json()["title"] == "Deploy API v2"
    assert patched.json()["priority"] == "high", "patch must not reset untouched fields"

    moved = await client.post(f"{api}/tasks/{task_id}/move", json={"status": "in_progress"})
    assert moved.json()["status"] == "in_progress"

    completed = await client.post(f"{api}/tasks/{task_id}/complete")
    assert completed.json()["status"] == "done"
    assert completed.json()["completed"] is not None

    reopened = await client.post(f"{api}/tasks/{task_id}/reopen?status=todo")
    assert reopened.json()["status"] == "todo"
    assert reopened.json()["completed"] is None

    archived = await client.delete(f"{api}/tasks/{task_id}")
    assert archived.json()["archived"] is not None

    hard = await client.delete(f"{api}/tasks/{task_id}?hard=true")
    assert hard.status_code == 200
    assert (await client.get(f"{api}/tasks/{task_id}")).status_code == 404


async def test_client_supplied_id_is_idempotent(client, api):
    """The Mac replays its outbox after reconnecting; the same create must not 409."""
    body = {"id": "11111111-1111-1111-1111-111111111111", "title": "Offline task"}
    first = await client.post(f"{api}/tasks", json=body)
    second = await client.post(f"{api}/tasks", json=body)
    assert first.status_code == 201
    assert second.status_code == 201
    assert first.json()["id"] == second.json()["id"]
    assert len((await client.get(f"{api}/tasks")).json()) == 1


async def test_validation_errors(client, api):
    assert (await client.post(f"{api}/tasks", json={"title": "  "})).status_code == 400
    assert (await client.post(f"{api}/tasks", json={"title": "x", "status": "sideways"})).status_code == 400
    assert (await client.post(f"{api}/tasks", json={"title": "x", "priority": "spicy"})).status_code == 400
    assert (await client.post(f"{api}/tasks", json={"title": "x", "due": "whenever"})).status_code == 400
    assert (await client.post(f"{api}/tasks", json={})).status_code == 422
    assert (await client.get(f"{api}/tasks/missing")).status_code == 404


async def test_due_clearing_sentinels(client, api):
    created = (await client.post(f"{api}/tasks", json={"title": "A", "due": "tomorrow"})).json()
    assert created["due"] is not None
    for sentinel in ("none", "", "clear"):
        cleared = await client.patch(f"{api}/tasks/{created['id']}", json={"due": sentinel})
        assert cleared.json()["due"] is None
        await client.patch(f"{api}/tasks/{created['id']}", json={"due": "tomorrow"})


async def test_ordering_and_move_index(client, api):
    ids = []
    for title in ("a", "b", "c"):
        ids.append((await client.post(f"{api}/tasks", json={"title": title})).json()["id"])

    order = [t["title"] for t in (await client.get(f"{api}/tasks")).json()]
    assert order == ["c", "b", "a"], "new tasks land on top"

    await client.post(f"{api}/tasks/{ids[0]}/move", json={"status": "inbox", "index": 1})
    order = [t["title"] for t in (await client.get(f"{api}/tasks")).json()]
    assert order == ["c", "a", "b"]


async def test_repeated_midpoint_inserts_keep_strict_order(client, api):
    ids = []
    for index in range(8):
        ids.append((await client.post(f"{api}/tasks", json={"title": f"t{index}"})).json()["id"])

    for _ in range(25):
        tasks = (await client.get(f"{api}/tasks")).json()
        await client.post(f"{api}/tasks/{tasks[-1]['id']}/move", json={"status": "inbox", "index": 1})

    positions = [t["position"] for t in (await client.get(f"{api}/tasks")).json()]
    assert positions == sorted(positions)
    assert len(set(positions)) == len(positions), "positions must not collapse"


async def test_filters(client, api):
    await client.post(f"{api}/tasks", json={"title": "today one", "due": "today", "tags": ["home"]})
    await client.post(f"{api}/tasks", json={"title": "urgent one", "status": "in_progress",
                                            "priority": "urgent"})
    done = (await client.post(f"{api}/tasks", json={"title": "finished"})).json()
    await client.post(f"{api}/tasks/{done['id']}/complete")

    assert len((await client.get(f"{api}/tasks?status=in_progress")).json()) == 1
    assert len((await client.get(f"{api}/tasks?due=today")).json()) == 1
    assert len((await client.get(f"{api}/tasks?tag=HOME")).json()) == 1
    assert len((await client.get(f"{api}/tasks?priority=urgent")).json()) == 1
    assert len((await client.get(f"{api}/tasks?includeDone=false")).json()) == 2
    assert len((await client.get(f"{api}/tasks?q=urgent")).json()) == 1


async def test_summary(client, api):
    await client.post(f"{api}/tasks", json={"title": "a", "status": "in_progress"})
    await client.post(f"{api}/tasks", json={"title": "b", "due": "today"})
    done = (await client.post(f"{api}/tasks", json={"title": "c"})).json()
    await client.post(f"{api}/tasks/{done['id']}/complete")

    summary = (await client.get(f"{api}/summary")).json()
    assert summary["open"] == 2
    assert summary["active"] == 1
    assert summary["dueToday"] == 1
    assert summary["doneToday"] == 1
    assert summary["byStatus"]["done"] == 1
    assert summary["agenda"]


async def test_auth_is_enforced_when_a_token_is_set(client, api, monkeypatch):
    from app.config import get_settings
    settings = get_settings()
    monkeypatch.setattr(settings, "api_token", "secret")

    assert (await client.get(f"{api}/health")).status_code == 200, "health stays open"
    assert (await client.get(f"{api}/tasks")).status_code == 401
    assert (await client.get(f"{api}/tasks", headers={"Authorization": "Bearer nope"})).status_code == 401
    ok = await client.get(f"{api}/tasks", headers={"Authorization": "Bearer secret"})
    assert ok.status_code == 200
