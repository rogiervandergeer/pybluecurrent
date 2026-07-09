from asyncio import create_task, sleep
from datetime import date, datetime
from json import JSONDecodeError
from os import environ

from fake_socket import FAKE_CUSTOMER_ID, FAKE_TOKEN, FakeSocket, load_fixture, make_fake_connect
from pytest import MonkeyPatch, mark, raises, skip

from pybluecurrent import BlueCurrentClient
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


class TestHeaders:
    def test_user_agent(self, client: BlueCurrentClient):
        user_agent = client._user_agent
        assert user_agent.startswith("pybluecurrent ")
        assert len(user_agent.split(".")) in (3, 4)  # Either pybluecurrent x.y.z or pybluecurrent x.y.z.dev0
        assert "+" not in user_agent


class TestAuthentication:
    async def test_authenticate(self, client_with_auth: BlueCurrentClient):
        async with client_with_auth:
            assert client_with_auth.token is not None

    async def test_authentication_failed(self, monkeypatch: MonkeyPatch, fake_socket: FakeSocket):
        fake_socket.on("VALIDATE_PASSWORD", {"object": "STATUS_PASSWORD", "accepted": False})
        await _expect_auth_failure(monkeypatch, fake_socket, username="username", password="password")

    async def test_authentication_rejected_live(self, connected_client: BlueCurrentClient):
        # Guards the rejection *contract* against backend drift: the offline test above scripts our
        # assumption of how rejection looks, so this hits the real backend with bad credentials to
        # confirm the client still raises. The connected_client fixture skips it when no creds are set.
        with raises(AuthenticationFailed):
            async with BlueCurrentClient(username="invalid", password="invalid"):
                pass


class TestSocketApi:
    async def test_get_account(self, connected_client: BlueCurrentClient):
        account = await connected_client.get_account()
        assert "full_name" in account
        assert isinstance(account["first_login_app"], date)

    async def test_get_charge_cards(self, connected_client: BlueCurrentClient):
        charge_cards = await connected_client.get_charge_cards()
        if len(charge_cards) == 0:
            skip(reason="No charge cards.")
        assert all("uid" in charge_card for charge_card in charge_cards)
        assert all(
            obj is None or isinstance(obj, date)
            for charge_card in charge_cards
            for obj in [
                charge_card["date_created"],
                charge_card["date_modified"],
                charge_card["date_became_invalid"],
            ]
        )

    async def test_get_charge_points(self, connected_client: BlueCurrentClient):
        charge_points = await connected_client.get_charge_points()
        if len(charge_points) == 0:
            skip(reason="No charge cards.")
        for charge_point in charge_points:
            assert "evse_id" in charge_point

    async def test_get_grid_status(self, connected_client: BlueCurrentClient, evse_id: str):
        status = await connected_client.get_grid_status(evse_id=evse_id)
        assert "grid_actual_p1" in status
        assert "id" in status

    async def test_get_charge_point_settings(self, connected_client: BlueCurrentClient, evse_id: str):
        settings = await connected_client.get_charge_point_settings(evse_id=evse_id)
        assert isinstance(settings, dict)
        assert settings["evse_id"] == evse_id

    @mark.skip("Does not work")
    async def test_get_sessions(self, connected_client: BlueCurrentClient, evse_id: str):
        sessions = await connected_client.get_sessions(evse_id=evse_id)
        print(sessions)

    async def test_get_sustainability_status(self, connected_client: BlueCurrentClient):
        sessions = await connected_client.get_sustainability_status()
        assert set(sessions.keys()) == {"trees", "co2"}

    @mark.skip("Does not work.")
    async def test_unlock_connector(self, connected_client: BlueCurrentClient, evse_id: str):
        result = await connected_client.unlock_connector(evse_id=evse_id)
        print(result)

    @mark.skip("Do not change chargepoint status.")
    async def test_soft_reset(self, connected_client: BlueCurrentClient, evse_id: str):
        _ = await connected_client.soft_reset(evse_id=evse_id)

    @mark.skipif(environ.get("BLUECURRENT_READ_ONLY", "TRUE") != "FALSE", reason="Running read-only tests.")
    async def test_set_plug_and_charge_card(self, connected_client: BlueCurrentClient, evse_id: str):
        async def _get_plug_and_charge_card_uid() -> str | None:
            settings = await connected_client.get_charge_point_settings(evse_id=evse_id)
            try:
                return settings["plug_and_charge_charge_card"]["uid"]  # type: ignore
            except KeyError:
                return None

        # Get the original card, if any
        before_card = await _get_plug_and_charge_card_uid()

        # Get all possible cards, including no card
        charge_cards = await connected_client.get_charge_cards()
        if len(charge_cards) == 0:
            skip(reason="No charge cards.")
        uids: list[str | None] = [charge_card["uid"] for charge_card in charge_cards] + ["BCU_HOME_USE"]  # type: ignore
        # Set each card as plug_and_charge_card
        for uid in uids:
            if uid != before_card:
                await connected_client.set_plug_and_charge_charge_card(evse_id=evse_id, uid=uid)
                assert await _get_plug_and_charge_card_uid() == uid
        # Set the original card as plug_and_charge_card
        await connected_client.set_plug_and_charge_charge_card(evse_id=evse_id, uid=before_card)
        assert await _get_plug_and_charge_card_uid() == before_card

    @mark.skipif(environ.get("BLUECURRENT_READ_ONLY", "TRUE") != "FALSE", reason="Running read-only tests.")
    async def test_set_invalid_plug_and_charge_card(self, connected_client: BlueCurrentClient, evse_id: str):
        settings = await connected_client.get_charge_point_settings(evse_id=evse_id)
        with raises(BlueCurrentException):
            await connected_client.set_plug_and_charge_charge_card(evse_id=evse_id, uid="INVALID_CARD")
        assert await connected_client.get_charge_point_settings(evse_id=evse_id) == settings

    @mark.skipif(environ.get("BLUECURRENT_READ_ONLY", "TRUE") != "FALSE", reason="Running read-only tests.")
    async def test_set_status(self, connected_client: BlueCurrentClient, evse_id: str):
        before_status = await connected_client.get_charge_point_status(evse_id=evse_id)
        if before_status["activity"] != "available":
            skip(reason="Only perform this test if the charge point is available.")
        await connected_client.set_status(evse_id=evse_id, enabled=False)
        assert (await connected_client.get_charge_point_status(evse_id=evse_id))["activity"] == "unavailable"
        await connected_client.set_status(evse_id=evse_id, enabled=True)
        assert (await connected_client.get_charge_point_status(evse_id=evse_id))["activity"] == "available"

    async def test_error(self, connected_client: BlueCurrentClient):
        with raises(BlueCurrentException) as e:
            await connected_client.set_status("BCU123456", False)
        assert e.value.args[0]["message"] == "forbidden"


