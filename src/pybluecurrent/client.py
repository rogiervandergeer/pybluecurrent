from asyncio import Lock, Task, create_task, wait_for
from asyncio import TimeoutError as AsyncTimeoutError
from collections import defaultdict
from contextlib import asynccontextmanager
from datetime import date, datetime
from json import dumps, loads
from logging import getLogger
from typing import Any, AsyncIterable, AsyncIterator
from uuid import uuid4

from asyncio_multisubscriber_queue import MultisubscriberQueue
from httpx import AsyncClient
from sjcl import SJCL
from websockets.asyncio.client import ClientConnection, connect

from pybluecurrent._version import __version__
from pybluecurrent.exceptions import AuthenticationFailed, BlueCurrentException
from pybluecurrent.utilities import parse_datetime_keys, parse_list_datetime_keys


class BlueCurrentClient:
    api_url: str = "https://api.bluecurrent.nl/app/bc_api/api/v2.0"
    psk: str = "d9ab2352a935be4ade182ce4921044f8"
    socket_url: str = "wss://motown.bluecurrent.nl/appserver/2.0"
    http_timeout: float = 30.0

    def __init__(self, username: str | None = None, password: str | None = None, api_token: str | None = None):
        if api_token is None and (username is None or password is None):
            raise ValueError("Provide either username and password, or api_token.")
        self.consumer: Task | None = None
        self.credentials: tuple[str | None, str | None] = (username, password)
        self.api_token: str | None = api_token
        self.customer_id: str | None = None
        self.logger = getLogger("BlueCurrentClient")
        self.httpx_client: AsyncClient | None = None
        self.queue = MultisubscriberQueue()
        self.locks: defaultdict[str, Lock] = defaultdict(Lock)
        self.socket: ClientConnection | None = None
        self.token: str | None = None

    async def __aenter__(self) -> "BlueCurrentClient":
        self.logger.debug("Creating BlueCurrent websocket connection")
        self.connection = connect(self.socket_url, user_agent_header=self._user_agent)
        self.socket = await self.connection.__aenter__()
        self.consumer = create_task(self._handler())
        self.httpx_client = AsyncClient(timeout=self.http_timeout)
        await self.httpx_client.__aenter__()
        if self.token is None:
            await self._login()
        await self._hello()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        self.logger.debug("Closing BlueCurrent connection")
        if self.consumer is not None:
            self.consumer.cancel()
        await self.connection.__aexit__(exc_type, exc_val, exc_tb)
        if self.httpx_client is not None:
            await self.httpx_client.__aexit__(exc_type, exc_val, exc_tb)
        self.consumer, self.socket, self.httpx_client = None, None, None

    async def get_account(self) -> dict[str, bool | datetime | str]:
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
                "first_login_app": datetime(2020, 1, 15, 13, 33, 52),
                "hubspot_user_identity": "a_very_long_string"
            }
        """
        result = await self._request(dict(command="GET_ACCOUNT"), "ACCOUNT")
        del result["object"]
        return parse_datetime_keys(result, formats={"first_login_app": (("%d-%b-%y", "%Y-%m-%dT%H:%M:%S"), False)})

    async def get_api_token(self) -> str:
        """
        Get the API token (home automation key) for your account.

        The token can be used to authenticate instead of a username and password, by constructing
        the client with ``BlueCurrentClient(api_token=...)``.
        """
        if self.httpx_client is None:
            raise RuntimeError(f"{self.__class__.__name__} is not connected.")
        response = await self.httpx_client.get(
            f"{self.api_url}/gethomeautomationkey",
            headers={"Authorization": f"Token {self.token}", "User-Agent": self._user_agent},
        )
        response.raise_for_status()
        return response.json()["key"]

    async def generate_api_token(self) -> str:
        """
        Generate a new API token (home automation key) and return it.

        Warning: this rotates the token. Any previously issued token is invalidated, which will
        break anything still using the old one (for example a Home Assistant integration).
        """
        if self.httpx_client is None:
            raise RuntimeError(f"{self.__class__.__name__} is not connected.")
        response = await self.httpx_client.post(
            f"{self.api_url}/generatehomeautomationkey",
            headers={"Authorization": f"Token {self.token}", "User-Agent": self._user_agent},
        )
        response.raise_for_status()
        return await self.get_api_token()

    async def get_charge_cards(self) -> list[dict[str, date | int | str | None]]:
        """
        Get your charge cards:

        Returns:
            A list of dictionaries, each representing a charge card:
            {
                "uid": "A1B2C3D4E5F6",
                "id": "NL-ABC-123456-0",
                "name": "My Charge Card",
                "customer_name": "Your Name",
                "valid": 1,
                "date_created": date(2023, 6, 27),
                "date_modified": date(2023, 7, 11),
                "date_became_invalid": None
            }
        """
        result = (await self._request(dict(command="GET_CHARGE_CARDS"), "CHARGE_CARDS"))["cards"]
        return parse_list_datetime_keys(
            result,
            formats={
                "date_created": ("%Y-%m-%d", True),
                "date_modified": ("%Y-%m-%d", True),
                "date_became_invalid": ("%Y-%m-%d", True),
            },
        )

    async def get_charge_points(self) -> list[dict[str, bool | dict | str]]:
        """
        Get a list of your charge points.

        Returns:
            A list of dictionaries, each representing a charge point:
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
        return (await self._request(dict(command="GET_CHARGE_POINTS"), "CHARGE_POINTS"))["data"]

    async def get_charge_point_settings(self, evse_id: str) -> dict[str, bool | dict[str, Any] | str]:
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
        return (await self._request(dict(command="GET_CH_SETTINGS", evse_id=evse_id), "CH_SETTINGS"))["data"]

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
        return (await self._request(dict(command="GET_GRID_STATUS", evse_id=evse_id), "GRID_STATUS"))["data"]

    async def get_sessions(self, evse_id: str):
        """Does not work"""
        return await self._request(dict(command="GET_SESSIONS"), "SESSIONS")

    async def get_sustainability_status(self) -> dict[str, float | int]:
        """
        Get statistics on the sustainability of all your charge points.

        Returns:
            A dictionary with two keys:
            {"trees": 1, "co2": 12.345}
        """
        result = await self._request(dict(command="GET_SUSTAINABILITY_STATUS"), "SUSTAINABILITY_STATUS")
        result.pop("object")
        return result

    async def set_plug_and_charge_charge_card(self, evse_id: str, uid: str | None = None) -> None:
        """
        Set a plug-and-charge charge card for the charge point.

        Args:
            evse_id: A charge point ID.
            uid: A charge card UID or None. Defaults to None.
                Setting the plug-and-charge card to None will result in plug-and-charge
                transactions being started without a charge card. Note that the
                charge point status will show "BCU_HOME_USE" as the charge card.
                Setting the plug-and-charge card to "BCU_HOME_USE" has the same effect
                as setting it to None.
        """
        token_uid = "BCU-APP" if uid is None or uid == "BCU_HOME_USE" else uid
        result = await self._request(
            dict(command="SET_PLUG_AND_CHARGE_CHARGE_CARD", evse_id=evse_id, token_uid=token_uid),
            "STATUS_SET_PLUG_AND_CHARGE_CHARGE_CARD",
        )
        if not result.get("success"):
            raise BlueCurrentException(result)

    async def set_status(self, evse_id: str, enabled: bool) -> None:
        """
        Enable or disable a charge point.

        Args:
            evse_id: The ID of the charge point.
            enabled: Boolean that indicates the desired status.
        """
        command = "SET_OPERATIVE" if enabled else "SET_INOPERATIVE"
        flow_id = str(uuid4())
        async with self._command(command):
            await self._send(dict(command=command, evse_id=evse_id, flow_id=flow_id), token=True)
            await self._receive(f"RECEIVED_{command}", flow_id=flow_id)
            await self._receive(f"STATUS_{command}", timeout=30, flow_id=flow_id)

    async def unlock_connector(self, evse_id: str):
        flow_id = str(uuid4())
        async with self._command("UNLOCK_CONNECTOR"):
            await self._send(dict(command="UNLOCK_CONNECTOR", evse_id=evse_id, flow_id=flow_id), token=True)
            await self._receive("RECEIVED_UNLOCK_CONNECTOR", flow_id=flow_id)
            return await self._receive("STATUS_UNLOCK_CONNECTOR", timeout=30, flow_id=flow_id)

    async def soft_reset(self, evse_id: str):
        flow_id = str(uuid4())
        async with self._command("SOFT_RESET"):
            await self._send(dict(command="SOFT_RESET", evse_id=evse_id, flow_id=flow_id), token=True)
            await self._receive("RECEIVED_SOFT_RESET", flow_id=flow_id)
            return await self._receive("STATUS_SOFT_RESET", timeout=30, flow_id=flow_id)

    async def get_charge_point_status(self, evse_id: str) -> dict[str, datetime | float | int | str | None]:
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
                "start_datetime": datetime(2023, 7, 24, 15, 25, 33),
                "stop_datetime": datetime(2023, 7, 26, 7, 48, 40),
                "total_cost": 9.93,
                "vehicle_status": "A",
                "evse_id": "BCU123456",
            }
        """
        if self.httpx_client is None:
            raise RuntimeError(f"{self.__class__.__name__} is not connected.")
        response = await self.httpx_client.get(
            f"{self.api_url}/chargepointstatus?evse_id={evse_id}",
            headers={"Authorization": f"Token {self.token}", "User-Agent": self._user_agent},
        )
        response.raise_for_status()
        result = response.json()["data"]
        return parse_datetime_keys(
            result,
            formats={
                "start_datetime": ("%Y%m%d %H:%M:%S", False),
                "stop_datetime": ("%Y%m%d %H:%M:%S", False),
            },
        )

    async def get_contracts(self) -> list[dict[str, str]]:
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
        if self.httpx_client is None:
            raise RuntimeError(f"{self.__class__.__name__} is not connected.")
        response = await self.httpx_client.get(
            f"{self.api_url}/getcontracts",
            headers={"Authorization": f"Token {self.token}", "User-Agent": self._user_agent},
        )
        response.raise_for_status()
        return response.json()["contracts"]

    async def get_grids(self) -> list[dict[str, bool | dict[str, str] | str]]:
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
        if self.httpx_client is None:
            raise RuntimeError(f"{self.__class__.__name__} is not connected.")
        response = await self.httpx_client.get(
            f"{self.api_url}/getgrids",
            headers={"Authorization": f"Token {self.token}", "User-Agent": self._user_agent},
        )
        response.raise_for_status()
        return response.json()["grids"]

    async def get_transactions(self, evse_id: str, newest_first: bool = True, page: int = 1) -> dict[str, Any]:
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
                        "started_at": datetime(2023, 7, 1, 12, 34, 56),
                        "end_time": datetime(2023, 7, 1, 14, 0, 0),
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
        if self.httpx_client is None:
            raise RuntimeError(f"{self.__class__.__name__} is not connected.")
        response = await self.httpx_client.post(
            f"{self.api_url}/gettransactions?"
            f"page={page}&"
            f"sort_field_order={'DESC' if newest_first else 'ASC'}&"
            f"sort_field=stoppedtimestamp",
            headers={"Authorization": f"Token {self.token}", "User-Agent": self._user_agent},
            content=dumps({"chargepoints": [{"chargepoint_id": evse_id}]}),
        )
        response.raise_for_status()
        result = response.json()["data"]
        result["transactions"] = parse_list_datetime_keys(
            result["transactions"],
            formats={"started_at": ("%d-%m-%Y %H:%M:%S", False), "end_time": ("%d-%m-%Y %H:%M:%S", False)},
        )
        return result

    async def iterate_transactions(self, evse_id: str, newest_first: bool = True) -> AsyncIterable[dict[str, Any]]:
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
                "started_at": datetime(2023, 7, 1, 12, 34, 56),
                "end_time": datetime(2023, 7, 1, 14, 0, 0),
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
            transactions = await self.get_transactions(evse_id=evse_id, newest_first=newest_first, page=next_page)
            for tx in transactions["transactions"]:
                yield tx
            next_page = transactions["next_page"]

    async def _login(self) -> None:
        if self.api_token is not None:
            await self._login_with_token()
        else:
            await self._login_with_password()

    async def _login_with_password(self) -> None:
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

    async def _login_with_token(self) -> None:
        await self._send(dict(command="VALIDATE_API_TOKEN", token=self.api_token))
        message = await self._receive("STATUS_API_TOKEN")
        if not message.get("success"):
            self.logger.error("Authentication failed")
            raise AuthenticationFailed(message)
        self.token = message["token"]
        self.customer_id = message.get("customer_id")
        self.logger.info("Successfully authenticated")

    async def _hello(self) -> None:
        await self._send(dict(command="HELLO"), token=True)
        await self._receive("HELLO")

    def _encrypt_password(self) -> str:
        password = self.credentials[1]
        if password is None:
            raise RuntimeError("No password configured.")
        return dumps(
            {
                key: (value.decode("utf-8") if isinstance(value, bytes) else value)
                for key, value in SJCL().encrypt(password.encode("utf-8"), self.psk).items()
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

    async def _receive(self, obj: str, timeout: int = 10, flow_id: str | None = None) -> dict[str, Any]:
        with self.queue.queue() as q:
            while True:
                try:
                    message = await wait_for(q.get(), timeout=timeout)
                except AsyncTimeoutError as exc:
                    # On Python 3.10 asyncio.TimeoutError is a distinct class from the
                    # builtin TimeoutError; normalise so callers can catch the builtin.
                    raise TimeoutError from exc
                if message.get("object") == "ERROR":
                    # Attribute errors by flow_id when the backend echoes one, so a correlated
                    # error doesn't poison other concurrent calls. Not every error carries a
                    # flow_id (e.g. "forbidden" has none), so an uncorrelated error still raises
                    # for whoever is waiting — falling back to the old broadcast behaviour.
                    error_flow_id = message.get("flow_id")
                    if error_flow_id in (flow_id, None):
                        raise BlueCurrentException(message)
                    continue
                if message.get("object") == obj:
                    return message

    async def _send(self, data: dict[str, Any], token: bool = False):
        if token:
            data.update(dict(Authorization=f"Token {self.token}"))
        if self.socket is None:
            raise RuntimeError(f"{self.__class__.__name__} is not connected.")
        await self.socket.send(dumps(data, ensure_ascii=False))

    @asynccontextmanager
    async def _command(self, key: str) -> AsyncIterator[None]:
        # Serialise calls that await the same response object so concurrent same-type calls
        # can't consume each other's replies. Different keys keep running concurrently, so a
        # slow command (e.g. soft_reset) doesn't block a quick read.
        async with self.locks[key]:
            yield

    async def _request(self, data: dict[str, Any], response_object: str, timeout: int = 10) -> dict[str, Any]:
        async with self._command(response_object):
            await self._send(data, token=True)
            return await self._receive(response_object, timeout=timeout)
