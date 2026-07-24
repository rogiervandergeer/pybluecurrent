import logging
from asyncio import CancelledError, create_task, gather, sleep
from contextlib import suppress
from datetime import date, datetime, time

from fake_rest import FakeRest, make_fake_async_client
from fake_socket import (
    FAKE_CUSTOMER_ID,
    FAKE_TOKEN,
    FailingConnection,
    FakeConnection,
    FakeSocket,
    load_fixture,
    make_fake_connect,
    make_reconnecting_connect,
)
from models_check import assert_model
from pytest import MonkeyPatch, raises

from pybluecurrent import BlueCurrentClient, Weekday
from pybluecurrent.client import _redact
from pybluecurrent.client import logger as client_logger
from pybluecurrent.exceptions import AuthenticationFailed, BlueCurrentException, ConnectionLost, RequestTimeout
from pybluecurrent.models import (
    Account,
    ChargeCard,
    ChargePoint,
    ChargePointSettings,
    GridStatus,
    SustainabilityStatus,
)


async def _drain(client: BlueCurrentClient) -> None:
    """Yield control until the handler has finished shutting down (``_drop_reason`` is set)."""
    for _ in range(10):
        await sleep(0)
        if client._drop_reason is not None or client._closed is not None:
            break


async def _wait_until(pred, limit: int = 2000) -> None:
    """Yield control to the event loop until ``pred()`` holds (or fail after ``limit`` turns)."""
    for _ in range(limit):
        if pred():
            return
        await sleep(0)
    raise AssertionError("condition was not reached")


async def _expect_auth_failure(monkeypatch: MonkeyPatch, socket: FakeSocket, **client_kwargs) -> None:
    """Connect a client backed by ``socket`` and assert ``__aenter__`` fails authentication.

    Also asserts the failed connect leaks nothing: ``__aenter__`` tears down the socket, the
    handler task and the httpx client itself when it raises.
    """
    monkeypatch.setattr("pybluecurrent.client.connect", make_fake_connect(socket))
    client = BlueCurrentClient(**client_kwargs)
    with raises(AuthenticationFailed):
        await client.__aenter__()
    assert client.consumer is None
    assert client.socket is None
    assert client.httpx_client is None
    assert client.connection is None


class TestOfflineAuth:
    """Auth handshake against the offline fake — no credentials, no network."""

    async def test_password_auth_success(self, offline_client: BlueCurrentClient):
        assert offline_client.token == FAKE_TOKEN

    async def test_password_auth_failed(self, monkeypatch: MonkeyPatch, fake_socket: FakeSocket):
        fake_socket.on("VALIDATE_PASSWORD", {"object": "STATUS_PASSWORD", "accepted": False})
        await _expect_auth_failure(monkeypatch, fake_socket, username="username", password="password")

    async def test_token_auth_success(self, monkeypatch: MonkeyPatch, fake_socket: FakeSocket):
        monkeypatch.setattr("pybluecurrent.client.connect", make_fake_connect(fake_socket))
        async with BlueCurrentClient(api_token="some-token") as client:
            assert client.token == FAKE_TOKEN
            assert client.customer_id == FAKE_CUSTOMER_ID

    async def test_token_auth_failed(self, monkeypatch: MonkeyPatch, fake_socket: FakeSocket):
        fake_socket.on("VALIDATE_API_TOKEN", {"object": "STATUS_API_TOKEN", "success": False})
        await _expect_auth_failure(monkeypatch, fake_socket, api_token="bad-token")


class TestOfflineTransport:
    """Exercise ``_send`` / ``_receive`` / ``_handler`` directly against the fake."""

    async def test_send_injects_authorization(self, offline_client: BlueCurrentClient, fake_socket: FakeSocket):
        fake_socket.sent.clear()
        await offline_client._send({"command": "PING"}, token=True)
        assert fake_socket.sent[-1]["Authorization"] == f"Token {FAKE_TOKEN}"

    async def test_send_without_token(self, offline_client: BlueCurrentClient, fake_socket: FakeSocket):
        fake_socket.sent.clear()
        await offline_client._send({"command": "PING"})
        assert "Authorization" not in fake_socket.sent[-1]

    async def test_receive_matches_object(self, offline_client: BlueCurrentClient, fake_socket: FakeSocket):
        task = create_task(offline_client._receive("PONG"))
        await sleep(0)
        fake_socket.feed({"object": "PONG", "value": 1})
        assert (await task) == {"object": "PONG", "value": 1}

    async def test_receive_discards_nonmatching(self, offline_client: BlueCurrentClient, fake_socket: FakeSocket):
        task = create_task(offline_client._receive("WANTED"))
        await sleep(0)
        fake_socket.feed({"object": "NOISE", "value": 0})
        fake_socket.feed({"object": "WANTED", "value": 1})
        assert (await task)["value"] == 1

    async def test_receive_raises_on_error(self, offline_client: BlueCurrentClient, fake_socket: FakeSocket):
        task = create_task(offline_client._receive("WANTED"))
        await sleep(0)
        fake_socket.feed(load_fixture("error_forbidden"))
        with raises(BlueCurrentException) as exc:
            await task
        assert exc.value.args[0]["message"] == "forbidden"

    async def test_receive_timeout(self, offline_client: BlueCurrentClient):
        # RequestTimeout subclasses the builtin TimeoutError, so both catch sites keep working.
        with raises(TimeoutError):
            await offline_client._receive("NEVER_ARRIVES", timeout=0)
        with raises(RequestTimeout):
            await offline_client._receive("NEVER_ARRIVES", timeout=0)

    async def test_handler_fans_out_to_all_waiters(self, offline_client: BlueCurrentClient, fake_socket: FakeSocket):
        # A single frame reaches every concurrent waiter.
        first = create_task(offline_client._receive("PING"))
        second = create_task(offline_client._receive("PING"))
        await sleep(0)
        fake_socket.feed({"object": "PING", "value": 1})
        assert (await first)["value"] == 1
        assert (await second)["value"] == 1

    async def test_non_json_frame_is_skipped(self, offline_client: BlueCurrentClient, fake_socket: FakeSocket):
        # A malformed frame is logged and skipped; the handler survives and still delivers later frames.
        consumer = offline_client.consumer
        assert consumer is not None
        task = create_task(offline_client._receive("PONG"))
        await sleep(0)
        fake_socket.feed("this is not json")
        fake_socket.feed({"object": "PONG", "value": 1})
        assert (await task)["value"] == 1
        assert not consumer.done()


