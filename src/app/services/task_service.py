from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..api.schemas import CreateTaskBody, MoveBody, PatchTaskBody, SummaryDTO, TaskDTO
from ..db.models import (
    ActionItemState, MeetingActionItem, Task, TaskPriority, TaskSource, TaskStatus, utcnow,
)
from . import dates
from .errors import Invalid, NotFound

# Midpoint gaps below this are close enough to float resolution that two cards can
# end up sharing a position; the column is renumbered instead.
_MIN_POSITION_GAP = 1e-6

_STATUS_ALIASES = {
    "inbox": TaskStatus.inbox, "in": TaskStatus.inbox,
    "todo": TaskStatus.todo, "to do": TaskStatus.todo, "to_do": TaskStatus.todo,
    "next": TaskStatus.todo, "backlog": TaskStatus.todo,
    "in_progress": TaskStatus.in_progress, "in progress": TaskStatus.in_progress,
    "in-progress": TaskStatus.in_progress, "inprogress": TaskStatus.in_progress,
    "active": TaskStatus.in_progress, "doing": TaskStatus.in_progress, "wip": TaskStatus.in_progress,
    "done": TaskStatus.done, "complete": TaskStatus.done, "completed": TaskStatus.done,
    "finished": TaskStatus.done,
}
_PRIORITY_ALIASES = {
    "low": TaskPriority.low, "l": TaskPriority.low,
    "medium": TaskPriority.medium, "med": TaskPriority.medium, "m": TaskPriority.medium,
    "normal": TaskPriority.medium,
    "high": TaskPriority.high, "h": TaskPriority.high,
    "urgent": TaskPriority.urgent, "urg": TaskPriority.urgent, "critical": TaskPriority.urgent,
    "crit": TaskPriority.urgent, "asap": TaskPriority.urgent,
}


def parse_status(value: str) -> TaskStatus:
    status = _STATUS_ALIASES.get(value.strip().lower())
    if status is None:
        raise Invalid(f"unknown status '{value}'")
    return status


def parse_priority(value: str) -> TaskPriority:
    priority = _PRIORITY_ALIASES.get(value.strip().lower())
    if priority is None:
        raise Invalid(f"unknown priority '{value}'")
    return priority


def parse_source(value: str) -> TaskSource:
    try:
        return TaskSource(value.strip().lower())
    except ValueError:
        raise Invalid(f"unknown source '{value}'") from None


def normalise_tags(tags: list[str]) -> list[str]:
    seen: list[str] = []
    for tag in tags:
        cleaned = tag.strip().lstrip("#").lower()
        if cleaned and cleaned not in seen:
            seen.append(cleaned)
    return seen


