from asyncio import CancelledError, create_task, gather, sleep
from contextlib import suppress
from datetime import date, datetime, time
from json import JSONDecodeError

from fake_rest import FakeRest
from fake_socket import FAKE_CUSTOMER_ID, FAKE_TOKEN, FakeSocket, load_fixture, make_fake_connect
from pytest import MonkeyPatch, raises

from pybluecurrent import BlueCurrentClient, Weekday
from pybluecurrent.exceptions import AuthenticationFailed, BlueCurrentException


async def _expect_auth_failure(monkeypatch: MonkeyPatch, socket: FakeSocket, **client_kwargs) -> None:
    """Connect a client backed by ``socket`` and assert ``__aenter__`` fails authentication.

    Cleans up the leaked handler task / httpx client itself, since ``__aexit__`` never runs when
    ``__aenter__`` raises (a lifecycle gap tracked separately in #9).
    """
    monkeypatch.setattr("pybluecurrent.client.connect", make_fake_connect(socket))
    client = BlueCurrentClient(**client_kwargs)
    try:
        with raises(AuthenticationFailed):
            await client.__aenter__()
    finally:
        if client.consumer is not None:
            client.consumer.cancel()
        if client.httpx_client is not None:
            await client.httpx_client.__aexit__(None, None, None)


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
        with raises(TimeoutError):
            await offline_client._receive("NEVER_ARRIVES", timeout=0)

    async def test_handler_fans_out_to_all_waiters(self, offline_client: BlueCurrentClient, fake_socket: FakeSocket):
        # A single frame reaches every concurrent waiter — the broadcast behaviour that #8 hardens.
        first = create_task(offline_client._receive("PING"))
        second = create_task(offline_client._receive("PING"))
        await sleep(0)
        fake_socket.feed({"object": "PING", "value": 1})
        assert (await first)["value"] == 1
        assert (await second)["value"] == 1

    async def test_non_json_frame_kills_consumer(self, offline_client: BlueCurrentClient, fake_socket: FakeSocket):
        # Documents current behaviour: a malformed frame kills the handler task (a seam #9 will address).
        consumer = offline_client.consumer
        assert consumer is not None
        fake_socket.feed("this is not json")
        for _ in range(5):
            await sleep(0)
            if consumer.done():
                break
        assert consumer.done()
        assert isinstance(consumer.exception(), JSONDecodeError)


class TestOfflineCommands:
    """Command round-trips driven by recorded fixtures."""

    async def test_get_charge_points(self, offline_client: BlueCurrentClient, fake_socket: FakeSocket):
        fake_socket.on("GET_CHARGE_POINTS", load_fixture("charge_points"))
        charge_points = await offline_client.get_charge_points()
        assert charge_points[0]["evse_id"] == "BCU123456"

    async def test_get_grid_status(self, offline_client: BlueCurrentClient, fake_socket: FakeSocket):
        fake_socket.on("GET_GRID_STATUS", load_fixture("grid_status"))
        status = await offline_client.get_grid_status("BCU123456")
        assert status["id"] == "GRID-BCU123456"
        assert status["grid_actual_p1"] == 1

    async def test_get_charge_point_settings(self, offline_client: BlueCurrentClient, fake_socket: FakeSocket):
        fake_socket.on("GET_CH_SETTINGS", load_fixture("charge_point_settings"))
        settings = await offline_client.get_charge_point_settings("BCU123456")
        assert settings["evse_id"] == "BCU123456"

    async def test_get_charge_cards(self, offline_client: BlueCurrentClient, fake_socket: FakeSocket):
        fake_socket.on("GET_CHARGE_CARDS", load_fixture("charge_cards"))
        cards = await offline_client.get_charge_cards()
        assert cards[0]["uid"] == "A1B2C3D4E5F6"
        assert cards[0]["date_created"] == date(2023, 6, 27)
        assert cards[0]["date_became_invalid"] is None

    async def test_get_account(self, offline_client: BlueCurrentClient, fake_socket: FakeSocket):
        fake_socket.on("GET_ACCOUNT", load_fixture("account"))
        account = await offline_client.get_account()
        assert account["full_name"] == "Your Full Name"
        assert account["first_login_app"] == datetime(2020, 1, 15, 13, 33, 52)

    async def test_get_sustainability_status(self, offline_client: BlueCurrentClient, fake_socket: FakeSocket):
        fake_socket.on("GET_SUSTAINABILITY_STATUS", load_fixture("sustainability_status"))
        status = await offline_client.get_sustainability_status()
        assert status == {"trees": 1, "co2": 12.345}

    async def test_error_is_raised(self, offline_client: BlueCurrentClient, fake_socket: FakeSocket):
        fake_socket.on("GET_GRID_STATUS", load_fixture("error_forbidden"))
        with raises(BlueCurrentException) as exc:
            await offline_client.get_grid_status("BOGUS")
        assert exc.value.args[0]["message"] == "forbidden"


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

    async def test_boost(self, offline_client: BlueCurrentClient, fake_rest: FakeRest):
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
