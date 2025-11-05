from datetime import date
from os import environ

from pytest import mark, raises, skip

from pybluecurrent import BlueCurrentClient
from pybluecurrent.exceptions import AuthenticationFailed, BlueCurrentException


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

    async def test_authentication_failed(self, client: BlueCurrentClient):
        with raises(AuthenticationFailed):
            async with client:
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
