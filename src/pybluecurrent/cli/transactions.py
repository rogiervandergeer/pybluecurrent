"""The ``transactions`` command: export charging transactions to CSV, JSON or JSON Lines."""

from asyncio import run
from csv import DictWriter
from datetime import date, datetime, timedelta
from enum import Enum
from io import StringIO
from json import dumps
from pathlib import Path
from sys import stderr
from typing import Annotated, Any

from httpx import HTTPError
from typer import BadParameter, Exit, Option, echo

from pybluecurrent.client import BlueCurrentClient
from pybluecurrent.models import Transaction
from pybluecurrent.utilities import to_jsonable

# Column order for CSV, following the Transaction model.
FIELDNAMES = [
    "transaction_id",
    "chargepoint_id",
    "chargepoint_type",
    "evse_name",
    "started_at",
    "end_time",
    "kwh",
    "card_id",
    "card_name",
    "total_costs",
    "total_costs_ex_vat",
    "reimbursement_tariff_ex_vat",
    "vat",
    "currency",
    "reason_no_settlement",
]


class Format(str, Enum):
    csv = "csv"
    json = "json"
    jsonl = "jsonl"


def transactions(
    evse_id: Annotated[
        list[str] | None,
        Option(
            "--evse-id",
            help="Charge point to export, repeatable. Defaults to all of your charge points.",
        ),
    ] = None,
    format: Annotated[Format, Option("--format", help="Output format.")] = Format.csv,
    days: Annotated[int | None, Option("--days", help="Only export the last N days. Defaults to all.")] = None,
    output: Annotated[Path | None, Option("-o", "--output", help="Write to a file instead of stdout.")] = None,
    newest_first: Annotated[bool, Option("--newest-first/--oldest-first", help="Output order.")] = True,
    username: Annotated[str | None, Option(envvar="BLUECURRENT_USERNAME")] = None,
    password: Annotated[str | None, Option(envvar="BLUECURRENT_PASSWORD")] = None,
    api_token: Annotated[str | None, Option(envvar="BLUECURRENT_API_TOKEN")] = None,
) -> None:
    """Export your charging transactions."""
    rendered = run(_export(evse_id, format, days, newest_first, username, password, api_token))
    if output is None:
        echo(rendered, nl=False)
    else:
        output.write_text(rendered)


async def _export(
    evse_ids: list[str] | None,
    format: Format,
    days: int | None,
    newest_first: bool,
    username: str | None,
    password: str | None,
    api_token: str | None,
) -> str:
    try:
        client = BlueCurrentClient(username=username, password=password, api_token=api_token)
    except ValueError as error:
        raise BadParameter(str(error))
    client.auto_reconnect = False  # one-shot run; no point reconnecting
    async with client:
        if not evse_ids:
            evse_ids = [charge_point["evse_id"] for charge_point in await client.get_charge_points()]
        rows = await _collect(client, evse_ids, days, newest_first)
    return _render(rows, format)


async def _collect(
    client: BlueCurrentClient, evse_ids: list[str], days: int | None, newest_first: bool
) -> list[Transaction]:
    """Gather transactions for every charge point, then sort them as one series."""
    start_date = date.today() - timedelta(days=days) if days is not None else None
    rows: list[Transaction] = []
    failed = []
    for evse_id in evse_ids:
        try:
            async for transaction in client.iterate_transactions(
                evse_id=evse_id, newest_first=True, start_date=start_date
            ):
                rows.append(transaction)
        except HTTPError as error:
            # One inaccessible charge point (e.g. a former one) must not sink the whole export.
            print(f"warning: could not read transactions for {evse_id}: {error}", file=stderr)
            failed.append(evse_id)
    if failed and len(failed) == len(evse_ids):
        raise Exit(code=1)
    rows.sort(key=lambda row: row.get("started_at") or datetime.min, reverse=newest_first)
    return rows


def _render(rows: list[Transaction], format: Format) -> str:
    jsonable = [to_jsonable(dict(row)) for row in rows]
    if format is Format.json:
        return dumps(jsonable, indent=2) + "\n"
    if format is Format.jsonl:
        return "".join(f"{dumps(row)}\n" for row in jsonable)
    return _to_csv(jsonable)


def _to_csv(rows: list[dict[str, Any]]) -> str:
    buffer = StringIO()
    writer = DictWriter(buffer, fieldnames=FIELDNAMES, extrasaction="ignore")
    writer.writeheader()
    writer.writerows(rows)
    return buffer.getvalue()