class TestOfflineCommands:
    """Command round-trips driven by recorded fixtures."""

    async def test_get_charge_points(self, offline_client: BlueCurrentClient, fake_socket: FakeSocket):
        fake_socket.on("GET_CHARGE_POINTS", load_fixture("charge_points"))
        charge_points = await offline_client.get_charge_points()
        assert_model(charge_points, list[ChargePoint])
        assert charge_points[0]["evse_id"] == "BCU123456"
        assert charge_points[0]["plug_and_charge_charge_card"]["uid"] == "A1B2C3D4E5F6"

    async def test_get_grid_status(self, offline_client: BlueCurrentClient, fake_socket: FakeSocket):
        fake_socket.on("GET_GRID_STATUS", load_fixture("grid_status"))
        status = await offline_client.get_grid_status("BCU123456")
        assert_model(status, GridStatus)
        assert status["id"] == "GRID-BCU123456"
        assert status["grid_actual_p1"] == 1

    async def test_get_charge_point_settings(self, offline_client: BlueCurrentClient, fake_socket: FakeSocket):
        fake_socket.on("GET_CH_SETTINGS", load_fixture("charge_point_settings"))
        settings = await offline_client.get_charge_point_settings("BCU123456")
        assert_model(settings, ChargePointSettings)
        assert settings["evse_id"] == "BCU123456"
        # The no-card sentinel (uid "BCU_HOME_USE") is normalized to None.
        assert settings["plug_and_charge_charge_card"] is None
        # The delayed-charging schedule is normalized: selected_days -> days, "HH:MM" -> time.
        assert settings["delayed_charging"]["days"] == [1, 2, 3, 4, 5]
        assert settings["delayed_charging"]["start_time"] == time(23, 0)
        assert settings["delayed_charging"]["end_time"] == time(7, 0)

    async def test_get_charge_cards(self, offline_client: BlueCurrentClient, fake_socket: FakeSocket):
        fake_socket.on("GET_CHARGE_CARDS", load_fixture("charge_cards"))
        cards = await offline_client.get_charge_cards()
        assert_model(cards, list[ChargeCard])
        assert cards[0]["uid"] == "A1B2C3D4E5F6"
        assert cards[0]["date_created"] == date(2023, 6, 27)
        assert cards[0]["date_became_invalid"] is None

    async def test_get_account(self, offline_client: BlueCurrentClient, fake_socket: FakeSocket):
        fake_socket.on("GET_ACCOUNT", load_fixture("account"))
        account = await offline_client.get_account()
        assert_model(account, Account)
        assert account["full_name"] == "Your Full Name"
        assert account["first_login_app"] == datetime(2020, 1, 15, 13, 33, 52)

    async def test_get_sustainability_status(self, offline_client: BlueCurrentClient, fake_socket: FakeSocket):
        fake_socket.on("GET_SUSTAINABILITY_STATUS", load_fixture("sustainability_status"))
        status = await offline_client.get_sustainability_status()
        assert_model(status, SustainabilityStatus)
        assert status == {"trees": 1, "co2": 12.345}

    async def test_error_is_raised(self, offline_client: BlueCurrentClient, fake_socket: FakeSocket):
        fake_socket.on("GET_GRID_STATUS", load_fixture("error_forbidden"))
        with raises(BlueCurrentException) as exc:
            await offline_client.get_grid_status("BOGUS")
        assert exc.value.args[0]["message"] == "forbidden"


