from enum import IntEnum
from typing import Any


class Weekday(IntEnum):
    """A day of the week, numbered like datetime.date.isoweekday()."""

    MONDAY = 1
    TUESDAY = 2
    WEDNESDAY = 3
    THURSDAY = 4
    FRIDAY = 5
    SATURDAY = 6
    SUNDAY = 7

    @classmethod
    def _missing_(cls, value: Any) -> "Weekday | None":
        """Resolve a weekday from its name, or from any unambiguous abbreviation of it."""
        if isinstance(value, str):
            # Two letters are enough to identify any weekday, so anything shorter is ambiguous.
            name = value.strip().upper()
            if len(name) >= 2:
                for member in cls:
                    if member.name.startswith(name):
                        return member
        return None
