"""The MQTT relay, driven by the offline websocket, REST and MQTT fakes."""

from asyncio import CancelledError, Event, Task, create_task, sleep, wait_for
from contextlib import suppress
from datetime import date, datetime, time, timedelta, timezone
from logging import INFO
from pathlib import Path
from signal import SIGTERM, raise_signal

from aiomqtt import MqttError
from fake_mqtt import FakeMqtt, make_fake_mqtt
from fake_rest import FakeRest, make_fake_async_client
from fake_socket import FakeSocket, load_fixture, make_fake_connect
from pytest import fixture, raises

from pybluecurrent import BlueCurrentClient
from pybluecurrent.relay.relay import _Relay, _serve, _with_duration, run
from pybluecurrent.relay.serialize import encode
from pybluecurrent.relay.settings import Settings
from pybluecurrent.relay.state import TransactionStore

EVSE_ID = "BCU123456"


async def _wait_until(predicate, limit: int = 2000) -> None:
    """Yield control to the event loop until ``predicate()`` holds (or fail after ``limit`` turns)."""
    for _ in range(limit):
        if predicate():
            return
        await sleep(0)
    raise AssertionError("condition was not reached")


async def _forever_until(predicate) -> None:
    """Yield control until ``predicate()`` holds, without a turn limit (wrap it in a timeout)."""
    while not predicate():
        await sleep(0)


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


@fixture
def relay(offline_client: BlueCurrentClient, fake_mqtt: FakeMqtt, settings: Settings) -> _Relay:
    return _Relay(offline_client, fake_mqtt, settings, TransactionStore())


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
        # A naive backend datetime is read in the relay's own timezone and published as UTC.
        expected = datetime(2026, 9, 12, 10, 30).astimezone(timezone.utc).isoformat()
        assert f'"started_at": "{expected}"' in message
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
        store.add(EVSE_ID, 1, datetime(2026, 9, 10, 12, 0))
        assert store.start_date(EVSE_ID) == date(2026, 9, 8)

    def test_survives_a_restart(self, tmp_path: Path):
        path = tmp_path / "state.json"
        store = TransactionStore(path)
        store.add(EVSE_ID, 7, datetime(2026, 9, 10, 12, 0))
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
        fake_socket.on("GET_CHARGE_POINTS", load_fixture("charge_points"))
        fake_rest.on("chargepointstatus", load_fixture("charge_point_statuses"))
        task = create_task(relay._poll_statuses())
        await _wait_until(lambda: len(fake_mqtt.published(settings.health_topic)) > 0)
        await _stop(task)

        first = fake_mqtt.last(settings.status_topic(EVSE_ID, 1))
        assert first.payload["activity"] == "available"
        assert first.payload["evse_id"] == EVSE_ID and first.payload["socket_id"] == 1
        assert first.retain and first.qos == settings.mqtt_qos
        assert "timestamp" in first.payload
        assert settings.status_topic(EVSE_ID, 2) in fake_mqtt.topics()  # the second socket too
        assert fake_mqtt.last(settings.health_topic).payload["status"] == "up"

    async def test_reports_a_failure_as_unhealthy(
        self, relay: _Relay, fake_socket: FakeSocket, fake_rest: FakeRest, fake_mqtt: FakeMqtt, settings: Settings
    ):
        fake_socket.on("GET_CHARGE_POINTS", load_fixture("charge_points"))
        fake_rest.on("chargepointstatus", {"error": "nope"}, 500)
        task = create_task(relay._poll_statuses())
        await _wait_until(lambda: len(fake_mqtt.published(settings.health_topic)) > 0)
        await _stop(task)

        health = fake_mqtt.last(settings.health_topic)
        assert health.payload["status"] == "down" and health.payload["error"] == "HTTPStatusError"
        assert health.retain

    async def test_a_changed_activity_triggers_a_refresh(
        self, relay: _Relay, fake_socket: FakeSocket, fake_rest: FakeRest, fake_mqtt: FakeMqtt
    ):
        relay._note_activity(EVSE_ID, {"socket_id": 1, "activity": "available"})
        assert not relay.refresh_settings.is_set()  # the first reading is not a change
        relay._note_activity(EVSE_ID, {"socket_id": 1, "activity": "charging"})
        assert relay.refresh_settings.is_set() and relay.sync_transactions.is_set()