class TestOfflineTwoPhaseCommands:
    """The RECEIVED_ -> STATUS_ commands: a falsy STATUS_ ``success`` must raise, not pass silently.

    The backend sends STATUS_ seconds after RECEIVED_, so these drive the two phases by hand: script
    only the RECEIVED_ ack, let the command reach its STATUS_ wait, then feed the STATUS_ frame. (The
    broadcast queue only reaches live subscribers, so feeding both at once would drop STATUS_.)
    """

    @staticmethod
    async def _reach_status_wait() -> None:
        """Yield enough for a two-phase command to consume RECEIVED_ and subscribe for STATUS_."""
        for _ in range(10):
            await sleep(0)

    async def test_set_status_success(self, offline_client: BlueCurrentClient, fake_socket: FakeSocket):
        fake_socket.on("SET_INOPERATIVE", {"object": "RECEIVED_SET_INOPERATIVE"})
        task = create_task(offline_client.set_status("BCU123456", enabled=False))
        await self._reach_status_wait()
        fake_socket.feed({"object": "STATUS_SET_INOPERATIVE", "success": True})
        assert await task is None

    async def test_set_status_failure_raises(self, offline_client: BlueCurrentClient, fake_socket: FakeSocket):
        # The backend reports the charger's non-response as a STATUS_ frame with success=False; the
        # command did nothing, so surface it instead of returning cleanly.
        fake_socket.on("SET_INOPERATIVE", {"object": "RECEIVED_SET_INOPERATIVE"})
        task = create_task(offline_client.set_status("BCU123456", enabled=False))
        await self._reach_status_wait()
        fake_socket.feed({"object": "STATUS_SET_INOPERATIVE", "success": False, "error": "TIMEOUT"})
        with raises(BlueCurrentException) as exc:
            await task
        assert exc.value.args[0]["error"] == "TIMEOUT"

    async def test_unlock_connector_success_returns_none(
        self, offline_client: BlueCurrentClient, fake_socket: FakeSocket
    ):
        fake_socket.on("UNLOCK_CONNECTOR", {"object": "RECEIVED_UNLOCK_CONNECTOR"})
        task = create_task(offline_client.unlock_connector("BCU123456"))
        await self._reach_status_wait()
        fake_socket.feed({"object": "STATUS_UNLOCK_CONNECTOR", "success": True, "evse_id": "BCU123456"})
        assert await task is None

    async def test_unlock_connector_failure_raises(self, offline_client: BlueCurrentClient, fake_socket: FakeSocket):
        fake_socket.on("UNLOCK_CONNECTOR", {"object": "RECEIVED_UNLOCK_CONNECTOR"})
        task = create_task(offline_client.unlock_connector("BCU123456"))
        await self._reach_status_wait()
        fake_socket.feed({"object": "STATUS_UNLOCK_CONNECTOR", "success": False, "error": "TIMEOUT"})
        with raises(BlueCurrentException):
            await task

    async def test_soft_reset_success_returns_none(self, offline_client: BlueCurrentClient, fake_socket: FakeSocket):
        fake_socket.on("SOFT_RESET", {"object": "RECEIVED_SOFT_RESET"})
        task = create_task(offline_client.soft_reset("BCU123456"))
        await self._reach_status_wait()
        fake_socket.feed({"object": "STATUS_SOFT_RESET", "success": True})
        assert await task is None

    async def test_soft_reset_failure_raises(self, offline_client: BlueCurrentClient, fake_socket: FakeSocket):
        fake_socket.on("SOFT_RESET", {"object": "RECEIVED_SOFT_RESET"})
        task = create_task(offline_client.soft_reset("BCU123456"))
        await self._reach_status_wait()
        fake_socket.feed({"object": "STATUS_SOFT_RESET", "success": False, "error": "TIMEOUT"})
        with raises(BlueCurrentException):
            await task


class TestOfflineConcurrency:
    """Concurrency safety: same-type serialization (C1) and error attribution (C2)."""

    async def test_same_type_calls_do_not_cross_consume(
        self, offline_client: BlueCurrentClient, fake_socket: FakeSocket
    ):
        # Give each GET_GRID_STATUS a distinct response. Without the per-object lock both waiters
        # (subscribed at once) would grab the first reply; with it, each call gets its own.
        sent = []

        def responder(msg):
            sent.append(msg)
            return [{"object": "GRID_STATUS", "data": {"id": f"GRID-{len(sent)}"}}]

        fake_socket.responder["GET_GRID_STATUS"] = responder
        first, second = await gather(
            offline_client.get_grid_status("A"),
            offline_client.get_grid_status("B"),
        )
        assert {first["id"], second["id"]} == {"GRID-1", "GRID-2"}

    async def test_read_ignores_an_async_commands_error(
        self, offline_client: BlueCurrentClient, fake_socket: FakeSocket
    ):
        # A read (flow_id=None) must not be poisoned by an error carrying a flow_id (someone else's).
        task = create_task(offline_client._receive("WANTED"))
        await sleep(0)
        fake_socket.feed({"object": "ERROR", "flow_id": "other", "message": "not mine"})
        fake_socket.feed({"object": "WANTED", "value": 1})
        assert (await task)["value"] == 1

    async def test_async_command_ignores_another_commands_error(
        self, offline_client: BlueCurrentClient, fake_socket: FakeSocket
    ):
        # An async waiter (flow_id="F") ignores an error correlated to a different flow and
        # raises only on the one bearing its own flow_id.
        task = create_task(offline_client._receive("STATUS_X", flow_id="F"))
        await sleep(0)
        fake_socket.feed({"object": "ERROR", "flow_id": "other", "message": "another command"})
        fake_socket.feed({"object": "ERROR", "flow_id": "F", "message": "mine"})
        with raises(BlueCurrentException) as exc:
            await task
        assert exc.value.args[0]["message"] == "mine"

    async def test_async_command_claims_uncorrelated_error(
        self, offline_client: BlueCurrentClient, fake_socket: FakeSocket
    ):
        # Not every error carries a flow_id ("forbidden" has none); the waiting async command
        # still surfaces such an uncorrelated error rather than hanging on it.
        task = create_task(offline_client._receive("STATUS_X", flow_id="F"))
        await sleep(0)
        fake_socket.feed({"object": "ERROR", "message": "forbidden"})
        with raises(BlueCurrentException) as exc:
            await task
        assert exc.value.args[0]["message"] == "forbidden"

    async def test_different_type_calls_run_concurrently(
        self, offline_client: BlueCurrentClient, fake_socket: FakeSocket
    ):
        # A slow two-phase command holds only its own lock: stall soft_reset after its RECEIVED_ ack
        # (no STATUS_ fed) and show a grid read on a different key still completes.
        fake_socket.on("SOFT_RESET", {"object": "RECEIVED_SOFT_RESET"})
        reset = create_task(offline_client.soft_reset("A"))
        await sleep(0)
        fake_socket.on("GET_GRID_STATUS", {"object": "GRID_STATUS", "data": {"id": "GRID-1"}})
        status = await offline_client.get_grid_status("A")
        assert status["id"] == "GRID-1"
        reset.cancel()
        with suppress(CancelledError):
            await reset


