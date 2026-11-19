"""The MQTT relay, driven by the offline websocket, REST and MQTT fakes."""

from asyncio import CancelledError, Event, Task, create_task, get_running_loop, sleep, wait_for
from contextlib import suppress
from copy import deepcopy
from dataclasses import fields, replace
from datetime import date, datetime, time, timedelta
from inspect import signature
from json import dumps, loads
from logging import INFO
from pathlib import Path
from signal import SIG_DFL, SIGTERM, getsignal, raise_signal, signal
from sys import modules
from time import monotonic

from aiomqtt import MqttError
from fake_mqtt import FakeMqtt, make_fake_mqtt
from fake_rest import FakeRest, make_fake_async_client
from fake_socket import FakeSocket, load_fixture, make_fake_connect
from pytest import fixture, mark, raises
from typer.testing import CliRunner

from pybluecurrent import BlueCurrentClient
from pybluecurrent.cli import app
from pybluecurrent.cli.relay import relay as relay_command
from pybluecurrent.exceptions import ConnectionLost
from pybluecurrent.relay.commands import run_command
from pybluecurrent.relay.relay import _Relay, _serve, _with_duration, run
from pybluecurrent.relay.serialize import encode
from pybluecurrent.relay.settings import Settings
from pybluecurrent.relay.state import TransactionStore

EVSE_ID = "BCU123456"


async def _wait_until(predicate, timeout: float = 2) -> None:
    """Yield control to the event loop until ``predicate()`` holds; fail after ``timeout`` seconds."""

    async def poll() -> None:
        while not predicate():
            await sleep(0)

    await wait_for(poll(), timeout)


async def _stop(task: Task) -> None:
    task.cancel()
    with suppress(CancelledError):
        await task


@fixture
def settings() -> Settings:
    return Settings(poll_interval=1, settings_interval=1, heartbeat_interval=1)


@fixture
def fake_mqtt() -> FakeMqtt:
    return FakeMqtt()


def _relay(client: BlueCurrentClient, mqtt: FakeMqtt, settings: Settings, store: TransactionStore) -> _Relay:
    """A relay on a connection, as _serve gives it one."""
    relay = _Relay(client, settings, store)
    relay.mqtt = mqtt
    return relay


@fixture
def relay(offline_client: BlueCurrentClient, fake_mqtt: FakeMqtt, settings: Settings) -> _Relay:
    """A relay that knows its charge point, as if the settings loop had read it already."""
    relay = _relay(offline_client, fake_mqtt, settings, TransactionStore())
    relay.evse_ids = [EVSE_ID]
    relay.charge_points_known.set()
    return relay


class TestSerialize:
    def test_stamps_and_renders(self):
        payload = {
            "started_at": datetime(2026, 9, 12, 10, 30),
            "day": date(2026, 9, 12),
            "delayed_charging": {"start_time": time(23, 0), "days": [1, 2]},
        }
        message = encode(payload)
        assert '"delayed_charging": {"start_time": "23:00", "days": [1, 2]}' in message
        assert '"day": "2026-09-12"' in message
        assert '"started_at": "2026-09-12T08:30:00+00:00"' in message  # summer time: UTC+2
        assert '"started_at": "2026-01-12T09:30:00+00:00"' in encode({"started_at": datetime(2026, 1, 12, 10, 30)})
        assert '"timestamp": "' in message and "+00:00" in message


class TestTransactionStore:
    def test_remembers_what_was_published(self):
        store = TransactionStore()
        assert not store.seen(EVSE_ID, 1)
        store.add(EVSE_ID, 1, datetime(2026, 9, 1, 12, 0))
        assert store.seen(EVSE_ID, 1) and not store.seen("BCU999999", 1)

    def test_first_scan_uses_the_lookback(self):
        store = TransactionStore(lookback_days=28)
        assert store.start_date(EVSE_ID) == date.today() - timedelta(days=28)

    def test_all_history_when_the_lookback_is_zero(self):
        assert TransactionStore(lookback_days=0).start_date(EVSE_ID) is None

    def test_later_scans_rescan_the_overlap_window(self):
        store = TransactionStore(rescan_days=2)
        store.add(EVSE_ID, 1, datetime.now() - timedelta(days=10))
        assert store.start_date(EVSE_ID) == date.today() - timedelta(days=12)

    def test_never_rescans_what_was_forgotten(self):
        # A session that ended 28 days ago rescans from 30 days ago, but what ended more than 28 days
        # ago was pruned: fetching it again would publish it a second time.
        store = TransactionStore(lookback_days=28, rescan_days=2)
        store.add(EVSE_ID, 1, datetime.now() - timedelta(days=28))
        assert store.start_date(EVSE_ID) == date.today() - timedelta(days=28)

    def test_survives_a_restart(self, tmp_path: Path):
        path = tmp_path / "state.json"
        store = TransactionStore(path)
        store.add(EVSE_ID, 7, datetime.now())
        store.save()
        assert TransactionStore(path).seen(EVSE_ID, 7)

    def test_forgets_what_falls_out_of_the_window(self, tmp_path: Path):
        path = tmp_path / "state.json"
        store = TransactionStore(path, lookback_days=28)
        store.add(EVSE_ID, 1, datetime.now() - timedelta(days=90))
        store.add(EVSE_ID, 2, datetime.now())
        store.save()
        reloaded = TransactionStore(path)
        assert not reloaded.seen(EVSE_ID, 1) and reloaded.seen(EVSE_ID, 2)

    def test_starts_empty_on_an_unreadable_file(self, tmp_path: Path):
        path = tmp_path / "state.json"
        path.write_text("{not json")
        assert not TransactionStore(path).seen(EVSE_ID, 1)


