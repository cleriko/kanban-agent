from __future__ import annotations

import json
import logging
from typing import Annotated, Any, Literal, TypedDict

from langchain_core.messages import AIMessage, AnyMessage, HumanMessage, SystemMessage, ToolMessage
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages

from ..ai.providers.base import LLMProvider, ProviderError
from ..config import Settings
from .prompts.agent import AGENT_SYSTEM, BOARD_TEMPLATE
from .tools import READ_ONLY, TOOLS, TOOLS_BY_NAME, ToolExecutor, now_label

log = logging.getLogger(__name__)


class AgentState(TypedDict):
    messages: Annotated[list[AnyMessage], add_messages]
    iterations: int
    # Writes the model asked for; the API proposes these to the user rather than running them.
    proposed: list[dict[str, Any]]
    changed_tasks: bool


# The model is asked to reply with one JSON object, which keeps this working on small
# local models that have no native tool-calling support.
DECISION_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "thought": {"type": "string"},
        "tool": {"type": ["string", "null"]},
        "arguments": {"type": "object"},
        "reply": {"type": ["string", "null"]},
    },
    "required": [],
}

DECISION_INSTRUCTIONS = """\
Respond with a single JSON object and nothing else.

To use a tool:
  {{"tool": "<name>", "arguments": {{...}}}}

To answer the user:
  {{"reply": "<your answer>"}}

Available tools:
{tools}
"""


def build_graph(llm: LLMProvider, executor: ToolExecutor, settings: Settings):
    """The agent loop.

    agent → tools → agent → … until the model replies, or the iteration cap is hit.
    Read tools execute inside the loop because the model needs their results to reason.
    Writes are collected and handed back for confirmation instead of being applied.
    """

    tool_catalogue = json.dumps(
        [{"name": t.name, "description": t.description, "parameters": t.parameters}
         for t in TOOLS], indent=2)

    async def agent(state: AgentState) -> dict[str, Any]:
        system = state["messages"][0].content
        conversation = _render(state["messages"][1:])
        prompt = (
            f"{conversation}\n\n"
            + DECISION_INSTRUCTIONS.format(tools=tool_catalogue)
        )
        try:
            decision = await llm.generate_structured(prompt, DECISION_SCHEMA, system=system)
        except ProviderError as exc:
            return {"messages": [AIMessage(content=f"__error__:{exc}")],
                    "iterations": state["iterations"] + 1}

        tool = (decision.get("tool") or "").strip() or None
        # An unknown name still goes to the tools node, which reports the error back
        # so the model can correct itself rather than the turn ending silently.
        if tool:
            return {
                "messages": [AIMessage(
                    content="",
                    tool_calls=[{"id": f"call-{state['iterations']}", "name": tool,
                                 "args": decision.get("arguments") or {}}])],
                "iterations": state["iterations"] + 1,
            }

        reply = (decision.get("reply") or decision.get("thought") or "").strip()
        return {"messages": [AIMessage(content=reply or "Done.")],
                "iterations": state["iterations"] + 1}

    async def tools(state: AgentState) -> dict[str, Any]:
        last = state["messages"][-1]
        results: list[AnyMessage] = []
        proposed: list[dict[str, Any]] = []
        changed = False

        for call in getattr(last, "tool_calls", []) or []:
            name = call["name"]
            args = call.get("args") or {}
            spec = TOOLS_BY_NAME.get(name)

            if spec is None:
                results.append(ToolMessage(tool_call_id=call["id"], name=name,
                                           content=json.dumps({"ok": False, "error": "unknown tool"})))
                continue

            if name not in READ_ONLY:
                # A write. Describe it and stop — the user decides.
                proposed.append({"tool": name, "arguments": args})
                results.append(ToolMessage(
                    tool_call_id=call["id"], name=name,
                    content=json.dumps({
                        "ok": True,
                        "status": "proposed_to_user",
                        "note": "Not executed. The user confirms writes in the app.",
                    })))
                continue

            try:
                outcome = await executor.execute(name, args)
                changed = changed or outcome.changed_tasks
                payload = {"ok": outcome.ok, "summary": outcome.summary, "result": outcome.result}
            except Exception as exc:  # noqa: BLE001 - report to the model, do not crash the turn
                payload = {"ok": False, "error": str(exc)}
            results.append(ToolMessage(tool_call_id=call["id"], name=name,
                                       content=json.dumps(payload, default=str)[:6000]))

        return {"messages": results, "proposed": proposed, "changed_tasks": changed}

    def route(state: AgentState) -> Literal["tools", "__end__"]:
        last = state["messages"][-1]
        if not isinstance(last, AIMessage) or not getattr(last, "tool_calls", None):
            return END
        if state["iterations"] >= settings.agent_max_iterations:
            log.warning("agent hit the iteration cap")
            return END
        return "tools"

    def after_tools(state: AgentState) -> Literal["agent", "__end__"]:
        # Once a write is proposed the turn is over: the user is the next actor.
        return END if state.get("proposed") else "agent"

    builder = StateGraph(AgentState)
    builder.add_node("agent", agent)
    builder.add_node("tools", tools)
    builder.add_edge(START, "agent")
    builder.add_conditional_edges("agent", route, {"tools": "tools", END: END})
    builder.add_conditional_edges("tools", after_tools, {"agent": "agent", END: END})
    return builder.compile()


def system_message(context: dict[str, Any]) -> SystemMessage:
    columns = context.get("byStatus") or {}
    column_line = "columns: " + ", ".join(f"{k}={v}" for k, v in sorted(columns.items())) \
        if columns else ""

    agenda_lines = []
    for task in (context.get("agenda") or [])[:10]:
        bits = [f"  - id={task.get('id')}", f"title={task.get('title')!r}",
                f"status={task.get('status')}", f"priority={task.get('priority')}"]
        if task.get("due"):
            bits.append(f"due={task['due']}")
        if task.get("overdue"):
            bits.append("OVERDUE")
        agenda_lines.append(" ".join(bits))
    agenda = "agenda:\n" + "\n".join(agenda_lines) if agenda_lines else "agenda: empty"

    board = BOARD_TEMPLATE.format(
        open=context.get("open", 0), active=context.get("active", 0),
        due_today=context.get("dueToday", 0), overdue=context.get("overdue", 0),
        done_today=context.get("doneToday", 0),
        columns=column_line, agenda=agenda)
    return SystemMessage(content=AGENT_SYSTEM.format(now=now_label(), board=board))


def _render(messages: list[AnyMessage]) -> str:
    """Flattens the conversation into a transcript.

    Small local models handle a plain transcript far more reliably than a structured
    multi-turn tool-call format, and this keeps every provider on the same path.
    """
    lines: list[str] = []
    for message in messages:
        if isinstance(message, HumanMessage):
            lines.append(f"User: {message.content}")
        elif isinstance(message, ToolMessage):
            lines.append(f"Tool result ({message.name}): {message.content}")
        elif isinstance(message, AIMessage):
            calls = getattr(message, "tool_calls", None)
            if calls:
                for call in calls:
                    lines.append(f"You called {call['name']} with {json.dumps(call.get('args') or {})}")
            elif message.content:
                lines.append(f"You: {message.content}")
    return "\n".join(lines)