class TestOfflineDelayedCharging:
    """Delayed charging, which is sent over REST rather than over the websocket."""

    async def test_set_delayed_charging(self, offline_client: BlueCurrentClient, fake_rest: FakeRest):
        await offline_client.set_delayed_charging("BCU123456", enabled=True)
        assert fake_rest.last_path == "setdelayedcharging"
        assert fake_rest.last_body == {"evse_id": "BCU123456", "value": True}

    async def test_set_delayed_charging_schedule(self, offline_client: BlueCurrentClient, fake_rest: FakeRest):
        # Days may be a Weekday, an isoweekday number or a weekday name, and end up sorted and
        # deduplicated in a JSON-encoded string — which is how the backend wants them.
        await offline_client.set_delayed_charging_schedule(
            "BCU123456", start_time=time(23, 0), end_time=time(7, 0), days=[Weekday.WEDNESDAY, 2, "mo", "MONDAY"]
        )
        assert fake_rest.last_path == "savescheduledelayedcharging"
        assert fake_rest.last_body == {
            "evse_id": "BCU123456",
            "start_time": "23:00",
            "end_time": "07:00",
            "days": "[1,2,3]",
        }

    async def test_set_delayed_charging_schedule_with_string_times(
        self, offline_client: BlueCurrentClient, fake_rest: FakeRest
    ):
        await offline_client.set_delayed_charging_schedule("BCU123456", "9:30", "17:00", days=[7])
        assert fake_rest.last_body["start_time"] == "09:30"
        assert fake_rest.last_body["end_time"] == "17:00"
        assert fake_rest.last_body["days"] == "[7]"

    async def test_set_delayed_charging_schedule_without_days(self, offline_client: BlueCurrentClient):
        with raises(ValueError):
            await offline_client.set_delayed_charging_schedule("BCU123456", time(23, 0), time(7, 0), days=[])

    async def test_set_delayed_charging_schedule_with_invalid_day(
        self, offline_client: BlueCurrentClient, fake_rest: FakeRest
    ):
        with raises(ValueError):
            await offline_client.set_delayed_charging_schedule(
                "BCU123456", time(23, 0), time(7, 0), days=[1, 2, "invalid"]
            )
        # Nothing is sent when a day cannot be resolved.
        assert fake_rest.requests == []

    async def test_boost_overrides_delayed_charging(
        self, offline_client: BlueCurrentClient, fake_socket: FakeSocket, fake_rest: FakeRest
    ):
        # boost() reads the settings to see which profile is active, then overrides that one.
        fake_socket.on(
            "GET_CH_SETTINGS",
            {
                "object": "CH_SETTINGS",
                "data": {"delayed_charging": {"value": True}, "price_based_charging": {"value": False}},
            },
        )
        await offline_client.boost("BCU123456")
        assert fake_rest.last_path == "overridedelayedchargingtimeout"
        assert fake_rest.last_body == {"evse_id": "BCU123456"}

    async def test_rejection_is_raised(self, offline_client: BlueCurrentClient, fake_rest: FakeRest):
        # A rejected command answers with an HTTP error carrying the same body the websocket
        # sends for a rejection, so it surfaces as the same exception.
        fake_rest.on("setdelayedcharging", {"object": "ERROR", "error": 2, "message": "forbidden"}, status_code=401)
        with raises(BlueCurrentException) as exc:
            await offline_client.set_delayed_charging("BCU123456", enabled=True)
        assert exc.value.args[0]["message"] == "forbidden"

    async def test_failure_is_raised(self, offline_client: BlueCurrentClient, fake_rest: FakeRest):
        fake_rest.on("savescheduledelayedcharging", {"success": False, "error": "invalid schedule"})
        with raises(BlueCurrentException) as exc:
            await offline_client.set_delayed_charging_schedule("BCU123456", time(23, 0), time(7, 0), days=[1])
        assert exc.value.args[0]["error"] == "invalid schedule"


