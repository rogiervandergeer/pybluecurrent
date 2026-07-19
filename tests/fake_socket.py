"""Offline fake for the BlueCurrent websocket transport.

Replaces ``websockets.asyncio.client.connect`` in tests so the client can run without a
live backend. Patch it in with::

    monkeypatch.setattr("pybluecurrent.client.connect", make_fake_connect(fake_socket))

The client only uses the socket in a handful of ways (async iteration in ``_handler``,
``send`` in ``_send``, and the ``connect(...)`` async context manager in ``__aenter__``),
so ``FakeSocket`` duck-types just those.

Two ways to drive responses:

* **Scripted** — a ``responder`` maps a command name to the frames the server sends back.
  A default responder scripts the auth handshake and ``HELLO`` so ``async with client``
  completes offline. Register more with :meth:`FakeSocket.on`.
* **Manual** — a command with no script feeds nothing; the test injects frames itself with
  :meth:`FakeSocket.feed` (in any order, duplicated, as ``ERROR``, or as raw non-JSON).
"""

from asyncio import Queue
from json import dumps, loads
from pathlib import Path
from typing import Any, Callable

Responder = Callable[[dict[str, Any]], list[dict[str, Any]]]

_FIXTURE_DIR = Path(__file__).parent / "fixtures"


def load_fixture(name: str) -> dict[str, Any]:
    """Load a recorded server frame from ``tests/fixtures`` (``.json`` optional)."""
    path = _FIXTURE_DIR / (name if name.endswith(".json") else f"{name}.json")
    return loads(path.read_text())


# Token the default handshake hands back; tests can assert against it.
FAKE_TOKEN = "fake-token-123"
FAKE_CUSTOMER_ID = "fake-customer-1"

# Sentinel enqueued by close() to end the ``async for`` loop in ``_handler``.
_DISCONNECT = object()


def default_responder() -> dict[str, Responder]:
    """A responder scripting the auth handshake and ``HELLO`` (enough for ``__aenter__``)."""
    return {
        "VALIDATE_PASSWORD": lambda msg: [{"object": "STATUS_PASSWORD", "accepted": True, "token": FAKE_TOKEN}],
        "VALIDATE_API_TOKEN": lambda msg: [
            {"object": "STATUS_API_TOKEN", "success": True, "token": FAKE_TOKEN, "customer_id": FAKE_CUSTOMER_ID}
        ],
        "HELLO": lambda msg: [{"object": "HELLO"}],
    }


class FakeSocket:
    """Duck-typed stand-in for a ``websockets`` client connection."""

    def __init__(self, responder: dict[str, Responder] | None = None) -> None:
        self._outbound: Queue[Any] = Queue()
        self.sent: list[dict[str, Any]] = []
        self.responder: dict[str, Responder] = default_responder() if responder is None else responder
        self.closed = False

    # -- server -> client -------------------------------------------------
    def feed(self, frame: dict[str, Any] | str) -> None:
        """Push a server->client frame. A dict is JSON-encoded; a str is sent verbatim.

        Raw strings let tests exercise the non-JSON-frame path (``_handler`` calls ``loads``).
        """
        self._outbound.put_nowait(frame if isinstance(frame, str) else dumps(frame, ensure_ascii=False))

    def close(self) -> None:
        """Signal a disconnect so the ``async for`` in ``_handler`` ends cleanly."""
        self.closed = True
        self._outbound.put_nowait(_DISCONNECT)

    def __aiter__(self) -> "FakeSocket":
        return self

    async def __anext__(self) -> str:
        frame = await self._outbound.get()
        if frame is _DISCONNECT:
            raise StopAsyncIteration
        return frame

    # -- client -> server -------------------------------------------------
    async def send(self, raw: str) -> None:
        """Record the outgoing message and feed any scripted responses.

        Uses ``put_nowait`` and never suspends, so the caller resumes synchronously and
        subscribes to the response queue before the handler task runs — mirroring the fact
        that real network latency keeps ``send`` ahead of the reply.
        """
        message = loads(raw)
        self.sent.append(message)
        handler = self.responder.get(message.get("command"))
        if handler is not None:
            for frame in handler(message):
                self.feed(frame)

    def on(self, command: str, *frames: dict[str, Any]) -> None:
        """Script the frames returned when ``command`` is sent."""
        self.responder[command] = lambda msg: list(frames)


class FakeConnection:
    """Async context manager returned by :func:`make_fake_connect`, yielding a FakeSocket."""

    def __init__(self, socket: FakeSocket) -> None:
        self.socket = socket

    async def __aenter__(self) -> FakeSocket:
        return self.socket

    async def __aexit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        self.socket.close()


def make_fake_connect(socket: FakeSocket) -> Callable[..., FakeConnection]:
    """Build a drop-in replacement for ``connect`` bound to ``socket``.

    The returned callable accepts (and ignores) the ``url`` / ``user_agent_header`` args the
    client passes, and returns a :class:`FakeConnection`.
    """

    def fake_connect(*args: Any, **kwargs: Any) -> FakeConnection:
        return FakeConnection(socket)

    return fake_connect


class FailingConnection:
    """A connection whose ``__aenter__`` raises — simulates a connect that never establishes.

    Use it in a :func:`make_reconnecting_connect` sequence to model a transport failure (``OSError``)
    during a reconnect attempt.
    """

    def __init__(self, error: BaseException) -> None:
        self._error = error

    async def __aenter__(self) -> Any:
        raise self._error

    async def __aexit__(self, *exc: Any) -> None:
        return None


def make_reconnecting_connect(factory: Callable[[], Any]) -> Callable[..., Any]:
    """Build a ``connect`` replacement that returns ``factory()`` for every new connection.

    Unlike :func:`make_fake_connect` (which reuses one socket), this asks ``factory`` for a fresh
    connection each time the client (re)connects — mirroring a real ``connect()`` that yields a new
    socket per call. ``factory`` returns an async context manager: a :class:`FakeConnection` wrapping
    a fresh :class:`FakeSocket`, or a :class:`FailingConnection` to simulate a failed attempt. Pass a
    closure over an iterator to script a sequence of drops / failures / successes.
    """

    def fake_connect(*args: Any, **kwargs: Any) -> Any:
        return factory()

    return fake_connect
