class BlueCurrentException(Exception):
    """Base for every error this client raises."""


class AuthenticationFailed(BlueCurrentException, ValueError):
    """Login or token validation was rejected by the backend."""


class ConnectionLost(BlueCurrentException):
    """The websocket handler terminated; the connection is no longer usable."""


class RequestTimeout(BlueCurrentException, TimeoutError):
    """A request did not receive its response within the deadline.

    Also a builtin ``TimeoutError``, so ``except TimeoutError`` catches it.
    """


class _GiveUp(Exception):
    """Internal signal that the reconnect supervisor is abandoning the connection permanently.

    Carries the terminal ``reason`` (an ``AuthenticationFailed`` for bad credentials, or a
    ``ConnectionLost`` when it stops trying to reconnect) to latch onto ``_closed``. Deliberately
    not a ``BlueCurrentException`` so it never leaks to callers or gets caught by the reconnect
    loop's ``except BlueCurrentException``.
    """

    def __init__(self, reason: BlueCurrentException) -> None:
        super().__init__(reason)
        self.reason = reason