class TestOfflinePriceBasedCharging:
    """Price-based charging, which is sent over REST rather than over the websocket."""

    async def test_set_price_based_charging(self, offline_client: BlueCurrentClient, fake_rest: FakeRest):
        await offline_client.set_price_based_charging("BCU123456", enabled=True)
        assert fake_rest.last_path == "setpricebasedcharging"
        assert fake_rest.last_body == {"evse_id": "BCU123456", "value": True}

    async def test_set_price_based_charging_settings(self, offline_client: BlueCurrentClient, fake_rest: FakeRest):
        # The departure time may be a time or a "HH:MM" string; the kWh values may be fractional.
        await offline_client.set_price_based_charging_settings(
            "BCU123456", expected_departure_time=time(7, 0), expected_kwh=25.1, minimum_kwh=10
        )
        assert fake_rest.last_path == "setpricebasedsettings"
        assert fake_rest.last_body == {
            "evse_id": "BCU123456",
            "expected_departure_time": "07:00",
            "expected_kwh": 25.1,
            "minimum_kwh": 10,
        }

    async def test_boost_overrides_price_based_charging(
        self, offline_client: BlueCurrentClient, fake_socket: FakeSocket, fake_rest: FakeRest
    ):
        fake_socket.on(
            "GET_CH_SETTINGS",
            {
                "object": "CH_SETTINGS",
                "data": {"delayed_charging": {"value": False}, "price_based_charging": {"value": True}},
            },
        )
        await offline_client.boost("BCU123456")
        assert fake_rest.last_path == "overridechargingprofiles"
        assert fake_rest.last_body == {"boost": True, "evse_id": "BCU123456"}

    async def test_boost_without_active_profile(
        self, offline_client: BlueCurrentClient, fake_socket: FakeSocket, fake_rest: FakeRest
    ):
        fake_socket.on(
            "GET_CH_SETTINGS",
            {
                "object": "CH_SETTINGS",
                "data": {"delayed_charging": {"value": False}, "price_based_charging": {"value": False}},
            },
        )
        with raises(ValueError):
            await offline_client.boost("BCU123456")
        # Nothing is overridden when no profile is active.
        assert fake_rest.requests == []

    async def test_failure_is_raised(self, offline_client: BlueCurrentClient, fake_rest: FakeRest):
        fake_rest.on("setpricebasedsettings", {"success": False, "error": "invalid settings"})
        with raises(BlueCurrentException) as exc:
            await offline_client.set_price_based_charging_settings("BCU123456", time(7, 0), 25, 10)
        assert exc.value.args[0]["error"] == "invalid settings"


class TestOfflineLifecycle:
    """Connection-failure surfacing and teardown."""

    async def test_inflight_receive_woken_by_drop(self, offline_client: BlueCurrentClient, fake_socket: FakeSocket):
        # A waiter blocked on a reply is woken by a mid-call drop, not left hanging until timeout.
        task = create_task(offline_client._receive("NEVER", timeout=10))
        await sleep(0)
        fake_socket.close()
        with raises(ConnectionLost):
            await task

    async def test_receive_after_death_fast_fails(self, offline_client: BlueCurrentClient, fake_socket: FakeSocket):
        fake_socket.close()
        await _drain(offline_client)
        with raises(ConnectionLost):
            await offline_client._receive("X", timeout=10)

    async def test_public_call_after_death_raises(self, offline_client: BlueCurrentClient, fake_socket: FakeSocket):
        # The guard reaches through _request / _send, so a normal read fails fast too.
        fake_socket.close()
        await _drain(offline_client)
        with raises(ConnectionLost):
            await offline_client.get_grid_status("A")

    async def test_send_after_death_raises(self, offline_client: BlueCurrentClient, fake_socket: FakeSocket):
        fake_socket.close()
        await _drain(offline_client)
        with raises(ConnectionLost):
            await offline_client._send({"command": "PING"})

    async def test_aexit_awaits_and_is_idempotent(self, monkeypatch: MonkeyPatch, fake_socket: FakeSocket):
        monkeypatch.setattr("pybluecurrent.client.connect", make_fake_connect(fake_socket))
        async with BlueCurrentClient("username", "password") as client:
            consumer = client.consumer
        assert consumer is not None
        assert consumer.done()  # the handler was awaited, not just cancelled fire-and-forget
        assert client.consumer is None and client.socket is None and client.connection is None
        await client.__aexit__(None, None, None)  # a second teardown must not raise

    async def test_per_call_deadline_survives_noise(self, offline_client: BlueCurrentClient, fake_socket: FakeSocket):
        # A batch of non-matching frames must not swallow the deadline; the call still times out.
        for _ in range(20):
            fake_socket.feed({"object": "NOISE"})
        with raises(RequestTimeout):
            await offline_client._receive("WANTED", timeout=0.1)

    async def test_new_exceptions_are_bluecurrent_exceptions(self):
        assert issubclass(RequestTimeout, TimeoutError)
        assert issubclass(RequestTimeout, BlueCurrentException)
        assert issubclass(ConnectionLost, BlueCurrentException)
        assert issubclass(AuthenticationFailed, (BlueCurrentException, ValueError))


