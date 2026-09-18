from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

_RELATIVE = {
    "today": 0, "tod": 0,
    "tomorrow": 1, "tmr": 1, "tom": 1,
    "yesterday": -1,
    "next week": 7, "nextweek": 7,
}
_WEEKDAYS = {"monday": 0, "mon": 0, "tuesday": 1, "tue": 1, "tues": 1, "wednesday": 2, "wed": 2,
             "thursday": 3, "thu": 3, "thurs": 3, "friday": 4, "fri": 4,
             "saturday": 5, "sat": 5, "sunday": 6, "sun": 6}
_CLEAR = {"", "none", "clear", "never", "-", "null"}
_OFFSET = re.compile(r"^\+?(\d+)([dwm])$")


def parse(value: str | None, *, now: datetime | None = None) -> datetime | None:
    """Accepts what the Mac app and the agent actually send: ISO-8601, or a small set
    of relative forms. Mirrors DateInput.parse in the Swift client."""
    if value is None:
        return None
    text = value.strip().lower()
    if text in _CLEAR:
        return None

    now = now or datetime.now(timezone.utc)

    if text in _RELATIVE:
        return _end_of_day(now + timedelta(days=_RELATIVE[text]))

    match = _OFFSET.match(text)
    if match:
        amount, unit = int(match.group(1)), match.group(2)
        days = {"d": 1, "w": 7, "m": 30}[unit] * amount
        return _end_of_day(now + timedelta(days=days))

    if text in _WEEKDAYS:
        target = _WEEKDAYS[text]
        ahead = (target - now.weekday()) % 7 or 7
        return _end_of_day(now + timedelta(days=ahead))

    iso = value.strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(iso)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def is_clear(value: str | None) -> bool:
    """True when the caller means 'remove the due date' rather than 'leave it alone'."""
    return value is not None and value.strip().lower() in _CLEAR


def iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _end_of_day(value: datetime) -> datetime:
    return value.replace(hour=17, minute=0, second=0, microsecond=0)