class TestStatus:
    async def test_publishes_every_socket_and_health(
        self, relay: _Relay, fake_socket: FakeSocket, fake_rest: FakeRest, fake_mqtt: FakeMqtt, settings: Settings
    ):
        fake_rest.on("chargepointstatus", load_fixture("charge_point_statuses"))
        task = create_task(relay._poll_statuses())
        await _wait_until(lambda: len(fake_mqtt.published(settings.health_topic)) > 0)
        await _stop(task)

        # The ids come from the settings loop: a poll asks the backend for statuses only.
        assert not [message for message in fake_socket.sent if message.get("command") == "GET_CHARGE_POINTS"]
        first = fake_mqtt.last(settings.status_topic(EVSE_ID, 1))
        assert first.payload["activity"] == "available"
        assert first.payload["evse_id"] == EVSE_ID and first.payload["socket_id"] == 1
        assert first.retain and first.qos == settings.mqtt_qos
        assert "timestamp" in first.payload
        assert settings.status_topic(EVSE_ID, 2) in fake_mqtt.topics()
        assert fake_mqtt.last(settings.health_topic).payload["status"] == "up"

    async def test_reports_a_failure_as_unhealthy(
        self, relay: _Relay, fake_rest: FakeRest, fake_mqtt: FakeMqtt, settings: Settings
    ):
        fake_rest.on("chargepointstatus", {"error": "nope"}, 500)
        task = create_task(relay._poll_statuses())
        await _wait_until(lambda: len(fake_mqtt.published(settings.health_topic)) > 0)
        await _stop(task)

        health = fake_mqtt.last(settings.health_topic)
        assert health.payload["status"] == "down" and health.payload["error"] == "HTTPStatusError"
        assert health.retain

    async def test_a_dropped_backend_is_reported_at_once(self, relay: _Relay, monkeypatch):
        # Without the check a read would wait out the reconnect first, and health says "up" meanwhile.
        monkeypatch.setattr(BlueCurrentClient, "connected", property(lambda self: False))
        assert await relay._read_statuses() == ([], "disconnected")

    async def test_waits_for_the_charge_points(
        self, offline_client: BlueCurrentClient, fake_rest: FakeRest, fake_mqtt: FakeMqtt
    ):
        # The first poll comes before the settings loop has read the charge points: it waits for them.
        fake_rest.on("chargepointstatus", load_fixture("charge_point_statuses"))
        relay = _relay(offline_client, fake_mqtt, Settings(poll_interval=2), TransactionStore())
        read = create_task(relay._read_statuses())
        await sleep(0)
        relay.evse_ids = [EVSE_ID]
        relay.charge_points_known.set()
        statuses, failure = await wait_for(read, 1)
        assert failure is None and len(statuses) == 2

    async def test_gives_up_waiting_after_one_poll(self, offline_client: BlueCurrentClient, fake_mqtt: FakeMqtt):
        relay = _relay(offline_client, fake_mqtt, Settings(poll_interval=0.01), TransactionStore())
        assert await relay._read_statuses() == ([], "no charge points yet")

    async def test_a_changed_activity_triggers_a_refresh(self, relay: _Relay):
        relay._note_activity(EVSE_ID, {"socket_id": 1, "activity": "available"})
        assert not relay.refresh_settings.is_set()  # the first reading is not a change
        relay._note_activity(EVSE_ID, {"socket_id": 1, "activity": "charging"})
        assert relay.refresh_settings.is_set() and relay.sync_transactions.is_set()


class TestSettings:
    async def test_publishes_the_settings_of_every_charge_point(
        self, offline_client: BlueCurrentClient, fake_socket: FakeSocket, fake_mqtt: FakeMqtt, settings: Settings
    ):
        relay = _relay(offline_client, fake_mqtt, settings, TransactionStore())
        fake_socket.on("GET_CHARGE_POINTS", load_fixture("charge_points"))
        task = create_task(relay._poll_settings())
        await _wait_until(lambda: settings.settings_topic(EVSE_ID) in fake_mqtt.topics())
        await _stop(task)

        assert relay.evse_ids == [EVSE_ID] and relay.charge_points_known.is_set()  # for the other tasks
        payload = fake_mqtt.last(settings.settings_topic(EVSE_ID)).payload
        assert payload["evse_id"] == EVSE_ID
        assert "delayed_charging" in payload and "price_based_charging" in payload

    async def test_retries_soon_until_the_first_read_succeeds(
        self, offline_client: BlueCurrentClient, fake_socket: FakeSocket, fake_mqtt: FakeMqtt, monkeypatch
    ):
        # The other tasks wait for the charge points, so a failed first read must not wait an hour.
        settings = Settings(poll_interval=0.01, settings_interval=60)
        relay = _relay(offline_client, fake_mqtt, settings, TransactionStore())
        fake_socket.on("GET_CHARGE_POINTS", load_fixture("charge_points"))
        fake_socket.on("GET_GRID_STATUS", load_fixture("grid_status"))
        read = offline_client.get_charge_points
        failures = [OSError("network")]

        async def fails_once():
            if failures:
                raise failures.pop()
            return await read()

        monkeypatch.setattr(offline_client, "get_charge_points", fails_once)
        task = create_task(relay._poll_settings())
        await _wait_until(lambda: settings.grid_topic(EVSE_ID) in fake_mqtt.topics())  # the read is done
        await sleep(0.1)  # ten poll intervals
        await _stop(task)
        # Then the settings interval: one read, not a second one a poll interval later.
        assert [message["command"] for message in fake_socket.sent].count("GET_CHARGE_POINTS") == 1

    async def test_a_read_cut_short_is_finished_on_the_next_connection(
        self, offline_client: BlueCurrentClient, fake_socket: FakeSocket, fake_mqtt: FakeMqtt, settings: Settings
    ):
        # GET_CHARGE_POINTS gets no answer: the read is still waiting when the connection ends.
        relay = _relay(offline_client, fake_mqtt, settings, TransactionStore())
        task = create_task(relay._poll_settings())
        await _wait_until(lambda: any(message.get("command") == "GET_CHARGE_POINTS" for message in fake_socket.sent))
        await _stop(task)
        assert relay.refresh_settings.is_set()  # or the next read waits for the settings interval


