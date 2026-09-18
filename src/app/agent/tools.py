from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable

from sqlalchemy.ext.asyncio import AsyncSession

from ..api.schemas import CreateTaskBody, MoveBody, PatchTaskBody
from ..db.models import ActionItemState, TaskSource
from ..infrastructure.storage import ObjectStorage
from ..services import dates
from ..services.errors import Invalid, NotFound
from ..services.meeting_service import MeetingService
from ..services.task_service import TaskService, to_dto

log = logging.getLogger(__name__)


@dataclass(slots=True)
class ToolSpec:
    name: str
    description: str
    parameters: dict[str, Any]
    destructive: bool
    # A read never changes anything, so it runs immediately rather than being proposed.
    read_only: bool = False


@dataclass(slots=True)
class ToolOutcome:
    ok: bool
    summary: str
    result: Any = None
    changed_tasks: bool = False


def _schema(properties: dict[str, Any], required: list[str] | None = None) -> dict[str, Any]:
    return {"type": "object", "properties": properties, "required": required or []}


_STATUS = {"type": "string", "enum": ["inbox", "todo", "in_progress", "done"]}
_PRIORITY = {"type": "string", "enum": ["low", "medium", "high", "urgent"]}
_SELECTOR = {"type": "string", "description": "Part of the task title, when the id is unknown."}

TOOLS: list[ToolSpec] = [
    ToolSpec("search_tasks", "Search tasks by text, status, tag, priority or due window.",
             _schema({"query": {"type": "string"}, "status": _STATUS, "tag": {"type": "string"},
                      "priority": _PRIORITY,
                      "due": {"type": "string", "enum": ["today", "overdue", "week", "none", "any"]},
                      "includeDone": {"type": "boolean"}, "limit": {"type": "integer"}}),
             destructive=False, read_only=True),
    ToolSpec("get_task", "Fetch one task by id.",
             _schema({"id": {"type": "string"}}, ["id"]), destructive=False, read_only=True),
    ToolSpec("get_tasks", "List tasks, optionally filtered by status.",
             _schema({"status": _STATUS, "limit": {"type": "integer"}}),
             destructive=False, read_only=True),
    ToolSpec("create_task", "Create a task.",
             _schema({"title": {"type": "string"}, "notes": {"type": "string"},
                      "status": _STATUS, "priority": _PRIORITY,
                      "due": {"type": "string"}, "tags": {"type": "array", "items": {"type": "string"}}},
                     ["title"]),
             destructive=False),
    ToolSpec("update_task", "Change fields on a task. Only pass what should change.",
             _schema({"id": {"type": "string"}, "selector": _SELECTOR,
                      "title": {"type": "string"}, "notes": {"type": "string"},
                      "priority": _PRIORITY, "due": {"type": "string"},
                      "tags": {"type": "array", "items": {"type": "string"}}}),
             destructive=False),
    ToolSpec("move_task", "Move a task to another status column.",
             _schema({"id": {"type": "string"}, "selector": _SELECTOR, "status": _STATUS},
                     ["status"]),
             destructive=False),
    ToolSpec("complete_task", "Mark a task done.",
             _schema({"id": {"type": "string"}, "selector": _SELECTOR}), destructive=False),
    ToolSpec("delete_task", "Permanently delete a task. Prefer archiving.",
             _schema({"id": {"type": "string"}, "selector": _SELECTOR}), destructive=True),
    ToolSpec("archive_task", "Archive a task, hiding it from the board. Recoverable.",
             _schema({"id": {"type": "string"}, "selector": _SELECTOR}), destructive=True),
    ToolSpec("search_meetings", "Find meetings by title or summary text.",
             _schema({"query": {"type": "string"}, "limit": {"type": "integer"}}),
             destructive=False, read_only=True),
    ToolSpec("get_meeting", "Fetch a meeting's summary, decisions and action items.",
             _schema({"id": {"type": "string"}, "selector": {"type": "string"}}),
             destructive=False, read_only=True),
    ToolSpec("get_transcript", "Fetch a meeting transcript. Use sparingly; it is long.",
             _schema({"id": {"type": "string"}, "selector": {"type": "string"},
                      "query": {"type": "string",
                                "description": "Only return segments containing this text."}}),
             destructive=False, read_only=True),
    ToolSpec("get_action_items", "List suggested action items, optionally across recent meetings.",
             _schema({"meetingId": {"type": "string"}, "state": {"type": "string"},
                      "limit": {"type": "integer"}}),
             destructive=False, read_only=True),
    ToolSpec("suggest_tasks",
             "Propose tasks from a meeting's action items for the user to review. "
             "This does NOT create tasks.",
             _schema({"meetingId": {"type": "string"}, "selector": {"type": "string"}}),
             destructive=False, read_only=True),
]

TOOLS_BY_NAME = {tool.name: tool for tool in TOOLS}
DESTRUCTIVE = {tool.name for tool in TOOLS if tool.destructive}
READ_ONLY = {tool.name for tool in TOOLS if tool.read_only}


