from asyncio import CancelledError, Event, Lock, Task, create_task, get_running_loop, sleep, wait, wait_for
from asyncio import TimeoutError as AsyncTimeoutError
from collections import defaultdict, deque
from contextlib import asynccontextmanager, suppress
from datetime import date, time
from json import JSONDecodeError, dumps, loads
from logging import getLogger
from random import uniform
from typing import Any, AsyncIterable, AsyncIterator, Iterable, cast
from uuid import uuid4

from asyncio_multisubscriber_queue import MultisubscriberQueue
from httpx import AsyncClient
from sjcl import SJCL
from websockets.asyncio.client import ClientConnection, connect
from websockets.exceptions import ConnectionClosed

from pybluecurrent._version import __version__
from pybluecurrent.enums import Weekday
from pybluecurrent.exceptions import (
    AuthenticationFailed,
    BlueCurrentException,
    ConnectionLost,
    RequestTimeout,
    _GiveUp,
)
from pybluecurrent.models import (
    Account,
    ChargeCard,
    ChargePoint,
    ChargePointSettings,
    ChargePointStatus,
    Contract,
    Grid,
    GridStatus,
    SustainabilityStatus,
    Transaction,
    TransactionsPage,
)
from pybluecurrent.utilities import (
    format_date,
    format_time,
    parse_datetime_keys,
    parse_list_datetime_keys,
    parse_time,
    rename_key,
)

logger = getLogger(__name__)


# The plug-and-charge card reads back with this sentinel uid when no card is configured ("home use").
_NO_CARD_UID = "BCU_HOME_USE"


def _normalize_charge_point(data: dict[str, Any]) -> None:
    """Normalize a charge-point or settings response in place.

    - Rewrite the smart-charging profile read keys to the canonical setter names (``days``,
      ``expected_departure_time``), since the backend reads them back under different keys.
    - Surface the plug-and-charge card as ``None`` when none is configured, rather than the
      backend's no-card sentinel card object.
    """
    delayed = data.get("delayed_charging")
    if isinstance(delayed, dict):
        rename_key(delayed, "selected_days", "days")
        for key in ("start_time", "end_time"):
            if isinstance(delayed.get(key), str):
                delayed[key] = parse_time(delayed[key])
    price_based = data.get("price_based_charging")
    if isinstance(price_based, dict):
        rename_key(price_based, "expected_leave_time", "expected_departure_time")
        if isinstance(price_based.get("expected_departure_time"), str):
            price_based["expected_departure_time"] = parse_time(price_based["expected_departure_time"])
    card = data.get("plug_and_charge_charge_card")
    if isinstance(card, dict) and card.get("uid") == _NO_CARD_UID:
        data["plug_and_charge_charge_card"] = None


# Identity sentinel broadcast on the queue when the receive handler exits, so in-flight _receive
# waiters wake immediately instead of blocking until their own deadline.
_CONNECTION_CLOSED = object()

# Frame fields masked before a received message is logged, so a token never reaches the logs.
_SENSITIVE_KEYS = ("token", "Authorization")


def _redact(message):
    """Return the message with any sensitive fields masked, for safe logging."""
    if isinstance(message, dict) and any(key in message for key in _SENSITIVE_KEYS):
        return {key: ("***" if key in _SENSITIVE_KEYS else value) for key, value in message.items()}
    return message