class TestStatusPush:
    async def test_updates_the_polled_status(
        self, relay: _Relay, fake_socket: FakeSocket, fake_rest: FakeRest, fake_mqtt: FakeMqtt, settings: Settings
    ):
        fake_rest.on("chargepointstatus", load_fixture("charge_point_statuses"))
        poll = create_task(relay._poll_statuses())
        await _wait_until(lambda: len(fake_mqtt.published(settings.health_topic)) > 0)
        await _stop(poll)
        polled = fake_mqtt.last(settings.status_topic(EVSE_ID, 1)).payload

        task = create_task(relay._follow_updates())
        await sleep(0)  # let it subscribe
        push = load_fixture("ch_status")  # as the backend sends it: the socket only at the top
        push["data"]["unheard_of"] = 1
        fake_socket.feed(push)
        await _wait_until(lambda: fake_mqtt.last(settings.status_topic(EVSE_ID, 1)).payload["activity"] == "charging")
        await _stop(task)

        pushed = fake_mqtt.last(settings.status_topic(EVSE_ID, 1)).payload
        assert pushed["actual_kwh"] == 18.48 and pushed["vehicle_status"] == "C"
        assert pushed["max_offline"] == 6
        for kept in ("smart_current_heartbeat_timeout", "start_datetime"):  # not in the push, or ""
            assert pushed[kept] == polled[kept]
        assert "unheard_of" not in pushed  # the topic keeps the shape of the poll
        assert relay.refresh_settings.is_set() and relay.sync_transactions.is_set()  # an activity change
        assert relay.poll_statuses.is_set()  # for the session's times, which the push leaves out


class TestGrid:
    async def test_the_settings_loop_fetches_it_then_the_pushes_update_it(
        self, offline_client: BlueCurrentClient, fake_socket: FakeSocket, fake_mqtt: FakeMqtt, settings: Settings
    ):
        relay = _relay(offline_client, fake_mqtt, settings, TransactionStore())
        fake_socket.on("GET_CHARGE_POINTS", load_fixture("charge_points"))
        fake_socket.on("GET_GRID_STATUS", load_fixture("grid_status"))
        tasks = [create_task(relay._follow_updates()), create_task(relay._poll_settings())]
        await _wait_until(lambda: settings.grid_topic(EVSE_ID) in fake_mqtt.topics())
        fetched = fake_mqtt.last(settings.grid_topic(EVSE_ID)).payload
        assert fetched["evse_id"] == EVSE_ID and "grid_max_install" in fetched

        fake_socket.feed({"object": "GRID_CURRENT", "evse_id": EVSE_ID, "grid_actual_p1": 9})
        await _wait_until(lambda: fake_mqtt.last(settings.grid_topic(EVSE_ID)).payload["grid_actual_p1"] == 9)
        relay.refresh_settings.set()  # every settings read fetches it again, as the pushes can stop
        await _wait_until(lambda: [m["command"] for m in fake_socket.sent].count("GET_GRID_STATUS") == 2)
        for task in tasks:
            await _stop(task)

        updated = fake_mqtt.last(settings.grid_topic(EVSE_ID)).payload
        assert updated["grid_max_install"] == fetched["grid_max_install"]  # the maximums are kept

    async def test_skips_pushes_before_the_fetch_and_logs_an_unknown_type_once(
        self, relay: _Relay, fake_socket: FakeSocket, fake_mqtt: FakeMqtt, settings: Settings, caplog
    ):
        caplog.set_level(INFO, logger="pybluecurrent.relay.relay")
        task = create_task(relay._follow_updates())
        await sleep(0)  # let it subscribe
        fake_socket.feed({"object": "GRID_CURRENT", "evse_id": EVSE_ID, "grid_actual_p1": 7})
        push = {"object": "CH_STATUS", "evse_id": EVSE_ID, "socket_id": 1, "data": {"activity": "charging"}}
        fake_socket.feed(push)
        fake_socket.feed({"object": "SOMETHING_NEW"})
        fake_socket.feed({"object": "SOMETHING_NEW"})
        fake_socket.feed({"object": "LAST_ONE"})  # queued behind the others: seeing it means all were handled
        await _wait_until(lambda: "LAST_ONE" in caplog.text)
        await _stop(task)
        assert settings.grid_topic(EVSE_ID) not in fake_mqtt.topics()  # no half grid status
        assert settings.status_topic(EVSE_ID, 1) not in fake_mqtt.topics()  # nor a half socket status
        assert sum("SOMETHING_NEW" in record.getMessage() for record in caplog.records) == 1

    async def test_a_backend_given_up_ends_it_even_before_the_charge_points_are_known(
        self, offline_client: BlueCurrentClient, fake_mqtt: FakeMqtt, settings: Settings, monkeypatch
    ):
        # The only place where the client giving up comes out of a task, so it must not wait.
        async def given_up(*objects):
            raise ConnectionLost("Reconnect abandoned.")
            yield

        monkeypatch.setattr(offline_client, "live_updates", given_up)
        relay = _relay(offline_client, fake_mqtt, settings, TransactionStore())
        with raises(ConnectionLost):
            await wait_for(relay._follow_updates(), 1)


