"""The relay: publish what the BlueCurrent backend reports to an MQTT broker."""

from asyncio import FIRST_COMPLETED, CancelledError, Event, Task, create_task, get_running_loop, sleep, wait, wait_for
from asyncio import TimeoutError as AsyncTimeoutError
from contextlib import aclosing, suppress
from logging import getLogger
from signal import SIGINT, SIGTERM
from time import monotonic
from typing import Any, Mapping

from aiomqtt import Client as MqttClient
from aiomqtt import MqttError, Will

from pybluecurrent.client import BlueCurrentClient
from pybluecurrent.exceptions import ConnectionLost
from pybluecurrent.models import ChargePointStatus
from pybluecurrent.relay.serialize import encode
from pybluecurrent.relay.settings import Settings
from pybluecurrent.relay.state import TransactionStore

logger = getLogger(__name__)

# The message types the relay handles itself; anything else is logged once, as a hint that the
# backend has something to offer that this version does not publish.
_EXPECTED_MESSAGES = ("GRID_CURRENT", "GRID_STATUS", "HELLO", "CHARGE_POINTS", "CH_SETTINGS", "CH_STATUS", "ERROR")


async def run(settings: Settings) -> None:
    """Run the relay until it is asked to stop, or until the backend connection is given up.

    The BlueCurrent connection outlives the MQTT one: a broker that goes away is reconnected to
    without logging in to BlueCurrent again, which the backend would eventually refuse. The store of
    published transactions outlives it too, so a broker outage does not republish them.
    """
    stopping = Event()
    _stop_on_signals(stopping)
    client = BlueCurrentClient(username=settings.username, password=settings.password, api_token=settings.api_token)
    store = TransactionStore(
        settings.transaction_state_file, settings.transaction_lookback_days, settings.transaction_rescan_days
    )
    async with client:
        while not stopping.is_set():
            try:
                await _serve(client, settings, store, stopping)
            except MqttError as error:
                logger.error("MQTT error: %s", error)
                await _wait_for(stopping, settings.mqtt_reconnect_interval)
            except ConnectionLost as error:
                # The client stopped reconnecting to keep the backend from seeing a burst of logins.
                # Leaving now would only move the next login to whatever restarts the relay, so wait
                # the window out first, then let the caller (and its restart policy) take over.
                logger.error("Giving up on the backend: %s", error)
                await _wait_for(stopping, client.reconnect_relogin_window)
                raise
    logger.info("Stopped")


async def _serve(client: BlueCurrentClient, settings: Settings, store: TransactionStore, stopping: Event) -> None:
    """Hold one MQTT connection and publish on it until the relay stops or the connection fails."""
    async with MqttClient(
        hostname=settings.mqtt_host,
        port=settings.mqtt_port,
        username=settings.mqtt_username,
        password=settings.mqtt_password,
        identifier=settings.mqtt_client_id,
        # What the broker says on the relay's behalf if it never gets to say goodbye itself — killed,
        # partitioned off, or gone with its node. Without it a retained "up" outlives the relay.
        will=Will(
            settings.health_topic,
            encode({"status": "down", "error": "lost"}),
            qos=settings.mqtt_qos,
            retain=True,
        ),
    ) as mqtt:
        logger.info("Connected to the broker at %s:%s", settings.mqtt_host, settings.mqtt_port)
        relay = _Relay(client, mqtt, settings, store)
        tasks = relay.start()
        last_word: str | None = "shutdown"
        try:
            await _until_stopped(tasks, stopping)
        except MqttError:
            last_word = None  # the broker is gone; there is nothing to say it on
            raise
        except BaseException as error:
            last_word = type(error).__name__
            raise
        finally:
            await _stop_tasks(tasks)
            if last_word is not None:
                # The last word on a topic nothing else clears: without it the broker keeps serving
                # the retained "up" of a relay that is gone.
                await relay.publish_health("down", error=last_word, timeout=settings.shutdown_publish_timeout)


async def _stop_tasks(tasks: list[Task], timeout: float = 0.5, attempts: int = 4) -> None:
    """Cancel the tasks and wait for them to end, asking again while any is still running.

    Once is not always enough: on Python 3.10 a cancellation that lands exactly as an awaited call
    completes can be lost (asyncio.wait_for keeps the result and drops the CancelledError), and the
    task then carries on as if nothing happened — which would hold up the shutdown until the grace
    period runs out.
    """
    for _ in range(attempts):
        pending = [task for task in tasks if not task.done()]
        if not pending:
            return
        for task in pending:
            task.cancel()
        await wait(pending, timeout=timeout)
    running = [task.get_name() for task in tasks if not task.done()]
    if running:
        logger.warning("Leaving with tasks still running: %s", ", ".join(running))


