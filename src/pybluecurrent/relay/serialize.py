"""Turning a response into the JSON the relay publishes."""

from datetime import date, datetime, time, timezone
from json import dumps
from typing import Any, Mapping
from zoneinfo import ZoneInfo

from pybluecurrent.utilities import format_time

# The backend's datetimes carry no timezone. As far as we know they are always Dutch time.
BACKEND_TIMEZONE = ZoneInfo("Europe/Amsterdam")


def now() -> str:
    """The current time, as the relay stamps its messages: UTC, ISO 8601, with an offset."""
    return datetime.now(timezone.utc).isoformat()


def encode(payload: Mapping[str, Any]) -> str:
    """Render a message as JSON, with a "timestamp" of its own added."""
    return dumps({**_convert(payload), "timestamp": now()}, ensure_ascii=False)


def _convert(value: Any) -> Any:
    """Render dates, datetimes and times as strings, recursing into nested values.

    Naive datetimes are read as BACKEND_TIMEZONE and rendered as UTC. Times of day stay "HH:MM", the
    format the setters accept.
    """
    if isinstance(value, time):
        return format_time(value)
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=BACKEND_TIMEZONE)
        return value.astimezone(timezone.utc).isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, dict):
        return {key: _convert(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_convert(item) for item in value]
    return value