class ToolExecutor:
    """Executes a validated tool call against the services.

    The agent never sees a session, a model or a table. It names a tool; this decides
    whether that is allowed and what it means.
    """

    def __init__(self, session: AsyncSession, storage: ObjectStorage) -> None:
        self._session = session
        self._tasks = TaskService(session)
        self._meetings = MeetingService(session, storage)

    async def execute(self, name: str, args: dict[str, Any]) -> ToolOutcome:
        handler: Callable | None = getattr(self, f"_{name}", None)
        if handler is None:
            raise Invalid(f"unknown tool '{name}'")
        return await handler(args or {})

    # --- Task reads -------------------------------------------------------

    async def _search_tasks(self, args: dict[str, Any]) -> ToolOutcome:
        tasks = await self._tasks.list(
            query=args.get("query"), status=args.get("status"), tag=args.get("tag"),
            priority=args.get("priority"), due=args.get("due"),
            include_done=bool(args.get("includeDone", True)),
            limit=int(args.get("limit") or 25))
        return ToolOutcome(True, f"{len(tasks)} task(s)", [_compact_task(t) for t in tasks])

    async def _get_tasks(self, args: dict[str, Any]) -> ToolOutcome:
        return await self._search_tasks(args)

    async def _get_task(self, args: dict[str, Any]) -> ToolOutcome:
        task = await self._tasks.get(str(args["id"]))
        return ToolOutcome(True, task.title, _compact_task(task))

    # --- Task writes ------------------------------------------------------

    async def _create_task(self, args: dict[str, Any]) -> ToolOutcome:
        task = await self._tasks.create(CreateTaskBody(
            title=str(args.get("title") or ""),
            notes=args.get("notes"),
            status=args.get("status"),
            priority=args.get("priority"),
            due=args.get("due"),
            tags=args.get("tags"),
            source=TaskSource.agent.value,
        ))
        return ToolOutcome(True, f"Created “{task.title}” in {task.status.value}",
                           _compact_task(task), changed_tasks=True)

    async def _update_task(self, args: dict[str, Any]) -> ToolOutcome:
        task_id = await self._resolve_task(args)
        task = await self._tasks.patch(task_id, PatchTaskBody(
            title=args.get("title"), notes=args.get("notes"), priority=args.get("priority"),
            due=args.get("due"), tags=args.get("tags")))
        return ToolOutcome(True, f"Updated “{task.title}”", _compact_task(task), changed_tasks=True)

    async def _move_task(self, args: dict[str, Any]) -> ToolOutcome:
        task_id = await self._resolve_task(args)
        before = (await self._tasks.get(task_id)).status.value
        task = await self._tasks.move(task_id, MoveBody(status=str(args["status"]), index=0))
        return ToolOutcome(True, f"“{task.title}” · {before} → {task.status.value}",
                           _compact_task(task), changed_tasks=True)

    async def _complete_task(self, args: dict[str, Any]) -> ToolOutcome:
        task = await self._tasks.complete(await self._resolve_task(args))
        return ToolOutcome(True, f"Completed “{task.title}”", _compact_task(task), changed_tasks=True)

    async def _archive_task(self, args: dict[str, Any]) -> ToolOutcome:
        task = await self._tasks.archive(await self._resolve_task(args))
        return ToolOutcome(True, f"Archived “{task.title}”", _compact_task(task), changed_tasks=True)

    async def _delete_task(self, args: dict[str, Any]) -> ToolOutcome:
        task_id = await self._resolve_task(args)
        task = await self._tasks.get(task_id)
        title = task.title
        await self._tasks.delete(task_id, hard=True)
        return ToolOutcome(True, f"Deleted “{title}”", {"id": task_id}, changed_tasks=True)

    # --- Meeting reads ----------------------------------------------------

    async def _search_meetings(self, args: dict[str, Any]) -> ToolOutcome:
        meetings = await self._meetings.list(limit=int(args.get("limit") or 10),
                                             query=args.get("query"))
        return ToolOutcome(True, f"{len(meetings)} meeting(s)",
                           [_compact_meeting(m) for m in meetings])

    async def _get_meeting(self, args: dict[str, Any]) -> ToolOutcome:
        meeting = await self._meetings.get(await self._resolve_meeting(args))
        return ToolOutcome(True, meeting.title, _full_meeting(meeting))

    async def _get_transcript(self, args: dict[str, Any]) -> ToolOutcome:
        meeting_id = await self._resolve_meeting(args)
        transcript = await self._meetings.transcript(meeting_id)
        segments = transcript.segments or []
        needle = (args.get("query") or "").strip().lower()
        if needle:
            segments = [s for s in segments if needle in str(s.get("text", "")).lower()]
        # Hard cap: a transcript can be tens of thousands of tokens.
        clipped = segments[:120]
        return ToolOutcome(
            True,
            f"{len(clipped)} of {len(transcript.segments or [])} segment(s)",
            {"segments": [{"start": s.get("start"), "speaker": s.get("speaker"),
                           "text": s.get("text")} for s in clipped],
             "truncated": max(0, len(segments) - len(clipped))})

    async def _get_action_items(self, args: dict[str, Any]) -> ToolOutcome:
        state = args.get("state")
        if args.get("meetingId"):
            items = await self._meetings.action_items(str(args["meetingId"]))
            meetings = {str(args["meetingId"]): (await self._meetings.get(str(args["meetingId"]))).title}
        else:
            recent = await self._meetings.list(limit=int(args.get("limit") or 10))
            items = [item for meeting in recent for item in meeting.action_items]
            meetings = {m.id: m.title for m in recent}

        if state:
            items = [i for i in items if i.state.value == state]

        return ToolOutcome(True, f"{len(items)} action item(s)", [
            {"id": i.id, "title": i.title, "assignee": i.assignee, "state": i.state.value,
             "meeting": meetings.get(i.meeting_id), "meetingId": i.meeting_id,
             "taskId": i.task_id}
            for i in items[:50]])

    async def _suggest_tasks(self, args: dict[str, Any]) -> ToolOutcome:
        """Returns proposals only. Creating tasks from a meeting is the user's call,
        made in the app's review UI — the agent cannot shortcut it."""
        meeting_id = await self._resolve_meeting(args)
        items = [i for i in await self._meetings.action_items(meeting_id)
                 if i.state == ActionItemState.suggested]
        meeting = await self._meetings.get(meeting_id)
        return ToolOutcome(
            True,
            f"{len(items)} suggestion(s) from “{meeting.title}” awaiting review",
            {"meetingId": meeting_id, "meeting": meeting.title,
             "suggestions": [{"id": i.id, "title": i.title, "assignee": i.assignee,
                              "confidence": i.confidence} for i in items],
             "note": "These are proposals. The user reviews and adds them in the app."})

    # --- Resolution -------------------------------------------------------

    async def _resolve_task(self, args: dict[str, Any]) -> str:
        task_id = args.get("id")
        if task_id:
            await self._tasks.get(str(task_id))
            return str(task_id)

        selector = (args.get("selector") or args.get("title") or "").strip()
        if not selector:
            raise Invalid("id or selector is required")

        candidates = await self._tasks.list(include_archived=False)
        needle = selector.lower()
        exact = [t for t in candidates if t.title.lower() == needle]
        if exact:
            return exact[0].id
        open_tasks = [t for t in candidates if t.status.value != "done"]
        for pool in (open_tasks, candidates):
            prefix = [t for t in pool if t.title.lower().startswith(needle)]
            if prefix:
                return prefix[0].id
            contains = [t for t in pool if needle in t.title.lower()]
            if contains:
                return contains[0].id
        raise NotFound(f"no task matching '{selector}'")

    async def _resolve_meeting(self, args: dict[str, Any]) -> str:
        meeting_id = args.get("id") or args.get("meetingId")
        if meeting_id:
            await self._meetings.get(str(meeting_id))
            return str(meeting_id)

        selector = (args.get("selector") or args.get("title") or "").strip()
        meetings = await self._meetings.list(limit=50)
        if not selector:
            # "yesterday's meeting" with nothing else to go on: the most recent one.
            if meetings:
                return meetings[0].id
            raise NotFound("there are no meetings")

        needle = selector.lower()
        for meeting in meetings:
            if needle in meeting.title.lower():
                return meeting.id
        raise NotFound(f"no meeting matching '{selector}'")