async def _until_stopped(tasks: list[Task], stopping: Event) -> None:
    """Return when the relay is asked to stop, or raise when a task ends — which is always a failure."""
    waiter = create_task(stopping.wait())
    try:
        done, _ = await wait([waiter, *tasks], return_when=FIRST_COMPLETED)
    finally:
        waiter.cancel()
        with suppress(CancelledError):
            await waiter
    for task in done - {waiter}:
        task.result()  # re-raises what ended it, e.g. the broker going away mid-publish
        raise RuntimeError(f"The {task.get_name()} task ended on its own")


class _Relay:
    """The publishing tasks, and the state they share."""

    def __init__(
        self, client: BlueCurrentClient, mqtt: MqttClient, settings: Settings, store: TransactionStore
    ) -> None:
        self.client = client
        self.mqtt = mqtt
        self.settings = settings
        self.store = store
        self.activity: dict[tuple[str, int], str] = {}
        self.grid: dict[str, dict[str, Any]] = {}
        self.health: dict[str, Any] = {"status": "down", "error": "starting"}
        self.refresh_settings = Event()
        self.sync_transactions = Event()
        self.unknown_messages: set[str] = set()

    def start(self) -> list[Task]:
        tasks = [
            create_task(self._poll_statuses(), name="status poller"),
            create_task(self._poll_settings(), name="settings poller"),
            create_task(self._follow_grid(), name="grid feed"),
            create_task(self._heartbeat(), name="heartbeat"),
        ]
        if self.settings.sync_transactions:
            tasks.append(create_task(self._sync_transactions(), name="transaction syncer"))
        return tasks

    async def publish(
        self, topic: str, payload: Mapping[str, Any], retain: bool = True, timeout: float | None = None
    ) -> None:
        logger.debug("Publishing to %s", topic)
        await self.mqtt.publish(
            topic, payload=encode(payload), qos=self.settings.mqtt_qos, retain=retain, timeout=timeout
        )

    async def publish_health(self, status: str, error: str | None = None, timeout: float | None = None) -> None:
        """Publish, and remember, the verdict on the backend connection."""
        self.health = {"status": status, **({"error": error} if error is not None else {})}
        await self.publish(self.settings.health_topic, self.health, timeout=timeout)

    async def _poll_statuses(self) -> None:
        """Publish the live status of every socket, and notice when one changes."""
        logger.info("Polling charge point status every %ss", self.settings.poll_interval)
        while True:
            started = monotonic()
            statuses, failure = await self._read_statuses()
            for evse_id, status in statuses:
                await self.publish(self.settings.status_topic(evse_id, status["socket_id"]), status)
                self._note_activity(evse_id, status)
            await self.publish_health("up") if failure is None else await self.publish_health("down", error=failure)
            await sleep(max(1.0, self.settings.poll_interval - (monotonic() - started)))

    async def _read_statuses(self) -> tuple[list[tuple[str, ChargePointStatus]], str | None]:
        """Read every socket of every charge point; report a failure rather than raising it.

        A bad response must not end weeks of uptime, so anything the backend or the network throws
        is turned into a health verdict — including what no model expects, such as a maintenance
        page where JSON belongs.
        """
        statuses = []
        try:
            for charge_point in await self.client.get_charge_points():
                evse_id = charge_point["evse_id"]
                statuses += [(evse_id, status) for status in await self.client.get_charge_point_statuses(evse_id)]
        except Exception as error:
            logger.error("Could not read the charge points: %r", error)
            return statuses, type(error).__name__
        return statuses, None

    def _note_activity(self, evse_id: str, status: ChargePointStatus) -> None:
        """A socket that changed what it is doing may have finished a session, and its settings may differ."""
        key = (evse_id, status["socket_id"])
        activity = status["activity"]
        if self.activity.get(key, activity) != activity:
            logger.info("Charge point %s socket %s is now %s", evse_id, status["socket_id"], activity)
            self.refresh_settings.set()
            self.sync_transactions.set()
        self.activity[key] = activity

    async def _poll_settings(self) -> None:
        """Publish each charge point's settings, refreshed on a change and on a slow interval."""
        while True:
            self.refresh_settings.clear()  # before reading, so a change during the read is not lost
            try:
                charge_points = await self.client.get_charge_points()
            except Exception as error:
                logger.error("Could not read the charge point settings: %r", error)
                charge_points = []
            for charge_point in charge_points:
                await self.publish(self.settings.settings_topic(charge_point["evse_id"]), charge_point)
            await _wait_for(self.refresh_settings, self.settings.settings_interval)

    async def _follow_grid(self) -> None:
        """Publish the grid status: fetched once per charge point, then kept current from the messages."""
        await self._prime_grid()
        async with aclosing(self.client.live_updates()) as updates:
            async for message in updates:
                name, evse_id = message.get("object"), message.get("evse_id")
                if name == "GRID_CURRENT" and isinstance(evse_id, str):
                    grid = self.grid.setdefault(evse_id, {"evse_id": evse_id})
                    grid.update({key: value for key, value in message.items() if key.startswith("grid_")})
                    await self.publish(self.settings.grid_topic(evse_id), grid)
                elif name and name not in _EXPECTED_MESSAGES and name not in self.unknown_messages:
                    # Once per type: worth seeing in the log, not worth repeating forever.
                    self.unknown_messages.add(name)
                    logger.info("Received a message type the relay does not publish: %s", name)

    async def _prime_grid(self) -> None:
        """Fetch the full grid status, of which the messages only update the actual currents."""
        try:
            for charge_point in await self.client.get_charge_points():
                evse_id = charge_point["evse_id"]
                grid = dict(await self.client.get_grid_status(evse_id))
                grid["evse_id"] = evse_id  # the grid status names its own connection, not the charge point
                self.grid[evse_id] = grid
                await self.publish(self.settings.grid_topic(evse_id), grid)
        except MqttError:
            raise  # the broker, not the backend: let the connection be rebuilt
        except Exception as error:
            logger.error("Could not read the grid status: %r", error)

    async def _sync_transactions(self) -> None:
        """Publish transactions that were not published before, on start and after every change."""
        self.sync_transactions.set()
        while True:
            await self.sync_transactions.wait()
            self.sync_transactions.clear()
            try:
                charge_points = await self.client.get_charge_points()
            except Exception as error:
                logger.error("Could not read the charge points: %r", error)
                await sleep(self.settings.poll_interval)
                self.sync_transactions.set()  # try again
                continue
            for charge_point in charge_points:
                await self._sync_charge_point(charge_point["evse_id"])
            self.store.save()

    async def _sync_charge_point(self, evse_id: str) -> None:
        published = 0
        try:
            async for transaction in self.client.iterate_transactions(
                evse_id=evse_id, newest_first=True, start_date=self.store.start_date(evse_id)
            ):
                transaction_id = transaction["transaction_id"]
                if self.store.seen(evse_id, transaction_id):
                    continue
                await self.publish(self.settings.transactions_topic(evse_id), _with_duration(transaction), retain=False)
                self.store.add(evse_id, transaction_id, transaction.get("started_at"))
                published += 1
        except MqttError:
            raise  # the broker, not the backend
        except Exception as error:
            # One charge point that cannot be read (a former one, say) must not stop the others.
            logger.error("Could not read the transactions of %s: %r", evse_id, error)
        if published:
            logger.info("Published %s new transactions for %s", published, evse_id)

    async def _heartbeat(self) -> None:
        """Repeat the last verdict, so a consumer can tell a living relay from a retained message."""
        while True:
            await sleep(self.settings.heartbeat_interval)
            await self.publish(self.settings.health_topic, self.health, retain=False)


def _with_duration(transaction: Mapping[str, Any]) -> dict[str, Any]:
    """The transaction with how long the session lasted, in seconds, when both of its ends are known."""
    started_at, end_time = transaction.get("started_at"), transaction.get("end_time")
    if started_at is None or end_time is None:
        return dict(transaction)
    return {**transaction, "duration": (end_time - started_at).total_seconds()}


def _stop_on_signals(stopping: Event) -> None:
    """Stop on SIGTERM and SIGINT.

    Registered on the running loop rather than with signal.signal: a container runs this as PID 1,
    where the kernel drops a signal that has no handler installed, so the relay would otherwise poll
    on through its whole grace period and be killed with a stale "up" retained on the health topic.
    """

    def stop() -> None:
        logger.info("Stopping")
        stopping.set()

    loop = get_running_loop()
    for number in (SIGINT, SIGTERM):
        loop.add_signal_handler(number, stop)


async def _wait_for(event: Event, seconds: float) -> None:
    """Wait for an event, giving up after ``seconds``.

    Suppresses asyncio's own TimeoutError, which on Python 3.10 is still a class of its own rather
    than the builtin one.
    """
    with suppress(AsyncTimeoutError):
        await wait_for(event.wait(), timeout=seconds)
