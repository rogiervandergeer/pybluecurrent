from itertools import islice

from pytest import fixture, mark, raises, skip

from pybluecurrent import BlueCurrentClient
from pybluecurrent.exceptions import AuthenticationFailed, BlueCurrentException


class TestHeaders:
    def test_user_agent(self, client: BlueCurrentClient):
        user_agent = client._user_agent
        assert user_agent.startswith("pybluecurrent ")
        assert len(user_agent.split(".")) in (3, 4)  # Either pybluecurrent x.y.z or pybluecurrent x.y.z.dev0
        assert "+" not in user_agent


class TestAuthentication:
    @mark.asyncio
    async def test_authenticate(self, client_with_auth: BlueCurrentClient):
        async with client_with_auth:
            assert client_with_auth.token is not None

    @mark.asyncio
    async def test_authentication_failed(self, client: BlueCurrentClient):
        with raises(AuthenticationFailed):
            async with client:
                pass

    def test_login(self, client_with_auth: BlueCurrentClient):
        client_with_auth.login()
        assert client_with_auth.token is not None


class TestSocketApi:
    @mark.asyncio
    async def test_get_account(self, connected_client: BlueCurrentClient):
        account = await connected_client.get_account()
        assert "full_name" in account

    @mark.asyncio
    async def test_get_charge_cards(self, connected_client: BlueCurrentClient):
        charge_cards = await connected_client.get_charge_cards()
        if len(charge_cards) == 0:
            skip(reason="No charge cards.")
        assert all("uid" in charge_card for charge_card in charge_cards)

    @mark.asyncio
    async def test_get_charge_points(self, connected_client: BlueCurrentClient):
        charge_points = await connected_client.get_charge_points()
        if len(charge_points) == 0:
            skip(reason="No charge cards.")
        for charge_point in charge_points:
            assert "evse_id" in charge_point

    @mark.asyncio
    async def test_get_grid_status(self, connected_client: BlueCurrentClient, evse_id: str):
        status = await connected_client.get_grid_status(evse_id=evse_id)
        assert "grid_actual_p1" in status
        assert "id" in status

    @mark.asyncio
    async def test_get_status(self, connected_client: BlueCurrentClient, evse_id: str):
        status = await connected_client.get_status(evse_id=evse_id)
        assert status["evse_id"] == evse_id

    @mark.asyncio
    async def test_get_charge_point_settings(self, connected_client: BlueCurrentClient, evse_id: str):
        settings = await connected_client.get_charge_point_settings(evse_id=evse_id)
        assert isinstance(settings, dict)
        assert settings["evse_id"] == evse_id

    @mark.asyncio
    @mark.skip("Does not work")
    async def test_get_sessions(self, connected_client: BlueCurrentClient, evse_id: str):
        sessions = await connected_client.get_sessions(evse_id=evse_id)
        print(sessions)

    @mark.asyncio
    async def test_get_sustainability_status(self, connected_client: BlueCurrentClient):
        sessions = await connected_client.get_sustainability_status()
        assert set(sessions.keys()) == {"trees", "co2"}

    @mark.asyncio
    @mark.skip("Does not work.")
    async def test_unlock_connector(self, connected_client: BlueCurrentClient, evse_id: str):
        result = await connected_client.unlock_connector(evse_id=evse_id)
        print(result)

    @mark.asyncio
    @mark.skip("Do not change chargepoint status.")
    async def test_soft_reset(self, connected_client: BlueCurrentClient, evse_id: str):
        _ = await connected_client.soft_reset(evse_id=evse_id)

    @mark.asyncio
    async def test_set_status(self, connected_client: BlueCurrentClient, evse_id: str):
        before_status = connected_client.get_charge_point_status(evse_id=evse_id)
        if before_status["activity"] != "available":
            skip(reason="Only perform this test if the charge point is available.")
        await connected_client.set_status(evse_id=evse_id, enabled=False)
        assert connected_client.get_charge_point_status(evse_id=evse_id)["activity"] == "unavailable"
        await connected_client.set_status(evse_id=evse_id, enabled=True)
        assert connected_client.get_charge_point_status(evse_id=evse_id)["activity"] == "available"

    @mark.asyncio
    async def test_error(self, connected_client: BlueCurrentClient):
        with raises(BlueCurrentException) as e:
            await connected_client.set_status("BCU123456", False)
        assert e.value.args[0]["message"] == "forbidden"


class TestRestApi:
    @fixture(scope="class")
    def authenticated_client(self, client_with_auth: BlueCurrentClient) -> BlueCurrentClient:
        client_with_auth.login()
        return client_with_auth

    def test_get_contracts(self, authenticated_client: BlueCurrentClient):
        contracts = authenticated_client.get_contracts()
        assert len(contracts) > 0
        assert "contract_id" in contracts[0]

    def test_get_charge_point_status(self, authenticated_client: BlueCurrentClient, evse_id: str):
        status = authenticated_client.get_charge_point_status(evse_id)
        assert status["evse_id"] == evse_id
        assert "activity" in status

    def test_get_grids(self, authenticated_client: BlueCurrentClient):
        grids = authenticated_client.get_grids()
        assert len(grids) > 0
        assert "id" in grids[0]

    def test_get_transactions(self, authenticated_client: BlueCurrentClient, evse_id: str):
        transactions = authenticated_client.get_transactions(evse_id)
        assert "transactions" in transactions

    def test_iterate_transactions(self, authenticated_client: BlueCurrentClient, evse_id: str):
        transactions = authenticated_client.get_transactions(evse_id)
        # If there are less than three pages, there might be less than 30.
        if transactions["total_pages"] < 3:  # type: ignore
            skip("Not enough transactions.")
        assert len({t["transaction_id"] for t in islice(authenticated_client.iterate_transactions(evse_id), 30)}) == 30
