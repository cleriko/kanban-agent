from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from langchain_core.messages import AIMessage, HumanMessage
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..agent.graph import build_graph, system_message
from ..agent.tools import DESTRUCTIVE, TOOLS_BY_NAME, ToolExecutor
from ..ai.gateway import AIGateway
from ..api.schemas import AgentActionDTO, AgentChatBody, AgentChatResponse
from ..config import Settings
from ..db.models import AgentAction, utcnow
from ..infrastructure.storage import ObjectStorage
from .errors import Conflict, NotFound

log = logging.getLogger(__name__)


class AgentService:
    """Runs a turn of the agent and records every write it wants to make.

    A write is never executed inside the turn. It is validated, summarised and stored
    as an `agent_actions` row, then returned to the client for confirmation. The
    client calls back to execute it by id. That is what makes agent behaviour
    auditable, confirmable and — with the stored `undo_state` — reversible later.
    """

    def __init__(self, session: AsyncSession, gateway: AIGateway,
                 storage: ObjectStorage, settings: Settings) -> None:
        self._session = session
        self._gateway = gateway
        self._settings = settings
        self._executor = ToolExecutor(session, storage)

    async def chat(self, body: AgentChatBody) -> AgentChatResponse:
        graph = build_graph(self._gateway.llm, self._executor, self._settings)

        messages: list[Any] = [system_message(body.context.model_dump())]
        for turn in body.history[-12:]:
            messages.append(HumanMessage(content=turn.content) if turn.role == "user"
                            else AIMessage(content=turn.content))
        messages.append(HumanMessage(content=body.message))

        state = await graph.ainvoke({
            "messages": messages, "iterations": 0, "proposed": [], "changed_tasks": False})

        reply = _final_text(state["messages"])
        if reply.startswith("__error__:"):
            raise Conflict(reply.removeprefix("__error__:").strip())

        pending: list[AgentActionDTO] = []
        for proposal in state.get("proposed") or []:
            action = await self._record(proposal["tool"], proposal.get("arguments") or {})
            if action is not None:
                pending.append(_to_dto(action))

        return AgentChatResponse(
            reply=reply or None,
            pending_actions=pending or None,
            applied_actions=None,
            tasks_changed=state.get("changed_tasks") or None,
        )

    async def _record(self, tool: str, arguments: dict[str, Any]) -> AgentAction | None:
        """Validates a proposed write and stores it. Invalid proposals are dropped
        here rather than being shown to the user as something they can approve."""
        spec = TOOLS_BY_NAME.get(tool)
        if spec is None:
            log.warning("agent proposed an unknown tool: %s", tool)
            return None

        try:
            summary, undo_state = await self._describe(tool, arguments)
        except (NotFound, Exception) as exc:  # noqa: BLE001
            log.info("dropping unexecutable proposal %s: %s", tool, exc)
            return None

        action = AgentAction(
            tool=tool,
            arguments=arguments,
            summary=summary,
            destructive=tool in DESTRUCTIVE,
            undo_state=undo_state,
        )
        self._session.add(action)
        await self._session.flush()
        return action

    async def _describe(self, tool: str, arguments: dict[str, Any]) -> tuple[str, dict | None]:
        """Renders the human-readable line the user confirms, and snapshots the row
        being changed so the operation can be undone later."""
        from ..services.task_service import to_dto

        if tool == "create_task":
            title = str(arguments.get("title") or "").strip()
            if not title:
                raise ValueError("create_task without a title")
            status = arguments.get("status") or "inbox"
            return f"CREATE · {title} → {status.upper()}", None

        task_id = await self._executor._resolve_task(arguments)  # noqa: SLF001 - same module family
        task = await self._executor._tasks.get(task_id)          # noqa: SLF001
        snapshot = to_dto(task).model_dump()
        arguments["id"] = task_id   # pin the id so execution cannot drift to another task

        if tool == "move_task":
            target = str(arguments.get("status") or "").lower()
            return f"{task.title.upper()} · {task.status.value.upper()} → {target.upper()}", snapshot
        if tool == "complete_task":
            return f"COMPLETE · {task.title}", snapshot
        if tool == "archive_task":
            return f"ARCHIVE · {task.title}", snapshot
        if tool == "delete_task":
            return f"DELETE · {task.title}", snapshot
        if tool == "update_task":
            fields = [k for k in ("title", "notes", "priority", "due", "tags")
                      if arguments.get(k) is not None]
            return f"UPDATE {', '.join(fields).upper() or 'TASK'} · {task.title}", snapshot
        return f"{tool.replace('_', ' ').upper()} · {task.title}", snapshot

    # --- Execution --------------------------------------------------------

    async def execute(self, action_id: str) -> AgentChatResponse:
        action = await self._session.get(AgentAction, action_id)
        if action is None:
            raise NotFound(f"no agent action with id {action_id}")
        if action.executed:
            raise Conflict("this action has already been executed")

        age = datetime.now(timezone.utc) - _as_utc(action.created_at)
        if age > timedelta(seconds=self._settings.agent_action_ttl_seconds):
            raise Conflict("this action has expired; ask the agent again")

        try:
            outcome = await self._executor.execute(action.tool, action.arguments)
        except Exception as exc:  # noqa: BLE001
            action.error = str(exc)[:2000]
            await self._session.flush()
            raise

        action.executed = True
        action.executed_at = utcnow()
        action.result = {"summary": outcome.summary}
        await self._session.flush()

        return AgentChatResponse(
            reply=None,
            applied_actions=[_to_dto(action)],
            tasks_changed=outcome.changed_tasks or None,
        )

    async def pending(self, limit: int = 20) -> list[AgentAction]:
        statement = (select(AgentAction)
                     .where(AgentAction.executed.is_(False))
                     .order_by(AgentAction.created_at.desc())
                     .limit(limit))
        return list((await self._session.execute(statement)).scalars().all())

    async def history(self, limit: int = 50) -> list[AgentAction]:
        statement = (select(AgentAction)
                     .order_by(AgentAction.created_at.desc())
                     .limit(limit))
        return list((await self._session.execute(statement)).scalars().all())


def _to_dto(action: AgentAction) -> AgentActionDTO:
    return AgentActionDTO(id=action.id, tool=action.tool, summary=action.summary,
                          destructive=action.destructive, applied=action.executed)


def _final_text(messages: list[Any]) -> str:
    for message in reversed(messages):
        if isinstance(message, AIMessage) and message.content and not getattr(message, "tool_calls", None):
            content = message.content
            if isinstance(content, list):
                content = "".join(p.get("text", "") if isinstance(p, dict) else str(p) for p in content)
            return str(content).strip()
    return ""


def _as_utc(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