def _sequence(*connections):
    """A connect factory that hands out ``connections`` in order (fails loudly if over-consumed)."""
    it = iter(connections)

    def factory():
        try:
            return next(it)
        except StopIteration:  # pragma: no cover - a test scripting bug, surfaced immediately
            raise AssertionError("unexpected extra connect attempt")

    return factory


def _setup_reconnecting(monkeypatch, fake_rest, factory, *, uniform_value=1.0):
    """Wire an auto-reconnecting client to a scripted transport; return (client, recorded_delays).

    ``sleep`` is replaced with a no-op that records the requested backoff (so tests run instantly and
    can assert the backoff schedule), and ``uniform`` is pinned so ``delay == backoff``.
    """
    monkeypatch.setattr("pybluecurrent.client.connect", make_reconnecting_connect(factory))
    monkeypatch.setattr("pybluecurrent.client.AsyncClient", make_fake_async_client(fake_rest))
    monkeypatch.setattr("pybluecurrent.client.uniform", lambda _a, _b: uniform_value)
    delays: list[float] = []

    async def fake_sleep(delay):
        delays.append(delay)
        await sleep(0)  # yield so the rest of the loop makes progress, but never actually wait

    monkeypatch.setattr("pybluecurrent.client.sleep", fake_sleep)
    client = BlueCurrentClient("username", "password")
    return client, delays


def _validate_count(*sockets):
    return sum(m.get("command") == "VALIDATE_PASSWORD" for s in sockets for m in s.sent)


