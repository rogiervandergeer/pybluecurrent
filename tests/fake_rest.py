"""Offline fake for the BlueCurrent REST transport.

Replaces ``httpx.AsyncClient`` in tests so the client's REST calls can run without a live
backend. Patch it in with::

    monkeypatch.setattr("pybluecurrent.client.AsyncClient", make_fake_async_client(fake_rest))

Every request is recorded — :attr:`FakeRest.requests` and :attr:`FakeRest.last_body` let a test
assert the exact wire payload — and answered with ``{"success": True}`` unless a different
response is scripted with :meth:`FakeRest.on`.
"""

from json import loads
from typing import Any, Callable

from httpx import AsyncClient, MockTransport, Request, Response


class FakeRest:
    """Recorder and scripted responder for the REST API."""

    def __init__(self) -> None:
        self.requests: list[Request] = []
        self.responses: dict[str, tuple[int, dict[str, Any]]] = {}

    # -- server -> client -------------------------------------------------
    def on(self, path: str, body: dict[str, Any], status_code: int = 200) -> None:
        """Script the response returned for requests to ``path`` (the last segment of the URL)."""
        self.responses[path] = (status_code, body)

    def handle(self, request: Request) -> Response:
        self.requests.append(request)
        status_code, body = self.responses.get(request.url.path.rsplit("/", 1)[-1], (200, {"success": True}))
        return Response(status_code, json=body)

    # -- client -> server -------------------------------------------------
    @property
    def last_path(self) -> str:
        """The path of the most recent request, without the API base URL."""
        return self.requests[-1].url.path.rsplit("/", 1)[-1]

    @property
    def last_body(self) -> Any:
        """The decoded JSON body of the most recent request."""
        return loads(self.requests[-1].content)


def make_fake_async_client(rest: FakeRest) -> Callable[..., AsyncClient]:
    """Build a drop-in replacement for ``AsyncClient`` whose requests are served by ``rest``."""

    def fake_async_client(**kwargs: Any) -> AsyncClient:
        return AsyncClient(transport=MockTransport(rest.handle), **kwargs)

    return fake_async_client
