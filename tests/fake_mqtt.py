"""Offline fake for the MQTT client the relay publishes with.

Patch it in with::

    monkeypatch.setattr("pybluecurrent.relay.relay.MqttClient", make_fake_mqtt(fake_mqtt))

The relay only connects, publishes and subscribes, so :class:`FakeMqtt` duck-types that much. It
records every message published (``sent``) and delivers what a test ``feed``s to ``messages``.
"""

from asyncio import Queue
from json import dumps, loads
from types import SimpleNamespace
from typing import Any, AsyncIterator, Callable


class Message:
    """One published message, decoded."""

    def __init__(self, topic: str, payload: str, qos: int, retain: bool) -> None:
        self.topic = topic
        self.payload: dict[str, Any] = loads(payload)
        self.qos = qos
        self.retain = retain

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"Message(topic={self.topic!r}, payload={self.payload!r}, retain={self.retain})"


class FakeMqtt:
    """Duck-typed stand-in for ``aiomqtt.Client``."""

    def __init__(self, fail_on_connect: BaseException | None = None) -> None:
        self.sent: list[Message] = []
        self.connections = 0
        self.fail_on_connect = fail_on_connect
        self.will: Any = None  # the Will of the last connection
        self.subscriptions: list[tuple[str, int]] = []
        self._incoming: Queue[SimpleNamespace] = Queue()

    async def __aenter__(self) -> "FakeMqtt":
        if self.fail_on_connect is not None:
            error, self.fail_on_connect = self.fail_on_connect, None  # fail once, then connect
            raise error
        self.connections += 1
        return self

    async def __aexit__(self, *exc: Any) -> None:
        return None

    async def publish(
        self, topic: str, payload: str, qos: int = 0, retain: bool = False, timeout: float | None = None
    ) -> None:
        self.sent.append(Message(topic, payload, qos, retain))

    async def subscribe(self, topic: str, qos: int = 0, timeout: float | None = None) -> tuple[int, ...]:
        self.subscriptions.append((topic, qos))
        return (qos,)  # the granted QoS, as a broker answers

    @property
    def messages(self) -> AsyncIterator[SimpleNamespace]:
        return self._deliver()

    async def _deliver(self) -> AsyncIterator[SimpleNamespace]:
        while True:
            yield await self._incoming.get()

    def feed(self, topic: str, payload: dict[str, Any], retain: bool = False) -> None:
        """Deliver a JSON message to the relay."""
        # Only what the relay reads of an aiomqtt Message.
        self._incoming.put_nowait(SimpleNamespace(topic=topic, payload=dumps(payload).encode(), retain=retain))

    def published(self, topic: str) -> list[Message]:
        """Every message published to one topic, oldest first."""
        return [message for message in self.sent if message.topic == topic]

    def last(self, topic: str) -> Message:
        """The most recent message on a topic; fails the test when there is none."""
        published = self.published(topic)
        assert published, f"nothing was published to {topic}; topics seen: {sorted({m.topic for m in self.sent})}"
        return published[-1]

    def topics(self) -> set[str]:
        return {message.topic for message in self.sent}


def make_fake_mqtt(mqtt: FakeMqtt) -> Callable[..., FakeMqtt]:
    """Build a drop-in replacement for ``aiomqtt.Client`` bound to ``mqtt``."""

    def fake_client(*args: Any, **kwargs: Any) -> FakeMqtt:
        mqtt.will = kwargs.get("will")
        return mqtt

    return fake_client