class TestTransactions:
    async def test_publishes_each_transaction_once(
        self, relay: _Relay, fake_rest: FakeRest, fake_mqtt: FakeMqtt, settings: Settings
    ):
        fake_rest.on("gettransactions", load_fixture("transactions"))
        await relay._sync_charge_point(EVSE_ID)
        published = fake_mqtt.published(settings.transactions_topic(EVSE_ID))
        assert published, "no transaction was published"
        assert not published[0].retain  # a transaction is an event, not a state
        assert published[0].payload["duration"] > 0

        await relay._sync_charge_point(EVSE_ID)  # a second sync has nothing new to say
        assert len(fake_mqtt.published(settings.transactions_topic(EVSE_ID))) == len(published)

    async def test_a_failure_does_not_stop_the_syncer(
        self, relay: _Relay, fake_rest: FakeRest, fake_mqtt: FakeMqtt, settings: Settings
    ):
        fake_rest.on("gettransactions", {"error": "nope"}, 500)
        await relay._sync_charge_point(EVSE_ID)  # must not raise
        assert not fake_mqtt.published(settings.transactions_topic(EVSE_ID))

    async def test_remembers_a_session_by_its_end_day(
        self, offline_client: BlueCurrentClient, fake_rest: FakeRest, fake_mqtt: FakeMqtt, settings: Settings
    ):
        # The backend selects by end day, so the store counts in end days too. Otherwise an overnight
        # session on the horizon is pruned, fetched again and republished.
        response = deepcopy(load_fixture("transactions"))
        overnight = response["data"]["transactions"][0]
        overnight.update(started_at="01-07-2026 22:00:00", end_time="02-07-2026 07:00:00")
        response["data"]["transactions"] = [overnight]
        fake_rest.on("gettransactions", response)
        relay = _relay(offline_client, fake_mqtt, settings, TransactionStore(lookback_days=0, rescan_days=0))
        await relay._sync_charge_point(EVSE_ID)
        assert relay.store.start_date(EVSE_ID) == date(2026, 7, 2)

    async def test_an_ongoing_session_waits_until_it_has_ended(
        self, relay: _Relay, fake_rest: FakeRest, fake_mqtt: FakeMqtt, settings: Settings
    ):
        response = deepcopy(load_fixture("transactions"))
        ongoing = response["data"]["transactions"][0]
        ongoing["end_time"] = ""
        fake_rest.on("gettransactions", response)
        await relay._sync_charge_point(EVSE_ID)
        published = {m.payload["transaction_id"] for m in fake_mqtt.published(settings.transactions_topic(EVSE_ID))}
        assert ongoing["transaction_id"] not in published and published

        fake_rest.on("gettransactions", load_fixture("transactions"))  # it has ended
        await relay._sync_charge_point(EVSE_ID)
        last = fake_mqtt.last(settings.transactions_topic(EVSE_ID))
        assert last.payload["transaction_id"] == ongoing["transaction_id"]

    async def test_waits_for_the_charge_points(
        self, offline_client: BlueCurrentClient, fake_rest: FakeRest, fake_mqtt: FakeMqtt
    ):
        fake_rest.on("gettransactions", load_fixture("transactions"))
        relay = _relay(offline_client, fake_mqtt, Settings(), TransactionStore())
        task = create_task(relay._sync_transactions())
        await sleep(0.05)
        assert not fake_rest.requests  # nothing to ask for before the ids are known
        relay.evse_ids = [EVSE_ID]
        relay.charge_points_known.set()
        await _wait_until(lambda: len(fake_rest.requests) > 0)
        await _stop(task)

    async def test_a_failed_sync_is_retried(
        self, offline_client: BlueCurrentClient, fake_rest: FakeRest, fake_mqtt: FakeMqtt
    ):
        settings = Settings(settings_interval=0.01)
        relay = _relay(offline_client, fake_mqtt, settings, TransactionStore())
        relay.evse_ids = [EVSE_ID]
        relay.charge_points_known.set()
        fake_rest.on("gettransactions", {"error": "nope"}, 500)
        task = create_task(relay._sync_transactions())
        await _wait_until(lambda: len(fake_rest.requests) > 0)
        fake_rest.on("gettransactions", load_fixture("transactions"))  # without a new activity change
        await _wait_until(lambda: settings.transactions_topic(EVSE_ID) in fake_mqtt.topics())
        await _stop(task)

    async def test_a_sync_cut_short_is_finished_on_the_next_connection(self, relay: _Relay, monkeypatch):
        started = Event()

        async def hangs(self, evse_id):
            started.set()
            await Event().wait()

        monkeypatch.setattr(_Relay, "_sync_charge_point", hangs)
        task = create_task(relay._sync_transactions())
        await wait_for(started.wait(), 1)
        await _stop(task)
        assert relay.sync_transactions.is_set()  # or the next sync waits for the settings interval

    def test_duration_needs_the_start(self):
        assert "duration" not in _with_duration({"started_at": None, "end_time": datetime(2026, 9, 1)})
        both = _with_duration({"started_at": datetime(2026, 9, 1, 10), "end_time": datetime(2026, 9, 1, 11)})
        assert both["duration"] == 3600
        # Overnight into the end of summer time: 22:00 to 07:00 Dutch time is ten hours, not nine.
        autumn = _with_duration({"started_at": datetime(2026, 10, 24, 22), "end_time": datetime(2026, 10, 25, 7)})
        assert autumn["duration"] == 10 * 3600


