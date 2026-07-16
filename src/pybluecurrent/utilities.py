from datetime import datetime, time
from typing import Any


def format_time(value: time | str) -> str:
    """Format a time of day as "HH:MM". Strings are parsed first, so that "9:30" becomes "09:30"."""
    return (datetime.strptime(value, "%H:%M").time() if isinstance(value, str) else value).strftime("%H:%M")


def parse_datetime_keys(
    source: dict[str, Any], formats: dict[str, tuple[str | tuple[str, ...], bool]]
) -> dict[str, Any]:
    """
    Parse date(time) keys in a dictionary.

    Args:
        source: The dictionary to parse datetimes in.
        formats: Expected date(time) formats: a dictionary mapping each key to a
            (format, is_date) tuple. The format may be a single strptime format string,
            or a tuple of format strings that are tried in order until one parses.

    Returns:
        The source dictionary, where keys matching any of the formats have been parsed to datetimes.
        Any empty strings in matching keys will be converted to None.

    For example, with
    source = {"a": 1, "b": "01-JAN-20", "c": ""}
    formats = {"b": ("%d-%b-%y", True), "c": ("%Y-%m-%d %H:%M:%S", False)}
    the result is {"a": 1, "b": date(2000, 1, 1), "c": None}
    """
    for key, (datetime_format, is_date) in formats.items():
        if key in source and source[key] is not None:
            if source[key] == "":
                source[key] = None
            else:
                result = _parse_datetime(source[key], datetime_format)
                source[key] = result.date() if is_date else result
    return source


def _parse_datetime(value: str, datetime_format: str | tuple[str, ...]) -> datetime:
    """Parse value with the first matching format (a single format, or several tried in order)."""
    candidates = (datetime_format,) if isinstance(datetime_format, str) else datetime_format
    for candidate in candidates:
        try:
            return datetime.strptime(value, candidate)
        except ValueError:
            continue
    raise ValueError(f"time data {value!r} does not match any of {candidates!r}")


def parse_list_datetime_keys(
    source: list[dict[str, Any]], formats: dict[str, tuple[str | tuple[str, ...], bool]]
) -> list[dict[str, Any]]:
    """Apply parse_datetime_keys on all elements in a list."""
    return [parse_datetime_keys(s, formats) for s in source]
