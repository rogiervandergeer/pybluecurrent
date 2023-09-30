from datetime import datetime
from typing import Any


def parse_datetime_keys(source: dict[str, Any], formats: dict[str, tuple[str, bool]]) -> dict[str, Any]:
    """
    Parse date(time) keys in a dictionary.

    Args:
        source: The dictionary to parse datetimes in.
        formats: Expected date(time) formats: a dictionary

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
                result = datetime.strptime(source[key], datetime_format)
                source[key] = result.date() if is_date else result
    return source


def parse_list_datetime_keys(
    source: list[dict[str, Any]], formats: dict[str, tuple[str, bool]]
) -> list[dict[str, Any]]:
    """Apply parse_datetime_keys on all elements in a list."""
    return [parse_datetime_keys(s, formats) for s in source]
