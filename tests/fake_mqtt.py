"""Offline fake for the MQTT client the relay publishes with.

Patch it in with::

    monkeypatch.setattr("pybluecurrent.relay.relay.MqttClient", make_fake_mqtt(fake_mqtt))

The relay only connects, publishes and (later) subscribes, so :class:`FakeMqtt` duck-types that
much, recording every message so a test can assert what was published, to which topic, and how.
"""

from json import loads
from typing import Any, Callable


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
        self.messages: list[Message] = []
        self.connections = 0
        self.fail_on_connect = fail_on_connect

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
        self.messages.append(Message(topic, payload, qos, retain))

    def published(self, topic: str) -> list[Message]:
        """Every message published to one topic, oldest first."""
        return [message for message in self.messages if message.topic == topic]

    def last(self, topic: str) -> Message:
        """The most recent message on a topic; fails the test when there is none."""
        published = self.published(topic)
        assert published, f"nothing was published to {topic}; topics seen: {sorted({m.topic for m in self.messages})}"
        return published[-1]

    def topics(self) -> set[str]:
        return {message.topic for message in self.messages}


def make_fake_mqtt(mqtt: FakeMqtt) -> Callable[..., FakeMqtt]:
    """Build a drop-in replacement for ``aiomqtt.Client`` bound to ``mqtt``."""

    def fake_client(*args: Any, **kwargs: Any) -> FakeMqtt:
        return mqtt

    return fake_client