class TestSettings:
    async def test_publishes_the_settings_of_every_charge_point(
        self, relay: _Relay, fake_socket: FakeSocket, fake_mqtt: FakeMqtt, settings: Settings
    ):
        fake_socket.on("GET_CHARGE_POINTS", load_fixture("charge_points"))
        task = create_task(relay._poll_settings())
        await _wait_until(lambda: settings.settings_topic(EVSE_ID) in fake_mqtt.topics())
        await _stop(task)

        payload = fake_mqtt.last(settings.settings_topic(EVSE_ID)).payload
        assert payload["evse_id"] == EVSE_ID
        assert "delayed_charging" in payload and "price_based_charging" in payload


class TestGrid:
    async def test_primes_then_follows_the_pushes(
        self, relay: _Relay, fake_socket: FakeSocket, fake_mqtt: FakeMqtt, settings: Settings
    ):
        fake_socket.on("GET_CHARGE_POINTS", load_fixture("charge_points"))
        fake_socket.on("GET_GRID_STATUS", load_fixture("grid_status"))
        task = create_task(relay._follow_grid())
        await _wait_until(lambda: settings.grid_topic(EVSE_ID) in fake_mqtt.topics())
        primed = fake_mqtt.last(settings.grid_topic(EVSE_ID)).payload
        assert primed["evse_id"] == EVSE_ID and "grid_max_install" in primed

        fake_socket.feed({"object": "GRID_CURRENT", "evse_id": EVSE_ID, "grid_actual_p1": 9})
        await _wait_until(lambda: fake_mqtt.last(settings.grid_topic(EVSE_ID)).payload["grid_actual_p1"] == 9)
        await _stop(task)

        updated = fake_mqtt.last(settings.grid_topic(EVSE_ID)).payload
        assert updated["grid_max_install"] == primed["grid_max_install"]  # the maximums are kept

    async def test_logs_an_unknown_message_type_once(
        self, relay: _Relay, fake_socket: FakeSocket, fake_mqtt: FakeMqtt, settings: Settings, caplog
    ):
        caplog.set_level(INFO, logger="pybluecurrent.relay.relay")
        fake_socket.on("GET_CHARGE_POINTS", load_fixture("charge_points"))
        fake_socket.on("GET_GRID_STATUS", load_fixture("grid_status"))
        task = create_task(relay._follow_grid())
        await _wait_until(lambda: len(fake_mqtt.messages) > 0)
        fake_socket.feed({"object": "SOMETHING_NEW"})
        fake_socket.feed({"object": "SOMETHING_NEW"})
        # Queued behind both, so seeing this one means both were handled.
        fake_socket.feed({"object": "GRID_CURRENT", "evse_id": EVSE_ID, "grid_actual_p1": 7})
        await _wait_until(lambda: fake_mqtt.last(settings.grid_topic(EVSE_ID)).payload.get("grid_actual_p1") == 7)
        await _stop(task)
        assert sum("SOMETHING_NEW" in record.getMessage() for record in caplog.records) == 1


class TestTransactions:
    async def test_publishes_each_transaction_once(
        self, relay: _Relay, fake_socket: FakeSocket, fake_rest: FakeRest, fake_mqtt: FakeMqtt, settings: Settings
    ):
        fake_socket.on("GET_CHARGE_POINTS", load_fixture("charge_points"))
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

    def test_duration_needs_both_ends(self):
        assert "duration" not in _with_duration({"started_at": datetime(2026, 9, 1), "end_time": None})
        both = _with_duration({"started_at": datetime(2026, 9, 1, 10), "end_time": datetime(2026, 9, 1, 11)})
        assert both["duration"] == 3600


class TestHeartbeat:
    async def test_repeats_the_last_verdict_without_retaining(
        self, offline_client: BlueCurrentClient, fake_mqtt: FakeMqtt
    ):
        settings = Settings(heartbeat_interval=0.01)
        relay = _Relay(offline_client, fake_mqtt, settings, TransactionStore())
        await relay.publish_health("up")
        task = create_task(relay._heartbeat())
        await wait_for(_forever_until(lambda: len(fake_mqtt.published(settings.health_topic)) > 1), 2)
        await _stop(task)
        heartbeat = fake_mqtt.last(settings.health_topic)
        # Not retained: a broker must not keep telling new subscribers the relay was alive.
        assert heartbeat.payload["status"] == "up" and not heartbeat.retain

    async def test_does_not_claim_health_it_does_not_have(self, offline_client: BlueCurrentClient, fake_mqtt: FakeMqtt):
        # While the backend is unreachable the heartbeat must keep saying so, or a consumer reads
        # "up" every minute through an outage.
        settings = Settings(heartbeat_interval=0.01)
        relay = _Relay(offline_client, fake_mqtt, settings, TransactionStore())
        await relay.publish_health("down", error="RequestTimeout")
        task = create_task(relay._heartbeat())
        await wait_for(_forever_until(lambda: len(fake_mqtt.published(settings.health_topic)) > 1), 2)
        await _stop(task)
        assert fake_mqtt.last(settings.health_topic).payload == {
            "status": "down",
            "error": "RequestTimeout",
            "timestamp": fake_mqtt.last(settings.health_topic).payload["timestamp"],
        }