SET_CARD_REPLY = "STATUS_SET_PLUG_AND_CHARGE_CHARGE_CARD"


def _card_requests(fake_socket: FakeSocket) -> list[dict]:
    return [message for message in fake_socket.sent if message.get("command") == "SET_PLUG_AND_CHARGE_CHARGE_CARD"]


class TestCommands:
    @fixture(autouse=True)
    def available(self, fake_rest: FakeRest) -> None:
        fake_rest.on("chargepointstatus", load_fixture("charge_point_status"))

    async def test_sets_the_card(self, offline_client: BlueCurrentClient, fake_socket: FakeSocket):
        fake_socket.on("SET_PLUG_AND_CHARGE_CHARGE_CARD", {"object": SET_CARD_REPLY, "success": True})
        request = {"command": "set_card", "evse_id": EVSE_ID, "uid": "04D4", "id": "a1"}
        answer = await run_command(offline_client, dumps(request).encode())
        assert answer == {"command": "set_card", "id": "a1", "evse_id": EVSE_ID, "status": "ok"}
        assert _card_requests(fake_socket)[-1]["token_uid"] == "04D4"

    async def test_no_uid_means_no_card(self, offline_client: BlueCurrentClient, fake_socket: FakeSocket):
        fake_socket.on("SET_PLUG_AND_CHARGE_CHARGE_CARD", {"object": SET_CARD_REPLY, "success": True})
        answer = await run_command(offline_client, dumps({"command": "set_card", "evse_id": EVSE_ID}))
        assert answer["status"] == "ok"
        assert _card_requests(fake_socket)[-1]["token_uid"] == "BCU-APP"

    async def test_a_refusal_is_an_error(self, offline_client: BlueCurrentClient, fake_socket: FakeSocket):
        fake_socket.on("SET_PLUG_AND_CHARGE_CHARGE_CARD", {"object": SET_CARD_REPLY, "success": False})
        answer = await run_command(offline_client, dumps({"command": "set_card", "evse_id": EVSE_ID, "uid": "X"}))
        assert answer["status"] == "error" and answer["error"]

    async def test_refused_while_a_socket_is_busy(
        self, offline_client: BlueCurrentClient, fake_socket: FakeSocket, fake_rest: FakeRest
    ):
        fake_rest.on("chargepointstatus", load_fixture("charge_point_statuses"))  # socket 2 is charging
        answer = await run_command(offline_client, dumps({"command": "set_card", "evse_id": EVSE_ID, "uid": "X"}))
        assert answer["status"] == "error" and answer["error"] == f"{EVSE_ID} socket 2 is charging"
        assert not _card_requests(fake_socket)

    @mark.parametrize(
        "response, status_code, error",
        [
            ({"error": "nope"}, 500, f"Could not read the status of {EVSE_ID}: HTTPStatusError"),
            ({"items": []}, 200, f"{EVSE_ID} reports no sockets"),
        ],
    )
    async def test_an_unknown_status_does_not_set_the_card(
        self,
        offline_client: BlueCurrentClient,
        fake_socket: FakeSocket,
        fake_rest: FakeRest,
        response: dict,
        status_code: int,
        error: str,
    ):
        fake_rest.on("chargepointstatus", response, status_code)
        answer = await run_command(offline_client, dumps({"command": "set_card", "evse_id": EVSE_ID, "uid": "X"}))
        assert answer["status"] == "error" and answer["error"] == error
        assert not _card_requests(fake_socket)

    @mark.parametrize(
        "request_, error",
        [
            ({"command": "fly", "evse_id": EVSE_ID}, "Unknown command"),
            ({"command": "set_card"}, "evse_id"),
            ({"command": "set_card", "evse_id": EVSE_ID, "uid": 5}, "uid"),
            ({"command": "set_card", "evse_id": EVSE_ID, "uid": ""}, "uid"),  # an empty HA text helper
        ],
    )
    async def test_a_bad_request_is_an_error_and_sends_nothing(
        self, offline_client: BlueCurrentClient, fake_socket: FakeSocket, request_: dict, error: str
    ):
        answer = await run_command(offline_client, dumps({**request_, "id": "x"}))
        assert answer["status"] == "error" and error in answer["error"]
        assert answer["id"] == "x"  # echoed, so the sender can match the answer
        assert not _card_requests(fake_socket)

    @mark.parametrize("payload", ["{not json", '["set_card"]', b"\xff"])
    async def test_an_unreadable_request_is_an_error(self, offline_client: BlueCurrentClient, payload):
        answer = await run_command(offline_client, payload)
        assert answer["status"] == "error" and answer["id"] is None

    async def test_answers_each_command_and_skips_retained_ones(
        self, relay: _Relay, fake_socket: FakeSocket, fake_mqtt: FakeMqtt, settings: Settings
    ):
        fake_socket.on("SET_PLUG_AND_CHARGE_CHARGE_CARD", {"object": SET_CARD_REPLY, "success": True})
        task = create_task(relay._follow_commands())
        fake_mqtt.feed(settings.command_topic, {"command": "set_card", "evse_id": EVSE_ID, "id": "old"}, retain=True)
        fake_mqtt.feed(settings.command_topic, {"command": "set_card", "evse_id": EVSE_ID, "id": "new"})
        await _wait_until(lambda: settings.ack_topic in fake_mqtt.topics())
        await _stop(task)

        assert fake_mqtt.subscriptions == [(settings.command_topic, settings.mqtt_qos)]
        answers = fake_mqtt.published(settings.ack_topic)
        assert [answer.payload["id"] for answer in answers] == ["new"]  # the retained one never ran
        assert answers[0].payload["status"] == "ok" and not answers[0].retain
        assert len(_card_requests(fake_socket)) == 1
        assert relay.refresh_settings.is_set()  # so the settings topic shows the new card

    async def test_a_command_wakes_the_status_poll_at_most_once_a_second(
        self, relay: _Relay, fake_mqtt: FakeMqtt, settings: Settings
    ):
        # An unplug is not pushed: without the wake-up, the status topic shows it only a poll later.
        relay.settings = replace(settings, poll_interval=60)
        tasks = [create_task(relay._poll_statuses()), create_task(relay._follow_commands())]
        await _wait_until(lambda: len(fake_mqtt.published(settings.health_topic)) == 1)
        for _ in range(10):  # an error answer wakes it too, but a flood costs one read a second
            fake_mqtt.feed(settings.command_topic, {"command": "fly", "evse_id": EVSE_ID})
        await sleep(0.5)
        assert len(fake_mqtt.published(settings.health_topic)) == 1
        await _wait_until(lambda: len(fake_mqtt.published(settings.health_topic)) == 2)
        for task in tasks:
            await _stop(task)

    async def test_a_refused_subscription_is_logged(self, relay: _Relay, fake_mqtt: FakeMqtt, monkeypatch, caplog):
        async def refuse(topic, qos=0, timeout=None):
            return (128,)

        monkeypatch.setattr(fake_mqtt, "subscribe", refuse)
        task = create_task(relay._follow_commands())
        await _wait_until(lambda: "refused" in caplog.text)
        await _stop(task)


