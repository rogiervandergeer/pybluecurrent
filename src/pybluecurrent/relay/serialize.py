"""Turning a response into the JSON the relay publishes."""

from datetime import datetime, timezone
from json import dumps
from typing import Any, Mapping
from zoneinfo import ZoneInfo

from pybluecurrent.utilities import to_jsonable

# The backend's datetimes carry no timezone. As far as we know they are always Dutch time.
BACKEND_TIMEZONE = ZoneInfo("Europe/Amsterdam")


def to_utc(value: datetime) -> datetime:
    """A backend datetime in UTC; a naive one is read as BACKEND_TIMEZONE."""
    if value.tzinfo is None:
        value = value.replace(tzinfo=BACKEND_TIMEZONE)
    return value.astimezone(timezone.utc)


def encode(payload: Mapping[str, Any]) -> str:
    """Render a message as JSON, with a "timestamp" of its own added: UTC, ISO 8601, with an offset.

    Naive datetimes are read as BACKEND_TIMEZONE and rendered as UTC. Times of day stay "HH:MM", the
    format the setters accept.
    """
    message = {**payload, "timestamp": datetime.now(timezone.utc).isoformat()}
    return dumps(to_jsonable(message, render_datetime=lambda value: to_utc(value).isoformat()), ensure_ascii=False)
