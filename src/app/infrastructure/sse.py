from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from typing import Any


def format_event(data: Any, event: str | None = None) -> str:
    """One server-sent event. Every line of the payload is prefixed, per the spec."""
    payload = data if isinstance(data, str) else json.dumps(data, default=str)
    lines = []
    if event:
        lines.append(f"event: {event}")
    for line in payload.splitlines() or [""]:
        lines.append(f"data: {line}")
    return "\n".join(lines) + "\n\n"


def comment(text: str = "keep-alive") -> str:
    """SSE comment. Keeps proxies from closing an idle stream."""
    return f": {text}\n\n"


async def poll_stream(
    fetch,
    *,
    is_terminal,
    interval: float = 1.0,
    keepalive: float = 15.0,
    timeout: float = 3 * 3600,
) -> AsyncIterator[str]:
    """Turns a pollable resource into an event stream.

    Deliberately a poll rather than a pub/sub fan-out: job state changes a handful of
    times over several minutes, and one indexed primary-key read per second costs far
    less than running a broker. Only emits when something actually changed.
    """
    previous: Any = None
    elapsed = 0.0
    since_keepalive = 0.0

    while elapsed < timeout:
        current = await fetch()
        if current is None:
            yield format_event({"error": "not_found"}, event="error")
            return

        if current != previous:
            yield format_event(current)
            previous = current
            since_keepalive = 0.0
            if is_terminal(current):
                return
        elif since_keepalive >= keepalive:
            yield comment()
            since_keepalive = 0.0

        await asyncio.sleep(interval)
        elapsed += interval
        since_keepalive += interval

    yield format_event({"error": "stream_timeout"}, event="error")