class TestHeartbeat:
    # The last verdict, "down" included: a heartbeat that said "up" through an outage would lie.
    @mark.parametrize("status, error", [("up", None), ("down", "RequestTimeout")])
    async def test_repeats_the_last_verdict_without_retaining(
        self, offline_client: BlueCurrentClient, fake_mqtt: FakeMqtt, status: str, error: str | None
    ):
        settings = Settings(heartbeat_interval=0.01)
        relay = _relay(offline_client, fake_mqtt, settings, TransactionStore())
        await relay.publish_health(status, error=error)
        task = create_task(relay._heartbeat())
        await _wait_until(lambda: len(fake_mqtt.published(settings.health_topic)) > 1)
        await _stop(task)
        heartbeat = fake_mqtt.last(settings.health_topic)
        assert heartbeat.payload["status"] == status and heartbeat.payload.get("error") == error
        assert not heartbeat.retain  # a broker must not keep telling new subscribers the relay was alive


class TestServe:
    async def test_stopping_publishes_a_last_word(
        self, offline_client: BlueCurrentClient, fake_mqtt: FakeMqtt, monkeypatch, settings: Settings
    ):
        # Without it a broker keeps serving the retained "up" of a relay that is no longer running.
        monkeypatch.setattr("pybluecurrent.relay.relay.MqttClient", make_fake_mqtt(fake_mqtt))
        stopping = Event()
        stopping.set()
        await wait_for(_serve(_Relay(offline_client, settings, TransactionStore()), stopping), 2)
        health = fake_mqtt.last(settings.health_topic)
        assert health.payload["status"] == "down" and health.payload["error"] == "shutdown" and health.retain
        # The broker's word for a relay that was killed: retained, and without the time it connected.
        assert fake_mqtt.will.topic == settings.health_topic and fake_mqtt.will.retain
        assert loads(fake_mqtt.will.payload) == {"status": "down", "error": "lost"}

    async def test_a_failing_task_surfaces_for_the_reconnect(
        self, offline_client: BlueCurrentClient, monkeypatch, settings: Settings
    ):
        # A publish that fails because the broker went away must end this connection, not be swallowed.
        mqtt = FakeMqtt()

        async def failing_publish(*args, **kwargs):
            raise MqttError("broker gone")

        monkeypatch.setattr(mqtt, "publish", failing_publish)
        monkeypatch.setattr("pybluecurrent.relay.relay.MqttClient", make_fake_mqtt(mqtt))
        with raises(MqttError):
            await wait_for(_serve(_Relay(offline_client, settings, TransactionStore()), Event()), 2)

    async def test_a_broker_error_on_a_live_connection_still_says_down(
        self, offline_client: BlueCurrentClient, fake_mqtt: FakeMqtt, monkeypatch, settings: Settings
    ):
        # A timed-out publish raises MqttError while the connection lives; closing it cleanly then
        # cancels the Will, so only the relay itself can replace the retained "up".
        publish = fake_mqtt.publish
        failures = [MqttError("Operation timed out")]

        async def fails_once(*args, **kwargs):
            if failures:
                raise failures.pop()
            await publish(*args, **kwargs)

        monkeypatch.setattr(fake_mqtt, "publish", fails_once)
        monkeypatch.setattr("pybluecurrent.relay.relay.MqttClient", make_fake_mqtt(fake_mqtt))
        with raises(MqttError):
            await wait_for(_serve(_Relay(offline_client, settings, TransactionStore()), Event()), 2)
        health = fake_mqtt.last(settings.health_topic)
        assert health.payload["status"] == "down" and health.payload["error"] == "MqttError" and health.retain