class TestReconnect:
    """The auto-reconnect supervisor (auto_reconnect=True), driven by a reconnecting fake transport."""

    async def test_transparent_reconnect_reuses_token(self, monkeypatch: MonkeyPatch, fake_rest: FakeRest):
        first, second = FakeSocket(), FakeSocket()
        second.on("GET_GRID_STATUS", {"object": "GRID_STATUS", "data": {"id": "GRID-2"}})
        client, _ = _setup_reconnecting(
            monkeypatch, fake_rest, _sequence(FakeConnection(first), FakeConnection(second))
        )
        async with client:
            token = client.token
            first.close()
            await _wait_until(lambda: client.socket is second and client._connected.is_set())
            assert client.token == token  # reused the cached token — no re-login
            assert _validate_count(second) == 0
            assert (await client.get_grid_status("A"))["id"] == "GRID-2"

    async def test_inflight_call_fails_then_client_recovers(self, monkeypatch: MonkeyPatch, fake_rest: FakeRest):
        first, second = FakeSocket(), FakeSocket()
        second.on("GET_GRID_STATUS", {"object": "GRID_STATUS", "data": {"id": "GRID-2"}})
        client, _ = _setup_reconnecting(
            monkeypatch, fake_rest, _sequence(FakeConnection(first), FakeConnection(second))
        )
        async with client:
            waiter = create_task(client._receive("NEVER"))
            await sleep(0)
            first.close()
            with raises(ConnectionLost):
                await waiter  # in-flight call fails, not retried
            await _wait_until(lambda: client.socket is second and client._connected.is_set())
            assert (await client.get_grid_status("A"))["id"] == "GRID-2"  # ...but the client recovered

    async def test_new_call_blocks_until_reconnected(self, monkeypatch: MonkeyPatch, fake_rest: FakeRest):
        first, second = FakeSocket(), FakeSocket()
        second.responder.pop("HELLO")  # withhold the reconnect handshake until we release it
        second.on("GET_GRID_STATUS", {"object": "GRID_STATUS", "data": {"id": "GRID-2"}})
        client, _ = _setup_reconnecting(
            monkeypatch, fake_rest, _sequence(FakeConnection(first), FakeConnection(second))
        )
        async with client:
            first.close()
            await _wait_until(lambda: client.socket is second and not client._connected.is_set())
            call = create_task(client.get_grid_status("A"))
            for _ in range(10):
                await sleep(0)
            assert not call.done()  # gated: connection not ready yet
            second.feed({"object": "HELLO"})  # release the reconnect handshake
            assert (await call)["id"] == "GRID-2"

    async def test_backoff_grows_and_caps(self, monkeypatch: MonkeyPatch, fake_rest: FakeRest):
        first, final = FakeSocket(), FakeSocket()
        failures = [FailingConnection(OSError("boom")) for _ in range(5)]
        client, delays = _setup_reconnecting(
            monkeypatch, fake_rest, _sequence(FakeConnection(first), *failures, FakeConnection(final))
        )
        client.reconnect_initial_backoff = 1.0
        client.reconnect_max_backoff = 8.0
        client.reconnect_backoff_multiplier = 2.0
        async with client:
            first.close()
            await _wait_until(lambda: client.socket is final and client._connected.is_set())
            assert delays == [1.0, 2.0, 4.0, 8.0, 8.0, 8.0]

    async def test_transport_failure_keeps_token(self, monkeypatch: MonkeyPatch, fake_rest: FakeRest):
        first, final = FakeSocket(), FakeSocket()
        client, _ = _setup_reconnecting(
            monkeypatch,
            fake_rest,
            _sequence(FakeConnection(first), FailingConnection(OSError()), FakeConnection(final)),
        )
        async with client:
            token = client.token
            first.close()
            await _wait_until(lambda: client.socket is final and client._connected.is_set())
            assert client.token == token
            assert len(client._login_times) == 1  # only the initial login; the transport retry issued none

    async def test_backoff_resets_after_stable_uptime(self, monkeypatch: MonkeyPatch, fake_rest: FakeRest):
        clock = [1000.0]
        first, second, third = FakeSocket(), FakeSocket(), FakeSocket()
        client, delays = _setup_reconnecting(
            monkeypatch,
            fake_rest,
            _sequence(
                FakeConnection(first),
                FailingConnection(OSError()),
                FakeConnection(second),
                FakeConnection(third),
            ),
        )
        monkeypatch.setattr(client, "_now", lambda: clock[0])
        client.reconnect_initial_backoff = 1.0
        client.reconnect_max_backoff = 100.0
        client.reconnect_backoff_multiplier = 2.0
        client.reconnect_stable_period = 30.0
        async with client:
            first.close()  # drop 1 (uptime 0): one failure grows backoff 1->2, then success on `second`
            await _wait_until(lambda: client.socket is second and client._connected.is_set())
            assert delays == [1.0, 2.0]
            clock[0] = 1100.0  # long uptime (100 >= 30) before drop 2 -> backoff resets to initial
            second.close()
            await _wait_until(lambda: client.socket is third and client._connected.is_set())
            assert delays[2] == 1.0

    async def test_backoff_kept_after_short_uptime(self, monkeypatch: MonkeyPatch, fake_rest: FakeRest):
        clock = [1000.0]
        first, second, third = FakeSocket(), FakeSocket(), FakeSocket()
        client, delays = _setup_reconnecting(
            monkeypatch,
            fake_rest,
            _sequence(
                FakeConnection(first),
                FailingConnection(OSError()),
                FakeConnection(second),
                FakeConnection(third),
            ),
        )
        monkeypatch.setattr(client, "_now", lambda: clock[0])
        client.reconnect_initial_backoff = 1.0
        client.reconnect_max_backoff = 100.0
        client.reconnect_backoff_multiplier = 2.0
        client.reconnect_stable_period = 30.0
        async with client:
            first.close()  # drop 1: backoff grows to 2 via one failure, then success
            await _wait_until(lambda: client.socket is second and client._connected.is_set())
            assert delays == [1.0, 2.0]
            second.close()  # drop 2, short uptime (0 < 30) -> backoff NOT reset, stays 2
            await _wait_until(lambda: client.socket is third and client._connected.is_set())
            assert delays[2] == 2.0

    async def test_reconnect_gives_up_after_repeated_logins(self, monkeypatch: MonkeyPatch, fake_rest: FakeRest):
        first = FakeSocket()
        # Each reconnect socket rejects HELLO (a stale-token signal), so the token is cleared and a
        # fresh login attempted every cycle; the client stops after reconnect_max_relogins of them.
        reconnects = [FakeSocket() for _ in range(4)]
        for s in reconnects:
            s.on("HELLO", {"object": "ERROR", "error": 1, "message": "Invalid Auth Token"})
        client, _ = _setup_reconnecting(
            monkeypatch, fake_rest, _sequence(FakeConnection(first), *(FakeConnection(s) for s in reconnects))
        )
        client.reconnect_max_relogins = 2
        async with client:
            first.close()
            await _wait_until(lambda: client._closed is not None)
            assert isinstance(client._closed, ConnectionLost)
            assert client._connected.is_set()
            assert _validate_count(first, *reconnects) == 2  # never issues a 3rd VALIDATE_PASSWORD
            with raises(ConnectionLost):
                await client.get_grid_status("A")  # gated call fails promptly, no hang

    async def test_bad_credentials_stops_reconnect(self, monkeypatch: MonkeyPatch, fake_rest: FakeRest):
        first, reject_hello, reject_login = FakeSocket(), FakeSocket(), FakeSocket()
        reject_hello.on("HELLO", {"object": "ERROR", "error": 1, "message": "Invalid Auth Token"})
        reject_login.on("VALIDATE_PASSWORD", {"object": "STATUS_PASSWORD", "accepted": False})
        client, _ = _setup_reconnecting(
            monkeypatch,
            fake_rest,
            _sequence(FakeConnection(first), FakeConnection(reject_hello), FakeConnection(reject_login)),
        )
        async with client:
            first.close()
            await _wait_until(lambda: client._closed is not None)
            assert isinstance(client._closed, AuthenticationFailed)  # permanent — no retry loop
            with raises(AuthenticationFailed):
                await client.get_grid_status("A")

    async def test_clean_shutdown(self, monkeypatch: MonkeyPatch, fake_rest: FakeRest):
        first, second = FakeSocket(), FakeSocket()
        client, _ = _setup_reconnecting(
            monkeypatch, fake_rest, _sequence(FakeConnection(first), FakeConnection(second))
        )
        async with client:
            supervisor = client._supervisor
            first.close()
            await _wait_until(lambda: client.socket is second and client._connected.is_set())
        assert supervisor is not None and supervisor.done()
        assert client.consumer is None and client.socket is None
        assert client.connection is None and client.httpx_client is None
        await client.__aexit__(None, None, None)  # idempotent

    async def test_rest_transport_survives_reconnect(self, monkeypatch: MonkeyPatch, fake_rest: FakeRest):
        first, second = FakeSocket(), FakeSocket()
        second.responder.pop("HELLO")  # keep the reconnect pending (mid-backoff window)
        client, _ = _setup_reconnecting(
            monkeypatch, fake_rest, _sequence(FakeConnection(first), FakeConnection(second))
        )
        async with client:
            httpx = client.httpx_client
            first.close()
            await _wait_until(lambda: client.socket is second and not client._connected.is_set())
            # REST transport is untouched by the websocket drop/reconnect — same client, still alive.
            assert client.httpx_client is httpx is not None
            second.feed({"object": "HELLO"})  # let it finish so shutdown is clean

    async def test_auto_reconnect_false_fast_fails(
        self, monkeypatch: MonkeyPatch, fake_socket: FakeSocket, fake_rest: FakeRest
    ):
        monkeypatch.setattr("pybluecurrent.client.connect", make_fake_connect(fake_socket))
        monkeypatch.setattr("pybluecurrent.client.AsyncClient", make_fake_async_client(fake_rest))
        client = BlueCurrentClient("username", "password")
        client.auto_reconnect = False
        async with client:
            assert client._supervisor is None
            fake_socket.close()
            await _drain(client)
            with raises(ConnectionLost):
                await client.get_grid_status("A")

    async def test_first_connect_failure_starts_no_supervisor(
        self, monkeypatch: MonkeyPatch, fake_socket: FakeSocket, fake_rest: FakeRest
    ):
        fake_socket.on("VALIDATE_PASSWORD", {"object": "STATUS_PASSWORD", "accepted": False})
        monkeypatch.setattr("pybluecurrent.client.connect", make_fake_connect(fake_socket))
        monkeypatch.setattr("pybluecurrent.client.AsyncClient", make_fake_async_client(fake_rest))
        client = BlueCurrentClient("username", "password")
        with raises(AuthenticationFailed):
            await client.__aenter__()
        assert client._supervisor is None
        assert client.consumer is None and client.socket is None


