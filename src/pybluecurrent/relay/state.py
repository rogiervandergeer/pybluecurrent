"""Remembering which transactions were published already."""

from datetime import date, datetime, timedelta
from json import dumps, loads
from logging import getLogger
from os import replace
from pathlib import Path

logger = getLogger(__name__)


class TransactionStore:
    """The transactions published per charge point, so none is published twice.

    Held as the ids that were published, with the day each session started, rather than as a
    high-water mark: within one socket transactions arrive in order, but another socket — or another
    charge point — can finish a session that started earlier, which a watermark would skip forever.

    Without a path the store lives in memory only, so a restart republishes its window once.
    """

    def __init__(self, path: Path | None = None, lookback_days: int = 28, rescan_days: int = 2) -> None:
        self.path = path
        self.lookback_days = lookback_days
        self.rescan_days = rescan_days
        self._published: dict[str, dict[int, date | None]] = {}
        self._load()

    def seen(self, evse_id: str, transaction_id: int) -> bool:
        return transaction_id in self._published.get(evse_id, {})

    def add(self, evse_id: str, transaction_id: int, started_at: datetime | None) -> None:
        self._published.setdefault(evse_id, {})[transaction_id] = started_at.date() if started_at else None

    def start_date(self, evse_id: str) -> date | None:
        """The oldest day still worth asking the backend for, or None for "all history".

        The first run scans back ``lookback_days``; later runs rescan only the window in which a
        session that is already known could still be joined by one that started before it but
        finished later.
        """
        days = [day for day in self._published.get(evse_id, {}).values() if day is not None]
        if days:
            # The backend filters by date, so the overlap is counted in days, not hours.
            return max(days) - timedelta(days=self.rescan_days)
        if self.lookback_days <= 0:
            return None
        return date.today() - timedelta(days=self.lookback_days)

    def save(self) -> None:
        """Write the store, if it has a path: to a temporary file first, so a crash cannot truncate it."""
        if self.path is None:
            return
        self._prune()
        data = {
            evse_id: {
                str(transaction_id): day.isoformat() if day else None for transaction_id, day in published.items()
            }
            for evse_id, published in self._published.items()
        }
        temporary = self.path.with_suffix(f"{self.path.suffix}.tmp")
        try:
            temporary.write_text(dumps(data))
            replace(temporary, self.path)
        except OSError as error:
            logger.error("Could not write the transaction state file: %s", error)

    def _prune(self) -> None:
        """Forget what is older than the scan window: it can never be fetched, so never republished."""
        if self.lookback_days <= 0:
            return
        horizon = date.today() - timedelta(days=self.lookback_days)
        for published in self._published.values():
            for transaction_id, day in list(published.items()):
                if day is not None and day < horizon:
                    del published[transaction_id]

    def _load(self) -> None:
        if self.path is None or not self.path.exists():
            return
        try:
            data = loads(self.path.read_text())
            self._published = {
                evse_id: {
                    int(transaction_id): date.fromisoformat(day) if day else None
                    for transaction_id, day in published.items()
                }
                for evse_id, published in data.items()
            }
        except Exception as error:
            # A corrupt or unreadable file must not stop the relay, whatever shape it is in: start
            # empty and republish the window once.
            logger.error("Could not read the transaction state file, starting empty: %s", error)
            self._published = {}
