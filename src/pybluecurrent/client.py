from asyncio import Task, create_task, run, wait_for
from json import dumps, loads
from logging import getLogger
from typing import Any, Iterable
from uuid import uuid4

from asyncio_multisubscriber_queue import MultisubscriberQueue
from requests import get, post
from sjcl import SJCL
from websockets import WebSocketClientProtocol, connect

from pybluecurrent._version import __version__
from pybluecurrent.exceptions import AuthenticationFailed, BlueCurrentException


class BlueCurrentClient:
    api_url: str = "https://bo.bluecurrent.nl/app/bc_api/api/v2.0"
    psk: str = "d9ab2352a935be4ade182ce4921044f8"
    socket_url: str = "wss://motown.bluecurrent.nl/appserver/2.0"

    def __init__(self, username: str, password: str):
        self.consumer: Task | None = None
        self.credentials: tuple[str, str] = (username, password)
        self.logger = getLogger("BlueCurrentClient")
        self.queue = MultisubscriberQueue()
        self.socket: WebSocketClientProtocol | None = None
        self.token: str | None = None

    ## Asynchronous API ##

    async def __aenter__(self) -> "BlueCurrentClient":
        self.logger.debug("Creating BlueCurrent websocket connection")
        self.connection = connect(self.socket_url, user_agent_header=self._user_agent)
        self.socket = await self.connection.__aenter__()
        self.consumer = create_task(self._handler())
        if self.token is None:
            await self._login()
        await self._hello()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        self.logger.debug("Closing BlueCurrent connection")
        self.consumer.cancel()
        await self.connection.__aexit__(exc_type, exc_val, exc_tb)
        self.consumer, self.socket = None, None

    async def get_account(self) -> dict[str, bool | str]:
        """
        Get account information.

        Returns:
            A dictionary describing your account:
            {
                "full_name": "Your Full Name",
                "email": "your@email.address",
                "login": "your@email.address",
                "should_reset_password": False,
                "developer_mode_enabled": False,
                "tel": "",
                "marketing_target": "bluecurrent",
                "first_login_app": "01-JAN-20",
                "hubspot_user_identity": "a_very_long_string"
            }
        """
        await self._send(dict(command="GET_ACCOUNT"), token=True)
        result = await self._receive("ACCOUNT")
        del result["object"]
        return result

    async def get_charge_cards(self):
        """
        Get your charge cards:

        Returns:
            A list of dictionaries, each representing a charge card:
            {
                "uid": "A1B2C3D4E5F6",
                "id": "NL-ABC-123456-0",
                "name": "My Charge Cards",
                "customer_name": "Your Name",
                "valid": 1,
                "date_created": "2023-06-27",
                "date_modified": "2023-07-11",
                "date_became_invalid": ""
            }
        """
        await self._send(dict(command="GET_CHARGE_CARDS"), token=True)
        return (await self._receive("CHARGE_CARDS"))["cards"]

    async def get_charge_points(self):
        """
        Get a list of your charge points.

        Returns:
            A list of dictionaries, each representing a charge card:
            {
                "evse_id": "BCU123456",
                "name": "",
                "model_type": "H:MOVE-C32T2",
                "chargepoint_type": "HIDDEN",
                "is_cable": True,
                "public_charging": {"value": False, "permission": "write"},
                                "default_card": {"uid": "A1B2C3D4E5F6", "id": "NL-ABC-123456-0", "name": "Your Card",
                                 "customer_name": "Your Name", "valid": 1},
                "preferred_card": {"uid": "A1B2C3D4E5F6", "id": "NL-ABC-123456-0", "name": "Your Card",
                                   "customer_name": "Your Name", "valid": 1},
                "plug_and_charge_card": {"uid": "A1B2C3D4E5F6", "id": "NL-ABC-123456-0", "name": "Your Card",
                                         "customer_name": "Your Name", "valid": 1},
                "tariff":  {"tariff_id","NLBCUT58", "price_ex_vat": 0.2, "start_price_ex_vat": 0, "price_in_vat": 0.242,
                            "start_price_in_vat": 0, "currency": "EUR", "vat_percentage": 21},
                "plug_and_charge_notification": False,
                "plug_and_charge": {"value": True, "permission": "write"},
                "led_interaction": {"value": False, "permission": "read"},
                "publish_location": {"value": False, "permission": "write"},
                "smart_charging": True,
                "smart_charging_dynamic": True,
                "activity": "available",
                "location": {"x_coord": 50.1234, "y_coord": 5.01234, "street": "Europalaan", "housenumber": "100",
                             "zipcode": "3526KS", "city": "Utrecht", "country": "NL"},
                "delayed_charging": {"value": False, "permission": "none"}
            }
        """
        await self._send(dict(command="GET_CHARGE_POINTS"), token=True)
        return (await self._receive("CHARGE_POINTS"))["data"]

    async def get_charge_point_settings(self, evse_id: str) -> dict[str, bool | dict]:
        """
        Get the settings of a charge point.

        All of this information is already included in the response of get_charge_points.

        Args:
            evse_id: A charge point ID.

        Returns:
            A dictionary describing the settings:
            {
                "evse_id": "BCU123456",
                "plug_and_charge": {"value": True, "permission": "write"},
                "public_charging": {"value": False, "permission": "write"},
                "default_card": {"uid": "A1B2C3D4E5F6", "id": "NL-ABC-123456-0", "name": "Your Card",
                                 "customer_name": "Your Name", "valid": 1},
                "preferred_card": {"uid": "A1B2C3D4E5F6", "id": "NL-ABC-123456-0", "name": "Your Card",
                                   "customer_name": "Your Name", "valid": 1},
                "plug_and_charge_card": {"uid": "A1B2C3D4E5F6", "id": "NL-ABC-123456-0", "name": "Your Card",
                                         "customer_name": "Your Name", "valid": 1},
                "smart_charging": True,
                "smart_charging_dynamic": True,
                "model_type": "H:MOVE-C32T2",
                "is_cable": True,
                "chargepoint_type": "HIDDEN",
                "plug_and_charge_notification": False,
                "led_intensity": {"value": 0, "permission": "none"},
                "led_interaction": {"value": False, "permission": "none"}
            }
        """
        await self._send(dict(command="GET_CH_SETTINGS", evse_id=evse_id), token=True)
        return (await self._receive("CH_SETTINGS"))["data"]

    async def get_grid_status(self, evse_id: str) -> dict[str, int | str]:
        """
        Get the grid status associated to a charge point.

        Args:
            evse_id: A charge point ID.

        Returns:
            A dictionary describing the actual grid current and maximum current in amps:
            {
                "id": "GRID-BCU123456",
                "grid_actual_p1": 1,
                "grid_actual_p2": 2,
                "grid_actual_p3": 3,
                "grid_max_install": 25,
                "grid_max_reserved": 25
            }
        """
        await self._send(dict(command="GET_GRID_STATUS", evse_id=evse_id), token=True)
        return (await self._receive("GRID_STATUS"))["data"]

    async def get_sessions(self, evse_id: str):
        await self._send(dict(command="GET_SESSIONS"), token=True)
        return await self._receive("SESSIONS")

    async def get_status(self, evse_id: str):
        await self._send(dict(command="GET_STATUS", evse_id=evse_id), token=True)
        return await self._receive("STATUS")

    async def get_sustainability_status(self) -> dict[str, float | int]:
        """
        Get statistics on the sustainability of all your charge points.

        Returns:
            A dictionary with two keys:
            {
                "trees": 1,
                "co2": 12.345
            }
        """
        await self._send(dict(command="GET_SUSTAINABILITY_STATUS"), token=True)
        result = await self._receive("SUSTAINABILITY_STATUS")
        result.pop("object")
        return result

    async def set_status(self, evse_id: str, enabled: bool):
        if enabled:
            await self._send(dict(command="SET_OPERATIVE", evse_id=evse_id, flow_id=str(uuid4())), token=True)
            await self._receive("RECEIVED_SET_OPERATIVE")
            await self._receive("STATUS_SET_OPERATIVE", timeout=30)
        else:
            await self._send(dict(command="SET_INOPERATIVE", evse_id=evse_id, flow_id=str(uuid4())), token=True)
            await self._receive("RECEIVED_SET_INOPERATIVE")
            await self._receive("STATUS_SET_INOPERATIVE", timeout=30)

    async def unlock_connector(self, evse_id: str):
        await self._send(
            dict(
                command="UNLOCK_CONNECTOR",
                evse_id=evse_id,
            ),
            token=True,
        )
        await self._receive("RECEIVED_UNLOCK_CONNECTOR")
        return await self._receive("STATUS_UNLOCK_CONNECTOR", timeout=30)

    async def soft_reset(self, evse_id: str):
        # TODO: verify flow id
        await self._send(dict(command="SOFT_RESET", evse_id=evse_id, flow_id=str(uuid4())), token=True)
        await self._receive("RECEIVED_SOFT_RESET")
        return await self._receive("STATUS_SOFT_RESET", timeout=30)

    ## Synchronous API ##

    def login(self) -> None:
        async def _login(client: BlueCurrentClient) -> None:
            async with client:
                pass

        if self.token is None:
            run(_login(self))

    def get_charge_point_status(self, evse_id: str):
        """
        Get the status of a charge point.

        Returns:
            A dictionary with the status:
            {
                "actual_p1": 0,
                "actual_p2": 0,
                "actual_p3": 0,
                "activity": "available",
                "actual_v1": 0,
                "actual_v2": 0,
                "actual_v3": 0,
                "actual_kwh": 0,
                "max_usage": 20,
                "smartcharging_max_usage": 6,
                "max_offline": 10,
                "offline_since": "",
                "start_datetime": "20230724 15:25:33",
                "stop_datetime": "20230726 07:48:40",
                "total_cost": 9.93,
                "vehicle_status": "A",
                "evse_id": "BCU123456",
            }
        """
        response = get(
            f"{self.api_url}/chargepointstatus?evse_id={evse_id}",
            headers={"Authorization": f"Token {self.token}", "User-Agent": self._user_agent},
        )
        response.raise_for_status()
        return response.json()["data"]

    def get_contracts(self) -> list[dict[str, str]]:
        """
        Get your contracts.

        Returns:
            A list of dictionaries, each representing a contract:
            [
                {
                    "contract_id": "BCU12345678",
                    "contact_email": "your@email.address",
                    "subscription_type": "BASIS",
                    "beneficiary_name": "Your Name",
                    "iban_beneficiary": "NL00ABCD0123456789"
                }
            ]
        """
        response = get(
            f"{self.api_url}/getcontracts",
            headers={"Authorization": f"Token {self.token}", "User-Agent": self._user_agent},
        )
        response.raise_for_status()
        return response.json()["contracts"]

    def get_grids(self) -> list[dict[str, bool | dict[str, str] | str]]:
        """
        Get your grid connections.

        Returns:
            A list of dictionaries, each representing a grid:
            [
                {
                    "address": {"street": "Street Name", "housenumber": "1", "postal_code": "1234AB",
                                "city": "Amsterdam", "country": "NL", "region": ""},
                    "smart_charging": True,
                    "id": "GRID-BCU123456"
                }
            ]
        """
        response = get(
            f"{self.api_url}/getgrids",
            headers={"Authorization": f"Token {self.token}", "User-Agent": self._user_agent},
        )
        response.raise_for_status()
        return response.json()["grids"]

    def get_transactions(
        self, evse_id: str, newest_first: bool = True, page: int = 1
    ) -> dict[str, int | list[dict[str, Any]]]:
        """
        Get a list of transactions.

        Args:
            evse_id: A charge point ID.
            newest_first: If True, start with the most recent transaction. Defaults to True.
            page: Page to get. Defaults to 1.

        Returns:
            A dictionary like this:
            {
                "current_page": 1,
                "next_page": 2,  # This is None when there is no next page.
                "max_per_page": 25,
                "total_pages": 8,
                "transactions: [
                    {
                        "transaction_id": 12345678,
                        "chargepoint_id": "BCU123456",
                        "chargepoint_type": "HIDDEN",
                        "evse_name": "Charge Point Name",
                        "started_at": "01-07-2023 12:34:56",
                        "end_time": "01-07-2023 14:00:00",
                        "kwh": 12.34,
                        "card_id": "NL-ABC-123456-0",
                        "card_name": "Card Name",
                        "total_costs": 5.97,
                        "total_costs_ex_vat": 4.93,
                        "vat": 21,
                        "currency": "EUR"
                    },
                    ...
                ]
            }

        """
        response = post(
            f"{self.api_url}/gettransactions?"
            f"page={page}&"
            f"sort_field_order={'DESC' if newest_first else 'ASC'}&"
            f"sort_field=stoppedtimestamp",
            headers={"Authorization": f"Token {self.token}", "User-Agent": self._user_agent},
            data=dumps({"chargepoints": [{"chargepoint_id": evse_id}]}).encode("UTF-8"),
        )
        response.raise_for_status()
        return response.json()["data"]

    def iterate_transactions(self, evse_id: str, newest_first: bool = True) -> Iterable[dict[str, Any]]:
        """
        Iterate through your transactions.

        Args:
            evse_id: A charge point ID.
            newest_first: If True, start with the most recent transaction. Defaults to True.

        Returns:
            An iterable of dictionaries describing the transactions.
            Each dictionary looks like this:
            {
                "transaction_id": 12345678,
                "chargepoint_id": "BCU123456",
                "chargepoint_type": "HIDDEN",
                "evse_name": "Charge Point Name",
                "started_at": "01-07-2023 12:34:56",
                "end_time": "01-07-2023 14:00:00",
                "kwh": 12.34,
                "card_id": "NL-ABC-123456-0",
                "card_name": "Card Name",
                "total_costs": 5.97,
                "total_costs_ex_vat": 4.93,
                "vat": 21,
                "currency": "EUR"
            }
        """
        next_page = 1
        while next_page is not None:
            transactions = self.get_transactions(evse_id=evse_id, newest_first=newest_first, page=next_page)
            yield from transactions["transactions"]  # type: ignore
            next_page = transactions["next_page"]  # type: ignore

    async def _login(self) -> None:
        await self._send(
            dict(
                command="VALIDATE_PASSWORD",
                username=self.credentials[0],
                password=self._encrypt_password(),
            )
        )
        message = await self._receive("STATUS_PASSWORD")
        if not message.get("accepted"):
            self.logger.error("Authentication failed")
            raise AuthenticationFailed(message)
        self.token = message["token"]
        self.logger.info("Successfully authenticated")

    async def _hello(self) -> None:
        await self._send(dict(command="HELLO"), token=True)
        await self._receive("HELLO")

    def _encrypt_password(self) -> str:
        return dumps(
            {
                key: (value.decode("utf-8") if isinstance(value, bytes) else value)
                for key, value in SJCL().encrypt(self.credentials[1].encode("utf-8"), self.psk).items()
            },
            ensure_ascii=False,
        )

    async def _handler(self) -> None:
        if self.socket is None:
            raise RuntimeError(f"{self.__class__.__name__} is not connected.")
        async for message in self.socket:
            self.logger.debug(f"Received message: {message}")
            await self.queue.put(loads(message))

    @property
    def _user_agent(self) -> str:
        return f"pybluecurrent {__version__.split('+')[0]}"

    async def _receive(self, obj: str, timeout: int = 10) -> dict[str, Any]:
        with self.queue.queue() as q:
            while True:
                message = await wait_for(q.get(), timeout=timeout)
                if message.get("object") == "ERROR":
                    raise BlueCurrentException(message)
                if message.get("object") == obj:
                    return message

    async def _send(self, data: dict[str, Any], token: bool = False):
        if token:
            data.update(dict(Authorization=f"Token {self.token}"))
        if self.socket is None:
            raise RuntimeError(f"{self.__class__.__name__} is not connected.")
        await self.socket.send(dumps(data, ensure_ascii=False))