# --- Compaction -------------------------------------------------------------
# Tool output goes straight into the model's context, so it is trimmed to what is
# actually useful for reasoning.


def _compact_task(task: Any) -> dict[str, Any]:
    dto = to_dto(task)
    compact = {"id": dto.id, "title": dto.title, "status": dto.status, "priority": dto.priority}
    if dto.due:
        compact["due"] = dto.due
    if dto.overdue:
        compact["overdue"] = True
    if dto.tags:
        compact["tags"] = dto.tags
    if dto.source != "manual":
        compact["source"] = dto.source
    return compact


def _compact_meeting(meeting: Any) -> dict[str, Any]:
    return {"id": meeting.id, "title": meeting.title,
            "date": dates.iso(meeting.started_at),
            "status": meeting.status.value,
            "suggestions": sum(1 for i in meeting.action_items
                               if i.state == ActionItemState.suggested)}


def _full_meeting(meeting: Any) -> dict[str, Any]:
    return {
        "id": meeting.id,
        "title": meeting.title,
        "date": dates.iso(meeting.started_at),
        "summary": meeting.summary,
        "keyPoints": meeting.key_points or [],
        "decisions": meeting.decisions or [],
        "questions": meeting.questions or [],
        "participants": meeting.participants or [],
        "actionItems": [{"id": i.id, "title": i.title, "assignee": i.assignee,
                         "state": i.state.value} for i in meeting.action_items],
    }


def now_label() -> str:
    return datetime.now(timezone.utc).astimezone().strftime("%A %d %B %Y, %H:%M %Z")


def tools_json() -> str:
    return json.dumps([{"name": t.name, "description": t.description,
                        "parameters": t.parameters, "destructive": t.destructive}
                       for t in TOOLS], indent=2)