class BlueCurrentClient:
    _api_base: str = "https://api.bluecurrent.nl/app/bc_api/api"
    api_url: str = f"{_api_base}/v2.0"
    # The status endpoint is the only one served at v2.1, where it becomes multi-socket aware.
    status_api_url: str = f"{_api_base}/v2.1"
    psk: str = "d9ab2352a935be4ade182ce4921044f8"
    socket_url: str = "wss://motown.bluecurrent.nl/appserver/2.0"
    http_timeout: float = 30.0
    # Two-phase commands (set_status, unlock_connector, soft_reset) await a STATUS_ verdict that the
    # backend renders behind its own ~30s ceiling — the answer lands just after 30s, so wait longer.
    command_timeout: int = 60
    # Auto-reconnect: a supervisor reconnects after drops with exponential backoff, reusing the token.
    auto_reconnect: bool = True
    reconnect_initial_backoff: float = 1.0
    reconnect_max_backoff: float = 60.0
    reconnect_backoff_multiplier: float = 2.0
    reconnect_stable_period: float = 30.0  # uptime before the backoff resets to initial
    reconnect_wait_timeout: float = 120.0  # max a call blocks waiting for a reconnect
    # Stop reconnecting after this many logins within the window (a reconnect normally reuses the token).
    reconnect_relogin_window: float = 300.0
    reconnect_max_relogins: int = 3

    def __init__(self, username: str | None = None, password: str | None = None, api_token: str | None = None):
        if api_token is None and (username is None or password is None):
            raise ValueError("Provide either username and password, or api_token.")
        self.connection = None
        self.consumer: Task | None = None
        self.credentials: tuple[str | None, str | None] = (username, password)
        self.api_token: str | None = api_token
        self.customer_id: str | None = None
        self.httpx_client: AsyncClient | None = None
        self.queue = MultisubscriberQueue()
        self.locks: defaultdict[str, Lock] = defaultdict(Lock)
        self.socket: ClientConnection | None = None
        self.token: str | None = None
        # Terminal give-up reason (bad credentials, gave up reconnecting, shutting down); latches calls.
        self._closed: BlueCurrentException | None = None
        # Reason the CURRENT connection dropped; transient, cleared before each reconnect attempt.
        self._drop_reason: BlueCurrentException | None = None
        # Reconnect supervisor state.
        self._connected: Event = Event()
        self._supervisor: Task | None = None
        self._shutting_down: bool = False
        self._backoff: float = self.reconnect_initial_backoff
        self._last_connect: float = 0.0
        self._login_times: deque[float] = deque()

    async def __aenter__(self) -> "BlueCurrentClient":
        logger.debug("Creating BlueCurrent websocket connection")
        self._closed = None
        self._drop_reason = None
        self._shutting_down = False
        self._connected = Event()
        self._backoff = self.reconnect_initial_backoff
        self._last_connect = 0.0
        self._login_times = deque()
        try:
            # Connect inline so a first-connect failure (bad creds, hello timeout) surfaces directly.
            await self._connect()
        except BaseException:
            await self._teardown()  # __aexit__ won't run when __aenter__ raises; don't leak the transport
            raise
        self._connected.set()
        if self.auto_reconnect:
            self._supervisor = create_task(self._supervise())
            self._supervisor.add_done_callback(self._on_supervisor_done)
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        logger.debug("Closing BlueCurrent connection")
        self._shutting_down = True
        self._closed = self._closed or ConnectionLost("Client is shutting down.")
        self._connected.set()  # release any gated waiter so it sees _closed
        if self._supervisor is not None:
            # Stop the supervisor first so it can't race a reconnect against the teardown below.
            self._supervisor.cancel()
            with suppress(CancelledError):
                await self._supervisor
            self._supervisor = None
        await self._teardown(exc_type, exc_val, exc_tb)

    def _now(self) -> float:
        """The running loop's clock, behind a seam so tests can drive reconnect timing."""
        return get_running_loop().time()

    async def _connect(self) -> None:
        """Build and authenticate one connection, reusing the cached token when present.

        Raises on failure, leaving partial state for the caller to tear down.
        """
        self.connection = connect(self.socket_url, user_agent_header=self._user_agent)
        self.socket = await self.connection.__aenter__()
        self.consumer = create_task(self._handler())
        self.consumer.add_done_callback(self._on_handler_done)
        if self.httpx_client is None:
            self.httpx_client = AsyncClient(timeout=self.http_timeout)
            await self.httpx_client.__aenter__()
        if self.token is None:
            self._note_login_attempt()
            await self._login()
        await self._hello()
        self._last_connect = self._now()

    async def _teardown_transport(self, exc_type=None, exc_val=None, exc_tb=None) -> None:
        """Close the websocket transport (handler + socket) but keep the httpx client so REST keeps working."""
        if self.consumer is not None:
            self.consumer.cancel()  # stop the handler and await it so its exception is retrieved
            with suppress(CancelledError):
                await self.consumer
        if self.connection is not None:
            try:
                await self.connection.__aexit__(exc_type, exc_val, exc_tb)
            except Exception:
                logger.debug("Error closing websocket connection", exc_info=True)
        self.consumer = self.socket = self.connection = None

    async def _teardown(self, exc_type=None, exc_val=None, exc_tb=None) -> None:
        """Full teardown: the websocket transport plus the httpx client."""
        await self._teardown_transport(exc_type, exc_val, exc_tb)
        if self.httpx_client is not None:
            try:
                await self.httpx_client.__aexit__(exc_type, exc_val, exc_tb)
            except Exception:
                logger.debug("Error closing httpx client", exc_info=True)
        self.httpx_client = None

    def _on_handler_done(self, task: Task) -> None:
        """Log an unexpected exit of the handler."""
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None:
            logger.error("Websocket receive handler exited", exc_info=exc)

    def _on_supervisor_done(self, task: Task) -> None:
        """Log an unexpected exit of the reconnect supervisor."""
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None:
            logger.error("Reconnect supervisor exited unexpectedly", exc_info=exc)

    def _note_login_attempt(self) -> None:
        """Record a login attempt; give up before one would exceed ``reconnect_max_relogins`` in-window."""
        now = self._now()
        window = self.reconnect_relogin_window
        while self._login_times and now - self._login_times[0] > window:
            self._login_times.popleft()
        if len(self._login_times) >= self.reconnect_max_relogins:
            raise _GiveUp(ConnectionLost("Reconnect abandoned."))
        self._login_times.append(now)

    async def _supervise(self) -> None:
        """Own the connection for the client's lifetime: reconnect after drops until told to stop."""
        try:
            while not self._shutting_down:
                await self._wait_for_drop()
                if self._shutting_down:
                    break
                logger.warning("Websocket connection dropped; reconnecting")
                self._connected.clear()  # gate new calls before tearing down the dead transport
                await self._teardown_transport()
                if self._now() - self._last_connect >= self.reconnect_stable_period:
                    self._backoff = self.reconnect_initial_backoff  # stable uptime → reset (flap guard)
                try:
                    await self._reconnect_with_backoff()
                except _GiveUp as give_up:
                    await self._teardown_transport()  # release the giving-up attempt's partial transport
                    logger.error("Reconnect abandoned: %s", give_up.reason)
                    self._closed = give_up.reason
                    self._connected.set()  # wake blocked waiters so they immediately re-raise _closed
                    return
                logger.info("Reconnected")
                self._connected.set()
        finally:
            self._connected.set()  # never leave a waiter blocked forever

    async def _wait_for_drop(self) -> None:
        """Block until the current handler task finishes (the connection dropped)."""
        if self.consumer is None:
            return
        # wait(), not `await self.consumer`: don't re-raise the handler's error or eat our own cancel.
        await wait({self.consumer})

    def _grow_backoff(self) -> None:
        self._backoff = min(self._backoff * self.reconnect_backoff_multiplier, self.reconnect_max_backoff)

    async def _reconnect_with_backoff(self) -> None:
        """Retry ``_connect`` with exponential backoff until connected, given up, or shutting down."""
        while not self._shutting_down:
            # Clear each attempt: the handshake's _send/_receive fast-fail on a stale _drop_reason.
            self._drop_reason = None
            delay = self._backoff * uniform(0.5, 1.0)  # jitter to avoid synchronised retries
            logger.debug("Reconnecting in %.1fs", delay)
            await sleep(delay)
            if self._shutting_down:
                return
            try:
                await self._connect()
            except AuthenticationFailed as exc:
                raise _GiveUp(exc)  # bad credentials are permanent — never retry
            except _GiveUp:
                raise  # gave up (from _note_login_attempt)
            except (OSError, ConnectionClosed, ConnectionLost) as exc:
                logger.debug("Reconnect attempt failed: %r", exc)
                await self._teardown_transport()  # transport failure: keep the token and retry
                self._grow_backoff()
            except (RequestTimeout, BlueCurrentException) as exc:
                # auth/hello rejected: the token may be stale, so log in again next attempt
                logger.debug("Reconnect attempt failed: %r", exc)
                self.token = None
                await self._teardown_transport()
                if isinstance(exc, RequestTimeout):
                    self._backoff = self.reconnect_max_backoff  # a timed-out login backs off hardest
                else:
                    self._grow_backoff()
            else:
                return

    async def _await_connected(self) -> None:
        """Wait for the connection before a call (bounded by ``reconnect_wait_timeout``), or fail fast.

        In-flight calls are unaffected — they fail via _send/_receive.
        """
        if self._closed is not None:
            raise self._closed
        if self._connected.is_set():
            return
        if not self.auto_reconnect:
            raise self._drop_reason or ConnectionLost("The websocket connection was closed.")
        try:
            await wait_for(self._connected.wait(), timeout=self.reconnect_wait_timeout)
        except AsyncTimeoutError as exc:
            logger.warning("Timed out waiting for reconnection")
            raise ConnectionLost("Timed out waiting for reconnection.") from exc
        # The supervisor may have set _connected to wake us *and* set _closed (give-up); re-check.
        if self._closed is not None:
            raise self._closed

    async def get_account(self) -> Account:
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
        parse_datetime_keys(result, formats={"first_login_app": (("%d-%b-%y", "%Y-%m-%dT%H:%M:%S"), False)})
        return cast(Account, result)

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

    async def get_charge_cards(self) -> list[ChargeCard]:
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
        parse_list_datetime_keys(
            result,
            formats={
                "date_created": ("%Y-%m-%d", True),
                "date_modified": ("%Y-%m-%d", True),
                "date_became_invalid": ("%Y-%m-%d", True),
            },
        )
        return cast(list[ChargeCard], result)

    async def get_charge_points(self) -> list[ChargePoint]:
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
                "plug_and_charge_charge_card": {"uid": "A1B2C3D4E5F6", "id": "NL-ABC-123456-0",
                                                "name": "Your Card", "customer_name": "Your Name", "valid": 1},
                "tariff":  {"tariff_id","NLBCUT58", "price_ex_vat": 0.2, "start_price_ex_vat": 0, "price_in_vat": 0.242,
                            "start_price_in_vat": 0, "currency": "EUR", "vat_percentage": 21},
                "plug_and_charge_notification": {"value": False, "permission": "write"},
                "plug_and_charge": {"value": True, "permission": "write"},
                "led_interaction": {"value": False, "permission": "read"},
                "publish_location": {"value": False, "permission": "write"},
                "smart_charging": True,
                "smart_charging_dynamic": True,
                "activity": "available",
                "location": {"x_coord": 50.1234, "y_coord": 5.01234, "street": "Europalaan", "housenumber": "100",
                             "zipcode": "3526KS", "city": "Utrecht", "country": "NL"},
                "delayed_charging": {"value": False, "permission": "write", "start_time": "23:00",
                                     "end_time": "07:00", "days": [1, 2, 3, 4, 5]},
                "price_based_charging": {"value": False, "permission": "write"}
            }
        """
        data = (await self._request(dict(command="GET_CHARGE_POINTS"), "CHARGE_POINTS"))["data"]
        for charge_point in data:
            _normalize_charge_point(charge_point)
        return data

    async def get_charge_point_settings(self, evse_id: str) -> ChargePointSettings:
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
                "plug_and_charge_charge_card": {"uid": "A1B2C3D4E5F6", "id": "NL-ABC-123456-0",
                                                "name": "Your Card", "customer_name": "Your Name", "valid": 1},
                "smart_charging": True,
                "smart_charging_dynamic": True,
                "model_type": "H:MOVE-C32T2",
                "is_cable": True,
                "chargepoint_type": "HIDDEN",
                "plug_and_charge_notification": {"value": False, "permission": "write"},
                "led_intensity": {"value": 0, "permission": "none"},
                "led_interaction": {"value": False, "permission": "none"},
                "delayed_charging": {"value": False, "permission": "write", "start_time": "23:00",
                                     "end_time": "07:00", "days": [1, 2, 3, 4, 5]},
                "price_based_charging": {"value": True, "permission": "write", "expected_departure_time": "07:00",
                                         "expected_kwh": 25, "minimum_kwh": 10}
            }
        """
        data = (await self._request(dict(command="GET_CH_SETTINGS", evse_id=evse_id), "CH_SETTINGS"))["data"]
        _normalize_charge_point(data)
        return data

    async def get_grid_status(self, evse_id: str) -> GridStatus:
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

    async def get_sustainability_status(self) -> SustainabilityStatus:
        """
        Get statistics on the sustainability of all your charge points.

        Returns:
            A dictionary with two keys:
            {"trees": 1, "co2": 12.345}
        """
        result = await self._request(dict(command="GET_SUSTAINABILITY_STATUS"), "SUSTAINABILITY_STATUS")
        result.pop("object")
        return cast(SustainabilityStatus, result)

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
            await self._await_connected()
            await self._send(dict(command=command, evse_id=evse_id, flow_id=flow_id), token=True)
            await self._receive(f"RECEIVED_{command}", flow_id=flow_id)
            status = await self._receive(f"STATUS_{command}", timeout=self.command_timeout, flow_id=flow_id)
            if not status.get("success"):
                raise BlueCurrentException(status)

    async def unlock_connector(self, evse_id: str) -> None:
        """Unlock the connector of a charge point. Raises ``BlueCurrentException`` if it fails."""
        flow_id = str(uuid4())
        async with self._command("UNLOCK_CONNECTOR"):
            await self._await_connected()
            await self._send(dict(command="UNLOCK_CONNECTOR", evse_id=evse_id, flow_id=flow_id), token=True)
            await self._receive("RECEIVED_UNLOCK_CONNECTOR", flow_id=flow_id)
            status = await self._receive("STATUS_UNLOCK_CONNECTOR", timeout=self.command_timeout, flow_id=flow_id)
            if not status.get("success"):
                raise BlueCurrentException(status)

    async def soft_reset(self, evse_id: str) -> None:
        """Soft-reset a charge point. Raises ``BlueCurrentException`` if it fails."""
        flow_id = str(uuid4())
        async with self._command("SOFT_RESET"):
            await self._await_connected()
            await self._send(dict(command="SOFT_RESET", evse_id=evse_id, flow_id=flow_id), token=True)
            await self._receive("RECEIVED_SOFT_RESET", flow_id=flow_id)
            status = await self._receive("STATUS_SOFT_RESET", timeout=self.command_timeout, flow_id=flow_id)
            if not status.get("success"):
                raise BlueCurrentException(status)

    async def get_charge_point_statuses(self, evse_id: str) -> list[ChargePointStatus]:
        """
        Get the status of every socket of a charge point.

        Most charge points have a single socket, so the list has one entry. Dual-socket models
        (such as the NanoXL) return one entry per socket, each tagged with its ``socket_id``. Use
        get_charge_point_status to fetch a single socket.

        Args:
            evse_id: A charge point ID.

        Returns:
            A list of dictionaries, each the status of one socket:
            [
                {
                    "actual_p1": 0,
                    "actual_p2": 0,
                    "actual_p3": 0,
                    "activity": "available",
                    "actual_v1": 0,
                    "actual_v2": 0,
                    "actual_v3": 0,
                    "actual_kwh": 0,
                    "boosting": False,
                    "max_usage": 20,
                    "smartcharging_max_usage": 6,
                    "max_offline": 10,
                    "offline_since": "",
                    "start_datetime": datetime(2023, 7, 24, 15, 25, 33),
                    "stop_datetime": datetime(2023, 7, 26, 7, 48, 40),
                    "total_cost": 9.93,
                    "vehicle_status": "A",
                    "evse_id": "BCU123456",
                    "socket_id": 1,
                }
            ]
        """
        if self.httpx_client is None:
            raise RuntimeError(f"{self.__class__.__name__} is not connected.")
        response = await self.httpx_client.get(
            f"{self.status_api_url}/chargepointstatus?evse_id={evse_id}",
            headers={"Authorization": f"Token {self.token}", "User-Agent": self._user_agent},
        )
        response.raise_for_status()
        items = response.json()["items"]
        for item in items:
            parse_datetime_keys(
                item,
                formats={
                    "start_datetime": ("%Y%m%d %H:%M:%S", False),
                    "stop_datetime": ("%Y%m%d %H:%M:%S", False),
                },
            )
        return cast(list[ChargePointStatus], items)

    async def get_charge_point_status(self, evse_id: str, socket_id: int = 1) -> ChargePointStatus:
        """
        Get the status of a single socket of a charge point.

        Most charge points have a single socket, numbered 1, so the default returns it. Dual-socket
        models (such as the NanoXL) have a socket per side: pass the socket_id you want, or use
        get_charge_point_statuses to fetch them all.

        Args:
            evse_id: A charge point ID.
            socket_id: The socket to fetch. Defaults to 1.

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
                "boosting": False,
                "max_usage": 20,
                "smartcharging_max_usage": 6,
                "max_offline": 10,
                "offline_since": "",
                "start_datetime": datetime(2023, 7, 24, 15, 25, 33),
                "stop_datetime": datetime(2023, 7, 26, 7, 48, 40),
                "total_cost": 9.93,
                "vehicle_status": "A",
                "evse_id": "BCU123456",
                "socket_id": 1,
            }

        Raises:
            ValueError: If the charge point has no socket with the given socket_id.
        """
        statuses = await self.get_charge_point_statuses(evse_id)
        for status in statuses:
            if status["socket_id"] == socket_id:
                return status
        sockets = sorted(status["socket_id"] for status in statuses)
        raise ValueError(f"Charge point {evse_id} has sockets {sockets}; no socket {socket_id}.")

    async def set_delayed_charging(self, evse_id: str, enabled: bool) -> None:
        """
        Enable or disable delayed charging for a charge point.

        While enabled, the charge point only charges within the window configured with
        set_delayed_charging_schedule. Enabling delayed charging disables any other smart
        charging profile, as a charge point has at most one profile active.

        Args:
            evse_id: A charge point ID.
            enabled: Boolean that indicates whether delayed charging should be enabled.
        """
        await self._post("setdelayedcharging", dict(evse_id=evse_id, value=enabled))

    async def set_delayed_charging_schedule(
        self,
        evse_id: str,
        start_time: time | str,
        end_time: time | str,
        days: Iterable[Weekday | int | str],
    ) -> None:
        """
        Set the schedule of the delayed charging profile of a charge point.

        The charge point charges between start_time and end_time on the selected days, and
        delays charging outside of that window. A window may span midnight. The schedule is
        only applied while delayed charging is enabled with set_delayed_charging.

        Args:
            evse_id: A charge point ID.
            start_time: The time at which charging may start, as a time or a "HH:MM" string.
            end_time: The time at which charging must stop, as a time or a "HH:MM" string.
            days: The days on which the schedule applies. Each day may be a Weekday, an
                isoweekday number (1 for Monday through 7 for Sunday), or a weekday name
                such as "monday" or "mo".
        """
        selected_days = sorted({int(Weekday(day)) for day in days})
        if not selected_days:
            raise ValueError("Select at least one day.")
        await self._post(
            "savescheduledelayedcharging",
            dict(
                evse_id=evse_id,
                start_time=format_time(start_time),
                end_time=format_time(end_time),
                # Days must be a compact JSON string (no whitespace), which is what the backend expects.
                days=dumps(selected_days, separators=(",", ":")),
            ),
        )

    async def set_price_based_charging(self, evse_id: str, enabled: bool) -> None:
        """
        Enable or disable price-based charging for a charge point.

        While enabled, the charge point charges during the cheapest hours before the expected
        departure time, as configured with set_price_based_charging_settings. Enabling price-based
        charging disables any other smart charging profile, as a charge point has at most one
        profile active.

        Args:
            evse_id: A charge point ID.
            enabled: Boolean that indicates whether price-based charging should be enabled.
        """
        await self._post("setpricebasedcharging", dict(evse_id=evse_id, value=enabled))

    async def set_price_based_charging_settings(
        self,
        evse_id: str,
        expected_departure_time: time | str,
        expected_kwh: float,
        minimum_kwh: float,
    ) -> None:
        """
        Set the settings of the price-based charging profile of a charge point.

        These settings are only applied while price-based charging is enabled with
        set_price_based_charging.

        Args:
            evse_id: A charge point ID.
            expected_departure_time: The time the vehicle is expected to leave, as a time or a
                "HH:MM" string. Read back under the same key from the price_based_charging settings.
            expected_kwh: The amount of energy, in kWh, expected to be charged before departure.
            minimum_kwh: The amount of energy, in kWh, to charge immediately regardless of price.
        """
        await self._post(
            "setpricebasedsettings",
            dict(
                evse_id=evse_id,
                expected_departure_time=format_time(expected_departure_time),
                expected_kwh=expected_kwh,
                minimum_kwh=minimum_kwh,
            ),
        )

    async def boost(self, evse_id: str) -> None:
        """
        Charge now, overriding the active smart charging profile of a charge point.

        Overrides whichever profile is currently delaying charging: delayed charging or
        price-based charging. The override applies to the ongoing session only, and cannot be
        undone. While it is active, get_charge_point_status reports "boosting": True.

        Args:
            evse_id: A charge point ID.

        Raises:
            ValueError: If no smart charging profile is active, so there is nothing to override.
        """
        settings = await self.get_charge_point_settings(evse_id)
        if settings["price_based_charging"]["value"]:
            await self._post("overridechargingprofiles", dict(boost=True, evse_id=evse_id))
        elif settings["delayed_charging"]["value"]:
            await self._post("overridedelayedchargingtimeout", dict(evse_id=evse_id))
        else:
            raise ValueError(f"No active smart charging profile to boost for {evse_id}.")

    async def get_contracts(self) -> list[Contract]:
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

    async def get_grids(self) -> list[Grid]:
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

    async def get_transactions(
        self,
        evse_id: str,
        newest_first: bool = True,
        page: int = 1,
        start_date: date | None = None,
        end_date: date | None = None,
    ) -> TransactionsPage:
        """
        Get a list of transactions.

        Args:
            evse_id: A charge point ID.
            newest_first: If True, start with the most recent transaction. Defaults to True.
            page: Page to get. Defaults to 1.
            start_date: Only return transactions from this date onwards. Omitted by default.
            end_date: Only return transactions up to this date. Omitted by default.

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
        query = [
            f"page={page}",
            f"sort_field_order={'DESC' if newest_first else 'ASC'}",
            "sort_field=stoppedtimestamp",
        ]
        if start_date is not None:
            query.append(f"start_date={format_date(start_date)}")
        if end_date is not None:
            query.append(f"end_date={format_date(end_date)}")
        response = await self.httpx_client.post(
            f"{self.api_url}/gettransactions?" + "&".join(query),
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

    async def iterate_transactions(
        self,
        evse_id: str,
        newest_first: bool = True,
        start_date: date | None = None,
        end_date: date | None = None,
    ) -> AsyncIterable[Transaction]:
        """
        Iterate through your transactions.

        Args:
            evse_id: A charge point ID.
            newest_first: If True, start with the most recent transaction. Defaults to True.
            start_date: Only return transactions from this date onwards. Omitted by default.
            end_date: Only return transactions up to this date. Omitted by default.

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
            transactions = await self.get_transactions(
                evse_id=evse_id,
                newest_first=newest_first,
                page=next_page,
                start_date=start_date,
                end_date=end_date,
            )
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
            logger.error("Authentication failed")
            raise AuthenticationFailed(message)
        self.token = message["token"]
        logger.info("Successfully authenticated")

    async def _login_with_token(self) -> None:
        await self._send(dict(command="VALIDATE_API_TOKEN", token=self.api_token))
        message = await self._receive("STATUS_API_TOKEN")
        if not message.get("success"):
            logger.error("Authentication failed")
            raise AuthenticationFailed(message)
        self.token = message["token"]
        self.customer_id = message.get("customer_id")
        logger.info("Successfully authenticated")

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
        terminal: BlueCurrentException = ConnectionLost("The websocket connection was closed.")
        try:
            async for message in self.socket:
                try:
                    decoded = loads(message)
                except JSONDecodeError:
                    # A single malformed frame is a transient wire artifact; log it and keep going.
                    logger.warning("Discarding malformed (non-JSON) frame: %r", message)
                    continue
                logger.debug("Received message: %s", _redact(decoded))
                await self.queue.put(decoded)
        except Exception as exc:
            terminal = ConnectionLost("The websocket connection failed.")
            terminal.__cause__ = exc
            raise
        finally:
            # Set the drop reason synchronously (survives cancellation of the queue.put) and wake waiters.
            self._drop_reason = terminal
            await self.queue.put(_CONNECTION_CLOSED)

    @property
    def _user_agent(self) -> str:
        return f"pybluecurrent {__version__.split('+')[0]}"

    async def _receive(self, obj: str, timeout: int = 10, flow_id: str | None = None) -> dict[str, Any]:
        terminal = self._closed or self._drop_reason
        if terminal is not None:
            raise terminal  # dead client or dropped connection: fail rather than block for a reply
        loop = get_running_loop()
        deadline = loop.time() + timeout
        with self.queue.queue() as q:
            while True:
                # One deadline for the whole call, so a stream of non-matching frames can't re-arm it.
                remaining = deadline - loop.time()
                if remaining <= 0:
                    raise RequestTimeout(f"No {obj} received within {timeout}s.")
                try:
                    message = await wait_for(q.get(), timeout=remaining)
                except AsyncTimeoutError as exc:
                    # RequestTimeout subclasses TimeoutError so ``except TimeoutError`` still works
                    # (and normalises py3.10, where asyncio.TimeoutError is a distinct class).
                    raise RequestTimeout(f"No {obj} received within {timeout}s.") from exc
                if message is _CONNECTION_CLOSED:
                    # Identity check before .get(): the sentinel is not a dict.
                    raise self._closed or self._drop_reason or ConnectionLost("The websocket connection was closed.")
                if message.get("object") == "ERROR":
                    # Route errors by flow_id so a correlated one doesn't poison other calls; an
                    # uncorrelated error (no flow_id, e.g. "forbidden") still raises for the waiter.
                    error_flow_id = message.get("flow_id")
                    if error_flow_id in (flow_id, None):
                        raise BlueCurrentException(message)
                    continue
                if message.get("object") == obj:
                    return message

    async def _send(self, data: dict[str, Any], token: bool = False):
        terminal = self._closed or self._drop_reason
        if terminal is not None:
            raise terminal
        if token:
            data.update(dict(Authorization=f"Token {self.token}"))
        if self.socket is None:
            raise RuntimeError(f"{self.__class__.__name__} is not connected.")
        try:
            await self.socket.send(dumps(data, ensure_ascii=False))
        except ConnectionClosed as exc:
            # Dropped between the readiness gate and the send; surface as ConnectionLost.
            raise ConnectionLost("The websocket connection was closed.") from exc

    @asynccontextmanager
    async def _command(self, key: str) -> AsyncIterator[None]:
        # Serialise same-key calls so they can't consume each other's replies; different keys run
        # concurrently, so a slow command (e.g. soft_reset) doesn't block a quick read.
        async with self.locks[key]:
            yield

    async def _request(self, data: dict[str, Any], response_object: str, timeout: int = 10) -> dict[str, Any]:
        async with self._command(response_object):
            await self._await_connected()
            await self._send(data, token=True)
            return await self._receive(response_object, timeout=timeout)

    async def _post(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        """Post a command to the REST API.

        A rejected command comes back as an HTTP error carrying the same {"object": "ERROR", ...}
        body that the websocket sends, so it is raised as a BlueCurrentException as well.
        """
        if self.httpx_client is None:
            raise RuntimeError(f"{self.__class__.__name__} is not connected.")
        response = await self.httpx_client.post(
            f"{self.api_url}/{path}",
            headers={"Authorization": f"Token {self.token}", "User-Agent": self._user_agent},
            json=body,  # send as JSON (sets Content-Type)
        )
        if not response.is_success:
            if response.headers.get("content-type", "").startswith("application/json"):
                raise BlueCurrentException(response.json())
            response.raise_for_status()
        result = response.json()
        if result.get("success") is False:
            raise BlueCurrentException(result)
        return result