class TestRestApi:
    async def test_get_contracts(self, connected_client: BlueCurrentClient):
        contracts = await connected_client.get_contracts()
        assert len(contracts) > 0
        assert "contract_id" in contracts[0]

    async def test_get_charge_point_status(self, connected_client: BlueCurrentClient, evse_id: str):
        status = await connected_client.get_charge_point_status(evse_id)
        assert status["evse_id"] == evse_id
        assert "activity" in status

    async def test_get_grids(self, connected_client: BlueCurrentClient):
        grids = await connected_client.get_grids()
        assert len(grids) > 0
        assert "id" in grids[0]

    async def test_get_transactions(self, connected_client: BlueCurrentClient, evse_id: str):
        transactions = await connected_client.get_transactions(evse_id)
        assert "transactions" in transactions

    async def test_iterate_transactions(self, connected_client: BlueCurrentClient, evse_id: str):
        transactions = await connected_client.get_transactions(evse_id)
        # If there are less than three pages, there might be fewer than 30.
        if transactions["total_pages"] < 3:  # type: ignore
            skip("Not enough transactions.")
        n_transactions = 0
        # Verify pagination works correctly - we get 30 unique transactions from multiple pages.
        unique_transactions = set()
        async for transaction in connected_client.iterate_transactions(evse_id):
            n_transactions += 1
            unique_transactions.add(transaction["transaction_id"])
            if n_transactions >= 30:
                break
        assert len(unique_transactions) == 30

    async def test_get_api_token(self, connected_client: BlueCurrentClient):
        token = await connected_client.get_api_token()
        assert isinstance(token, str)
        assert token

    async def test_api_token_auth(self, connected_client: BlueCurrentClient):
        token = await connected_client.get_api_token()
        async with BlueCurrentClient(api_token=token) as token_client:
            assert isinstance(await token_client.get_charge_points(), list)
            assert token_client.customer_id is not None


class TestOfflineAuth:
    """Auth handshake against the offline fake — no credentials, no network."""

    async def test_password_auth_success(self, offline_client: BlueCurrentClient):
        assert offline_client.token == FAKE_TOKEN

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