class TestRun:
    """The outer loop: one backend session, however often the broker comes and goes."""

    @fixture
    def backend(self, monkeypatch, fake_socket: FakeSocket, fake_rest: FakeRest) -> FakeSocket:
        """The faked backend, for a run() that builds and logs in its own client."""
        monkeypatch.setattr("pybluecurrent.client.connect", make_fake_connect(fake_socket))
        monkeypatch.setattr("pybluecurrent.client.AsyncClient", make_fake_async_client(fake_rest))
        fake_socket.on("GET_CHARGE_POINTS", load_fixture("charge_points"))
        fake_socket.on("GET_GRID_STATUS", load_fixture("grid_status"))
        fake_rest.on("chargepointstatus", load_fixture("charge_point_statuses"))
        return fake_socket

    async def test_a_broker_outage_does_not_log_in_again(self, monkeypatch, backend: FakeSocket):
        mqtt = FakeMqtt(fail_on_connect=MqttError("connection refused"))
        monkeypatch.setattr("pybluecurrent.relay.relay.MqttClient", make_fake_mqtt(mqtt))
        settings = Settings(username="username", password="password", mqtt_reconnect_interval=0.01)

        task = create_task(run(settings))
        await _wait_until(lambda: settings.health_topic in mqtt.topics(), 5)
        raise_signal(SIGTERM)
        await wait_for(task, 5)

        assert mqtt.connections == 1  # the first attempt was refused, the second connected
        logins = [message for message in backend.sent if message.get("command") == "VALIDATE_PASSWORD"]
        assert len(logins) == 1  # the broker outage cost no backend login
        assert mqtt.last(settings.health_topic).payload["error"] == "shutdown"

    async def test_an_unexpected_error_starts_over_without_a_login(self, monkeypatch, backend: FakeSocket):
        mqtt = FakeMqtt()
        monkeypatch.setattr("pybluecurrent.relay.relay.MqttClient", make_fake_mqtt(mqtt))
        original = _Relay._note_activity
        calls = []

        def fails_once(self, evse_id, status):  # a response with a field missing, say
            calls.append(evse_id)
            if len(calls) == 1:
                raise KeyError("activity")
            original(self, evse_id, status)

        monkeypatch.setattr(_Relay, "_note_activity", fails_once)
        settings = Settings(username="username", password="password", mqtt_reconnect_interval=0.01)
        task = create_task(run(settings))
        # Subscribed once per connection: the command listener comes back with the relay.
        await _wait_until(lambda: len(mqtt.subscriptions) == 2, 5)
        raise_signal(SIGTERM)
        await wait_for(task, 5)

        assert mqtt.connections == 2
        assert any(message.payload.get("error") == "KeyError" for message in mqtt.published(settings.health_topic))
        logins = [message for message in backend.sent if message.get("command") == "VALIDATE_PASSWORD"]
        assert len(logins) == 1

    async def test_a_broker_drop_republishes_without_asking_the_backend(self, monkeypatch, backend: FakeSocket):
        settings = Settings(
            username="username", password="password", mqtt_reconnect_interval=0.01, heartbeat_interval=0.2
        )
        mqtt = FakeMqtt()
        publish = mqtt.publish
        failures = [MqttError("broker gone")]

        async def fails_on_the_first_heartbeat(topic, payload, qos=0, retain=False, timeout=None):
            if topic == settings.health_topic and not retain and failures:
                raise failures.pop()
            await publish(topic, payload, qos, retain, timeout)

        monkeypatch.setattr(mqtt, "publish", fails_on_the_first_heartbeat)
        monkeypatch.setattr("pybluecurrent.relay.relay.MqttClient", make_fake_mqtt(mqtt))
        task = create_task(run(settings))
        await _wait_until(lambda: len(mqtt.subscriptions) == 2, 5)  # the second connection is up
        raise_signal(SIGTERM)
        await wait_for(task, 5)

        commands = [message.get("command") for message in backend.sent]
        assert commands.count("GET_CHARGE_POINTS") == 1 and commands.count("GET_GRID_STATUS") == 1
        assert len(mqtt.published(settings.settings_topic(EVSE_ID))) == 2  # the broker may have lost them
        assert len(mqtt.published(settings.grid_topic(EVSE_ID))) == 2

    async def test_a_failed_login_waits_before_it_gives_up(self, monkeypatch):
        # Or a restart policy would log in again at once, and a burst of logins locks the account.
        monkeypatch.setattr("pybluecurrent.client.connect", lambda *args, **kwargs: _Refused())
        monkeypatch.setattr(BlueCurrentClient, "reconnect_relogin_window", 0.2)
        started = monotonic()
        with raises(OSError):
            await wait_for(run(Settings(username="username", password="password")), 2)
        assert monotonic() - started >= 0.2

    async def test_stopping_while_it_waits_is_a_clean_exit(self, monkeypatch, caplog):
        def own_handler(number, frame):  # a program that runs the relay itself, with a handler of its own
            pass

        monkeypatch.setattr("pybluecurrent.client.connect", lambda *args, **kwargs: _Refused())
        before = signal(SIGTERM, own_handler)
        try:
            task = create_task(run(Settings(username="username", password="password")))
            await _wait_until(lambda: "Giving up" in caplog.text)
            raise_signal(SIGTERM)
            assert await wait_for(task, 2) is None  # not the login error: it was asked to stop
            assert not get_running_loop().remove_signal_handler(SIGTERM)  # the relay gave it back
            assert getsignal(SIGTERM) is own_handler  # as it was, not the default
        finally:
            signal(SIGTERM, before)

    async def test_a_handler_the_loop_set_is_not_given_back(self, monkeypatch, caplog):
        # getsignal shows only the loop's own no-op: putting that back would make SIGTERM do nothing.
        monkeypatch.setattr("pybluecurrent.client.connect", lambda *args, **kwargs: _Refused())
        get_running_loop().add_signal_handler(SIGTERM, lambda: None)
        task = create_task(run(Settings(username="username", password="password")))
        await _wait_until(lambda: "Giving up" in caplog.text)
        raise_signal(SIGTERM)
        await wait_for(task, 2)
        assert getsignal(SIGTERM) is SIG_DFL


