"""Remembering which transactions were published already."""

from datetime import date, datetime, timedelta
from json import dumps, loads
from logging import getLogger
from pathlib import Path

logger = getLogger(__name__)


class TransactionStore:
    """The transactions published per charge point, so none is published twice.

    Held as the ids that were published, with the day each session ended. The backend's date filter
    works on that day too, so whatever it returns is either still remembered here or new.

    Without a path the store lives in memory only, so a restart republishes its window once.
    """

    def __init__(self, path: Path | None = None, lookback_days: int = 28, rescan_days: int = 2) -> None:
        self.path = path
        self.lookback_days = lookback_days
        self.rescan_days = rescan_days
        self._published: dict[str, dict[int, date]] = {}
        self._load()

    def seen(self, evse_id: str, transaction_id: int) -> bool:
        return transaction_id in self._published.get(evse_id, {})

    def add(self, evse_id: str, transaction_id: int, ended_at: datetime) -> None:
        self._published.setdefault(evse_id, {})[transaction_id] = ended_at.date()

    def start_date(self, evse_id: str) -> date | None:
        """The oldest end day still worth asking the backend for, or None for "all history".

        The first run scans back ``lookback_days``; later runs rescan ``rescan_days`` before the
        newest known end, for a session the backend lists late. Never before the horizon, though:
        what ended earlier was forgotten, and fetching it again would republish it.
        """
        horizon = self._horizon()
        days = self._published.get(evse_id, {}).values()
        if not days:
            return horizon
        start = max(days) - timedelta(days=self.rescan_days)
        return start if horizon is None else max(start, horizon)

    def save(self) -> None:
        """Write the store, if it has a path: to a temporary file first, so a crash cannot truncate it."""
        if self.path is None:
            return
        self._prune()
        temporary = self.path.with_suffix(f"{self.path.suffix}.tmp")
        try:
            # json turns the int ids into strings, and str() renders a date as ISO 8601.
            temporary.write_text(dumps(self._published, default=str))
            temporary.replace(self.path)
        except OSError as error:
            logger.error("Could not write the transaction state file: %s", error)

    def _prune(self) -> None:
        """Forget what ended before the horizon: start_date never reaches back that far again."""
        horizon = self._horizon()
        if horizon is None:
            return
        self._published = {
            evse_id: {transaction_id: day for transaction_id, day in published.items() if day >= horizon}
            for evse_id, published in self._published.items()
        }

    def _horizon(self) -> date | None:
        """The oldest day ever scanned, or None when the whole history is."""
        return date.today() - timedelta(days=self.lookback_days) if self.lookback_days > 0 else None

    def _load(self) -> None:
        if self.path is None or not self.path.exists():
            return
        try:
            data = loads(self.path.read_text())
            self._published = {
                evse_id: {int(transaction_id): date.fromisoformat(day) for transaction_id, day in published.items()}
                for evse_id, published in data.items()
            }
        except Exception as error:
            # A corrupt or unreadable file must not stop the relay, whatever shape it is in: start
            # empty and republish the window once.
            logger.error("Could not read the transaction state file, starting empty: %s", error)
            self._published = {}
