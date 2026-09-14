"""Turning a response into the JSON the relay publishes."""

from datetime import date, datetime, time, timezone
from json import dumps
from typing import Any, Mapping

from pybluecurrent.utilities import format_time


def now() -> str:
    """The current time, as the relay stamps its messages: UTC, ISO 8601, with an offset."""
    return datetime.now(timezone.utc).isoformat()


def encode(payload: Mapping[str, Any]) -> str:
    """Render a message as JSON, with a "timestamp" of its own added."""
    return dumps({**_convert(payload), "timestamp": now()}, ensure_ascii=False)


def _convert(value: Any) -> Any:
    """Render dates, datetimes and times as strings, recursing into nested values.

    The backend's datetimes are naive local times, so they are read in the timezone the relay runs
    in — run it where the charge point is — and rendered as UTC with an offset, which every consumer
    can parse without knowing that. Times of day stay "HH:MM", the format the setters accept.
    """
    if isinstance(value, time):
        return format_time(value)
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc).isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, dict):
        return {key: _convert(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_convert(item) for item in value]
    return value
