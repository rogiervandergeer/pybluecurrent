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
