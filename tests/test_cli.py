from csv import DictReader
from datetime import date, datetime, timedelta
from io import StringIO
from json import loads
from pathlib import Path

from fake_rest import FakeRest, make_fake_async_client
from fake_socket import FakeSocket, load_fixture, make_fake_connect
from pytest import MonkeyPatch, fixture
from typer.testing import CliRunner

from pybluecurrent.cli import app
from pybluecurrent.cli.transactions import FIELDNAMES
from pybluecurrent.utilities import format_date, to_jsonable

CREDENTIALS = ["--username", "username", "--password", "password"]


@fixture(scope="function")
def runner(monkeypatch: MonkeyPatch, fake_socket: FakeSocket, fake_rest: FakeRest) -> CliRunner:
    """A CliRunner whose commands talk to the offline fakes instead of the network."""
    monkeypatch.setattr("pybluecurrent.client.connect", make_fake_connect(fake_socket))
    monkeypatch.setattr("pybluecurrent.client.AsyncClient", make_fake_async_client(fake_rest))
    # Keep a developer's real credentials out of the tests.
    for variable in ("BLUECURRENT_USERNAME", "BLUECURRENT_PASSWORD", "BLUECURRENT_API_TOKEN"):
        monkeypatch.delenv(variable, raising=False)
    fake_rest.on("gettransactions", load_fixture("transactions"))
    fake_socket.on("GET_CHARGE_POINTS", load_fixture("charge_points"))
    return CliRunner()


def export(runner: CliRunner, *arguments: str):
    result = runner.invoke(app, ["transactions", *CREDENTIALS, "--evse-id", "BCU123456", *arguments])
    assert result.exit_code == 0, result.output
    return result


class TestFormats:
    def test_csv_has_the_model_column_order(self, runner: CliRunner):
        rows = list(DictReader(StringIO(export(runner).stdout)))
        assert list(rows[0]) == FIELDNAMES
        assert len(rows) == 3

    def test_csv_leaves_a_missing_optional_field_empty(self, runner: CliRunner):
        rows = {row["transaction_id"]: row for row in DictReader(StringIO(export(runner).stdout))}
        assert rows["1"]["reason_no_settlement"] == "not settled"
        assert rows["3"]["reason_no_settlement"] == ""

    def test_json_is_an_array_with_iso_datetimes(self, runner: CliRunner):
        rows = loads(export(runner, "--format", "json").stdout)
        assert [row["transaction_id"] for row in rows] == [3, 2, 1]
        assert rows[0]["started_at"] == "2026-07-03T10:00:00"

    def test_jsonl_is_one_object_per_line(self, runner: CliRunner):
        lines = export(runner, "--format", "jsonl").stdout.splitlines()
        assert len(lines) == 3
        assert [loads(line)["transaction_id"] for line in lines] == [3, 2, 1]


class TestOrdering:
    def test_newest_first_by_default(self, runner: CliRunner):
        rows = loads(export(runner, "--format", "json").stdout)
        assert [row["transaction_id"] for row in rows] == [3, 2, 1]

    def test_oldest_first(self, runner: CliRunner):
        rows = loads(export(runner, "--format", "json", "--oldest-first").stdout)
        assert [row["transaction_id"] for row in rows] == [1, 2, 3]

    def test_sorts_across_charge_points(self, runner: CliRunner):
        """Two charge points are one sorted series, not two concatenated blocks."""
        result = runner.invoke(
            app,
            ["transactions", *CREDENTIALS, "--evse-id", "BCU123456", "--evse-id", "BCU200005", "--format", "json"],
        )
        assert result.exit_code == 0, result.output
        # The fake answers both charge points with the same page, so every id appears twice.
        assert [row["transaction_id"] for row in loads(result.stdout)] == [3, 3, 2, 2, 1, 1]


class TestDateFiltering:
    def test_days_sends_start_date(self, runner: CliRunner, fake_rest: FakeRest):
        export(runner, "--days", "7")
        query = str(fake_rest.requests[-1].url)
        assert f"start_date={format_date(date.today() - timedelta(days=7))}" in query

    def test_without_days_neither_bound_is_sent(self, runner: CliRunner, fake_rest: FakeRest):
        export(runner)
        query = str(fake_rest.requests[-1].url)
        assert "start_date" not in query
        assert "end_date" not in query


class TestChargePointSelection:
    def test_defaults_to_discovered_charge_points(self, runner: CliRunner, fake_rest: FakeRest):
        result = runner.invoke(app, ["transactions", *CREDENTIALS, "--format", "json"])
        assert result.exit_code == 0, result.output
        assert fake_rest.last_body["chargepoints"] == [{"chargepoint_id": "BCU123456"}]

    def test_explicit_evse_id_is_used_verbatim(self, runner: CliRunner, fake_rest: FakeRest):
        export(runner, "--format", "json")
        assert fake_rest.last_body["chargepoints"] == [{"chargepoint_id": "BCU123456"}]


class TestOutput:
    def test_writes_to_a_file(self, runner: CliRunner, tmp_path: Path):
        target = tmp_path / "transactions.json"
        export(runner, "--format", "json", "-o", str(target))
        assert len(loads(target.read_text())) == 3

    def test_rejects_an_unknown_format(self, runner: CliRunner):
        assert runner.invoke(app, ["transactions", *CREDENTIALS, "--format", "bogus"]).exit_code != 0


class TestToJsonable:
    def test_renders_datetimes_and_leaves_the_rest(self):
        assert to_jsonable({"a": datetime(2026, 7, 3, 10, 0), "b": date(2026, 7, 3), "c": 1, "d": None}) == {
            "a": "2026-07-03T10:00:00",
            "b": "2026-07-03",
            "c": 1,
            "d": None,
        }