class TestCli:
    def test_relay_is_a_subcommand(self):
        # The names, not the rendered help: how that looks depends on the terminal it is drawn for.
        assert "relay" in {command.callback.__name__ for command in app.registered_commands if command.callback}
        assert CliRunner().invoke(app, ["relay", "--help"]).exit_code == 0

    def test_missing_credentials_are_a_usage_error(self):
        env = {"BLUECURRENT_USERNAME": None, "BLUECURRENT_PASSWORD": None, "BLUECURRENT_API_TOKEN": None}
        result = CliRunner().invoke(app, ["relay"], env=env)
        assert result.exit_code == 2 and "BLUECURRENT_API_TOKEN" in result.output

    @mark.parametrize("option, shortest", [("--poll-interval", 15), ("--settings-interval", 120)])
    def test_refuses_a_short_interval(self, option: str, shortest: int, monkeypatch):
        # Each poll is a request: too many, and the backend blocks the account. Exit codes, not the
        # rendered error: how that looks depends on the terminal it is drawn for.
        async def no_relay(settings):  # should the check ever break, nothing goes to the network
            pass

        monkeypatch.setattr("pybluecurrent.relay.run", no_relay)

        def exit_code(seconds: int) -> int:
            arguments = ["relay", option, str(seconds)]
            return CliRunner().invoke(app, arguments, env={"BLUECURRENT_API_TOKEN": "token"}).exit_code

        assert exit_code(shortest) == 0 and exit_code(shortest - 1) == 2

    def test_has_the_defaults_of_the_settings(self):
        # Both define them: the command line cannot import Settings before it knows the extra is there.
        options = signature(relay_command).parameters
        defaults = {field.name: field.default for field in fields(Settings) if field.name in options}
        assert set(defaults) | {"heartbeat_interval", "mqtt_reconnect_interval"} == {f.name for f in fields(Settings)}
        assert defaults == {name: options[name].default for name in defaults}

    def test_without_the_extra_it_says_what_to_install(self, monkeypatch):
        for name in [name for name in modules if name.startswith("pybluecurrent.relay")]:
            monkeypatch.delitem(modules, name)
        monkeypatch.setitem(modules, "aiomqtt", None)  # as if it was never installed
        result = CliRunner().invoke(app, ["relay"], env={"BLUECURRENT_API_TOKEN": "token"})
        assert result.exit_code == 2 and "pybluecurrent[mqtt]" in result.output


class _Refused:
    """A websocket connection the network refuses."""

    async def __aenter__(self):
        raise OSError("refused")

    async def __aexit__(self, *exc):
        return None


def test_topics():
    settings = Settings(topic_prefix="p")
    assert settings.status_topic("BCU1", 2) == "p/BCU1/2/status"
    assert settings.settings_topic("BCU1") == "p/BCU1/settings"
    assert settings.grid_topic("BCU1") == "p/BCU1/grid"
    assert settings.transactions_topic("BCU1") == "p/BCU1/transactions"
    assert settings.health_topic == "p/backend_health"
    assert settings.command_topic == "p/command"
    assert settings.ack_topic == "p/command/ack"


def test_settings_keep_the_secrets_out_of_their_repr():
    settings = Settings(password="hunter2", api_token="token-123", mqtt_password="broker-secret")
    assert not [secret for secret in ("hunter2", "token-123", "broker-secret") if secret in repr(settings)]