class TestLogging:
    """Logger naming, credential redaction, and coverage of the reconnect failure paths."""

    def test_logger_is_module_named(self):
        assert client_logger.name == "pybluecurrent.client"

    def test_redact_masks_sensitive_keys(self):
        assert _redact({"object": "STATUS_PASSWORD", "accepted": True, "token": "secret"}) == {
            "object": "STATUS_PASSWORD",
            "accepted": True,
            "token": "***",
        }
        assert _redact({"Authorization": "Token secret", "x": 1}) == {"Authorization": "***", "x": 1}

    def test_redact_passes_through_when_nothing_sensitive(self):
        plain = {"object": "GRID_STATUS", "data": {"id": "G1"}}
        assert _redact(plain) is plain  # no copy when there's nothing to mask
        assert _redact("raw non-dict frame") == "raw non-dict frame"

    async def test_received_frame_logs_without_token(
        self, monkeypatch: MonkeyPatch, fake_socket: FakeSocket, fake_rest: FakeRest, caplog
    ):
        # The login handshake pushes a STATUS_PASSWORD frame (which carries the token) through _handler.
        monkeypatch.setattr("pybluecurrent.client.connect", make_fake_connect(fake_socket))
        monkeypatch.setattr("pybluecurrent.client.AsyncClient", make_fake_async_client(fake_rest))
        client = BlueCurrentClient("username", "password")
        client.auto_reconnect = False
        with caplog.at_level(logging.DEBUG, logger="pybluecurrent.client"):
            async with client:
                pass
        logged = "\n".join(record.getMessage() for record in caplog.records)
        assert "STATUS_PASSWORD" in logged  # the frame was logged...
        assert FAKE_TOKEN not in logged  # ...but the token was masked
        assert "'token': '***'" in logged

    async def test_reconnect_abandoned_is_logged(self, monkeypatch: MonkeyPatch, fake_rest: FakeRest, caplog):
        first = FakeSocket()
        reconnects = [FakeSocket() for _ in range(4)]
        for socket in reconnects:
            socket.on("HELLO", {"object": "ERROR", "error": 1, "message": "Invalid Auth Token"})
        client, _ = _setup_reconnecting(
            monkeypatch, fake_rest, _sequence(FakeConnection(first), *(FakeConnection(s) for s in reconnects))
        )
        client.reconnect_max_relogins = 2
        with caplog.at_level(logging.ERROR, logger="pybluecurrent.client"):
            async with client:
                first.close()
                await _wait_until(lambda: client._closed is not None)
        assert any("Reconnect abandoned" in record.getMessage() for record in caplog.records)