class TaskService:
    """Every task mutation goes through here — the API, the agent tools and the
    meeting-to-task flow all call these methods. There is exactly one definition of
    what "move a task" means."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    # --- Reads ------------------------------------------------------------

    async def get(self, task_id: str) -> Task:
        task = await self._session.get(Task, task_id)
        if task is None:
            raise NotFound(f"no task with id {task_id}")
        return task

    async def list(
        self,
        *,
        status: str | None = None,
        query: str | None = None,
        tag: str | None = None,
        priority: str | None = None,
        due: str | None = None,
        source: str | None = None,
        meeting_id: str | None = None,
        include_archived: bool = False,
        include_done: bool = True,
        limit: int | None = None,
    ) -> list[Task]:
        statement = select(Task)
        if not include_archived:
            statement = statement.where(Task.archived_at.is_(None))
        if not include_done:
            statement = statement.where(Task.status != TaskStatus.done)
        if status:
            statement = statement.where(Task.status == parse_status(status))
        if priority:
            statement = statement.where(Task.priority == parse_priority(priority))
        if source:
            statement = statement.where(Task.source == parse_source(source))
        if meeting_id:
            statement = statement.where(Task.meeting_id == meeting_id)
        if query:
            # Escape LIKE metacharacters so a search for "100%" does not match everything.
            escaped = query.lower().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            pattern = f"%{escaped}%"
            statement = statement.where(
                func.lower(Task.title).like(pattern, escape="\\")
                | func.lower(Task.notes).like(pattern, escape="\\"))

        statement = statement.order_by(Task.position, Task.created_at.desc())
        tasks = list((await self._session.execute(statement)).scalars().all())

        # Tag and due filters are applied in Python: tags are JSON, and the date
        # windows depend on "now" in a way SQL portability makes awkward.
        if tag:
            needle = tag.strip().lstrip("#").lower()
            tasks = [t for t in tasks if needle in [str(x).lower() for x in (t.tags or [])]]
        if due:
            tasks = [t for t in tasks if _matches_due(t, due)]
        if limit is not None:
            tasks = tasks[:max(0, limit)]
        return tasks

    async def summary(self) -> SummaryDTO:
        tasks = await self.list(include_archived=False)
        now = datetime.now(timezone.utc)
        today = now.date()

        open_tasks = [t for t in tasks if t.status != TaskStatus.done]
        by_status = {status.value: 0 for status in TaskStatus}
        for task in tasks:
            by_status[task.status.value] += 1

        agenda = sorted(
            open_tasks,
            key=lambda t: (
                not _is_overdue(t, now),
                _as_utc(t.due_date) if t.due_date else datetime.max.replace(tzinfo=timezone.utc),
                -_priority_rank(t.priority),
                t.position,
            ),
        )[:10]

        return SummaryDTO(
            open=len(open_tasks),
            active=sum(1 for t in open_tasks if t.status == TaskStatus.in_progress),
            dueToday=sum(1 for t in open_tasks if t.due_date and _as_utc(t.due_date).date() == today),
            overdue=sum(1 for t in open_tasks if _is_overdue(t, now)),
            doneToday=sum(1 for t in tasks
                          if t.completed_at and _as_utc(t.completed_at).date() == today),
            byStatus=by_status,
            agenda=[to_dto(t) for t in agenda],
            generatedAt=dates.iso(now) or "",
        )

    # --- Writes -----------------------------------------------------------

    async def create(self, body: CreateTaskBody) -> Task:
        title = (body.title or "").strip()
        if not title:
            raise Invalid("title is required")

        task_id = body.id or str(uuid.uuid4())
        existing = await self._session.get(Task, task_id)
        if existing is not None:
            # The client replays its outbox after reconnecting; creating the same id
            # twice must be idempotent rather than a 409.
            return existing

        status = parse_status(body.status) if body.status else TaskStatus.inbox
        task = Task(
            id=task_id,
            title=title,
            notes=body.notes or "",
            status=status,
            priority=parse_priority(body.priority) if body.priority else TaskPriority.medium,
            due_date=dates.parse(body.due) if body.due else None,
            tags=normalise_tags(body.tags or []),
            subtasks=[_subtask(s) for s in (body.subtasks or [])],
            source=TaskSource(body.source) if body.source else TaskSource.manual,
            meeting_id=body.meetingId,
            action_item_id=body.actionItemId,
        )
        if body.due and dates.parse(body.due) is None and not dates.is_clear(body.due):
            raise Invalid(f"could not parse due '{body.due}'")
        task.position = body.position if body.position is not None else await self._top_position(status)
        if status == TaskStatus.done:
            task.completed_at = utcnow()

        self._session.add(task)
        await self._session.flush()

        # Keep the meeting's suggestion in step when a task came from one.
        if task.meeting_id and task.action_item_id:
            await self._mark_action_item(task.action_item_id, ActionItemState.added, task.id)
        return task

    async def patch(self, task_id: str, body: PatchTaskBody) -> Task:
        task = await self.get(task_id)

        if body.title is not None:
            title = body.title.strip()
            if not title:
                raise Invalid("title cannot be empty")
            task.title = title
        if body.notes is not None:
            task.notes = body.notes
        if body.priority is not None:
            task.priority = parse_priority(body.priority)
        if body.tags is not None:
            task.tags = normalise_tags(body.tags)
        if body.due is not None:
            if dates.is_clear(body.due):
                task.due_date = None
            else:
                parsed = dates.parse(body.due)
                if parsed is None:
                    raise Invalid(f"could not parse due '{body.due}'")
                task.due_date = parsed
        # Status first, then archive: _apply_status clears archived_at for a non-done
        # column, which would otherwise undo an archive the same request asked for.
        if body.status is not None:
            await self._apply_status(task, parse_status(body.status), index=0,
                                     position=body.position)
        elif body.position is not None:
            task.position = body.position
        if body.archived is not None:
            task.archived_at = utcnow() if body.archived else None

        task.updated_at = utcnow()
        await self._session.flush()
        return task

    async def move(self, task_id: str, body: MoveBody) -> Task:
        task = await self.get(task_id)
        await self._apply_status(task, parse_status(body.status),
                                 index=body.index, position=body.position)
        task.updated_at = utcnow()
        await self._session.flush()
        return task

    async def complete(self, task_id: str) -> Task:
        task = await self.get(task_id)
        if task.status != TaskStatus.done:
            await self._apply_status(task, TaskStatus.done, index=0)
            task.updated_at = utcnow()
            await self._session.flush()
        return task

    async def reopen(self, task_id: str, status: str | None = None) -> Task:
        task = await self.get(task_id)
        await self._apply_status(task, parse_status(status) if status else TaskStatus.todo, index=0)
        task.updated_at = utcnow()
        await self._session.flush()
        return task

    async def archive(self, task_id: str) -> Task:
        task = await self.get(task_id)
        task.archived_at = utcnow()
        task.updated_at = utcnow()
        await self._session.flush()
        return task

    async def delete(self, task_id: str, *, hard: bool = False) -> TaskDTO:
        task = await self.get(task_id)
        if not hard:
            return to_dto(await self.archive(task_id))

        # Build the response before the row disappears: serialising a deleted ORM
        # instance afterwards is fragile, even while its attributes still load.
        snapshot = to_dto(task)
        # A deleted task releases its suggestion, so the meeting can offer it again.
        if task.action_item_id:
            await self._mark_action_item(task.action_item_id, ActionItemState.suggested, None)
        await self._session.delete(task)
        await self._session.flush()
        return snapshot

    # --- Internals --------------------------------------------------------

    async def _apply_status(self, task: Task, status: TaskStatus,
                            *, index: int | None, position: float | None = None) -> None:
        task.status = status
        if position is not None:
            task.position = position
        else:
            task.position = await self._position_for(status, index, exclude=task.id)

        if status == TaskStatus.done:
            task.completed_at = task.completed_at or utcnow()
        else:
            task.completed_at = None
            task.archived_at = None

    async def _position_for(self, status: TaskStatus, index: int | None,
                            *, exclude: str | None = None) -> float:
        positions = await self._positions(status, exclude)

        if not positions:
            return 0.0
        if index is None or index <= 0:
            return positions[0] - 1
        if index >= len(positions):
            return positions[-1] + 1

        low, high = positions[index - 1], positions[index]
        if high - low < _MIN_POSITION_GAP:
            # Repeated midpoint inserts eventually reach float resolution, at which
            # point two cards share a position and their order becomes arbitrary.
            # Renumber the column in place, then take the midpoint of the new gap.
            await self._renormalise(status)
            positions = await self._positions(status, exclude)
            if index >= len(positions):
                return positions[-1] + 1
            low, high = positions[index - 1], positions[index]
        # Sparse doubles: inserting between two neighbours is one UPDATE, never a
        # renumbering of the column.
        return (low + high) / 2

    async def _positions(self, status: TaskStatus, exclude: str | None = None) -> list[float]:
        statement = (select(Task.position)
                     .where(Task.status == status, Task.archived_at.is_(None))
                     .order_by(Task.position, Task.created_at))
        if exclude:
            statement = statement.where(Task.id != exclude)
        return list((await self._session.execute(statement)).scalars().all())

    async def _renormalise(self, status: TaskStatus) -> None:
        """Gives the column clean integer positions without changing its order."""
        statement = (select(Task)
                     .where(Task.status == status, Task.archived_at.is_(None))
                     .order_by(Task.position, Task.created_at))
        for position, task in enumerate((await self._session.execute(statement)).scalars().all()):
            task.position = float(position)
        await self._session.flush()

    async def _top_position(self, status: TaskStatus) -> float:
        return await self._position_for(status, 0)

    async def _mark_action_item(self, action_item_id: str, state: ActionItemState,
                                task_id: str | None) -> None:
        item = await self._session.get(MeetingActionItem, action_item_id)
        if item is None:
            return
        item.state = state
        item.task_id = task_id
        await self._session.flush()


# --- Helpers ----------------------------------------------------------------


def _as_utc(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _priority_rank(priority: TaskPriority) -> int:
    return {TaskPriority.low: 0, TaskPriority.medium: 1,
            TaskPriority.high: 2, TaskPriority.urgent: 3}[priority]


def _is_overdue(task: Task, now: datetime) -> bool:
    if task.due_date is None or task.status == TaskStatus.done or task.archived_at:
        return False
    due = _as_utc(task.due_date)
    return due < now and due.date() != now.date()


def _matches_due(task: Task, window: str) -> bool:
    now = datetime.now(timezone.utc)
    window = window.strip().lower()
    if window == "none":
        return task.due_date is None
    if window == "any":
        return task.due_date is not None
    if task.due_date is None:
        return False
    due = _as_utc(task.due_date)
    if window == "today":
        return due.date() == now.date()
    if window == "overdue":
        return _is_overdue(task, now)
    if window == "week":
        return due <= now.replace(hour=23, minute=59) + timedelta(days=7)
    # An unknown window must not silently match everything.
    return False


def to_dto(task: Task) -> TaskDTO:
    now = datetime.now(timezone.utc)
    due = _as_utc(task.due_date) if task.due_date else None
    return TaskDTO(
        id=task.id,
        title=task.title,
        notes=task.notes or "",
        status=task.status.value,
        priority=task.priority.value,
        due=dates.iso(task.due_date),
        tags=[str(t) for t in (task.tags or [])],
        subtasks=[_subtask(s) for s in (task.subtasks or [])],
        position=task.position,
        created=dates.iso(task.created_at) or "",
        updated=dates.iso(task.updated_at) or "",
        completed=dates.iso(task.completed_at),
        archived=dates.iso(task.archived_at),
        source=task.source.value,
        meetingId=task.meeting_id,
        actionItemId=task.action_item_id,
        overdue=_is_overdue(task, now),
        dueToday=bool(due and due.date() == now.date()),
    )


def _subtask(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return {"id": raw.get("id", str(uuid.uuid4())),
                "title": raw.get("title", ""),
                "done": bool(raw.get("done", False))}
    return {"id": str(uuid.uuid4()), "title": str(raw), "done": False}