class TestServe:
    async def test_stopping_publishes_a_last_word(
        self, offline_client: BlueCurrentClient, fake_mqtt: FakeMqtt, monkeypatch, settings: Settings
    ):
        # Without it a broker keeps serving the retained "up" of a relay that is no longer running.
        monkeypatch.setattr("pybluecurrent.relay.relay.MqttClient", make_fake_mqtt(fake_mqtt))
        stopping = Event()
        stopping.set()
        await wait_for(_serve(offline_client, settings, TransactionStore(), stopping), 2)
        health = fake_mqtt.last(settings.health_topic)
        assert health.payload["status"] == "down" and health.payload["error"] == "shutdown" and health.retain

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
            await wait_for(_serve(offline_client, settings, TransactionStore(), Event()), 2)


class TestRun:
    """The outer loop: one backend session, however often the broker comes and goes."""

    async def test_a_broker_outage_does_not_log_in_again(
        self, monkeypatch, fake_socket: FakeSocket, fake_rest: FakeRest
    ):
        monkeypatch.setattr("pybluecurrent.client.connect", make_fake_connect(fake_socket))
        monkeypatch.setattr("pybluecurrent.client.AsyncClient", make_fake_async_client(fake_rest))
        fake_socket.on("GET_CHARGE_POINTS", load_fixture("charge_points"))
        fake_socket.on("GET_GRID_STATUS", load_fixture("grid_status"))
        fake_rest.on("chargepointstatus", load_fixture("charge_point_statuses"))
        mqtt = FakeMqtt(fail_on_connect=MqttError("connection refused"))
        monkeypatch.setattr("pybluecurrent.relay.relay.MqttClient", make_fake_mqtt(mqtt))
        settings = Settings(username="username", password="password", mqtt_reconnect_interval=0.01)

        task = create_task(run(settings))
        await wait_for(_forever_until(lambda: settings.health_topic in mqtt.topics()), 5)
        raise_signal(SIGTERM)  # what Kubernetes sends a pod it is stopping
        await wait_for(task, 5)

        assert mqtt.connections == 1  # the first attempt was refused, the second connected
        logins = [message for message in fake_socket.sent if message.get("command") == "VALIDATE_PASSWORD"]
        assert len(logins) == 1  # the broker outage cost no backend login
        assert mqtt.last(settings.health_topic).payload["error"] == "shutdown"

    async def test_transactions_are_not_republished_after_a_broker_outage(
        self, offline_client: BlueCurrentClient, fake_socket: FakeSocket, fake_rest: FakeRest, monkeypatch
    ):
        # The store lives with the relay, not with the MQTT connection, or every broker blip would
        # replay the whole lookback window onto an event topic.
        fake_socket.on("GET_CHARGE_POINTS", load_fixture("charge_points"))
        fake_rest.on("gettransactions", load_fixture("transactions"))
        mqtt = FakeMqtt()
        monkeypatch.setattr("pybluecurrent.relay.relay.MqttClient", make_fake_mqtt(mqtt))
        settings = Settings(sync_transactions=True)
        store = TransactionStore()

        for _ in range(2):  # two MQTT connections, one store
            stopping = Event()
            stopping.set()
            relay = _Relay(offline_client, mqtt, settings, store)
            await relay._sync_charge_point(EVSE_ID)

        topic = settings.transactions_topic(EVSE_ID)
        assert len(mqtt.published(topic)) == len(load_fixture("transactions")["data"]["transactions"])


class TestCli:
    def test_relay_is_a_subcommand(self):
        from typer.testing import CliRunner

        from pybluecurrent.cli import app

        # The names, not the rendered help: how that looks depends on the terminal it is drawn for.
        assert "relay" in {command.callback.__name__ for command in app.registered_commands if command.callback}
        assert CliRunner().invoke(app, ["relay", "--help"]).exit_code == 0


def test_topics():
    settings = Settings(topic_prefix="p")
    assert settings.status_topic("BCU1", 2) == "p/BCU1/2/status"
    assert settings.settings_topic("BCU1") == "p/BCU1/settings"
    assert settings.grid_topic("BCU1") == "p/BCU1/grid"
    assert settings.transactions_topic("BCU1") == "p/BCU1/transactions"
    assert settings.health_topic == "p/backend_health"
