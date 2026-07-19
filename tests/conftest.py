from os import environ
from typing import AsyncGenerator

from fake_rest import FakeRest, make_fake_async_client
from fake_socket import FakeSocket, make_fake_connect
from pytest import MonkeyPatch, fixture, skip

from pybluecurrent import BlueCurrentClient


@fixture(scope="function")
def client() -> BlueCurrentClient:
    return BlueCurrentClient("username", "password")


@fixture(scope="function")
def fake_socket() -> FakeSocket:
    """A fresh offline websocket with the default auth + HELLO handshake scripted."""
    return FakeSocket()


@fixture(scope="function")
def fake_rest() -> FakeRest:
    """A fresh offline REST API, answering every request with success."""
    return FakeRest()


@fixture(scope="function")
async def offline_client(
    monkeypatch: MonkeyPatch, fake_socket: FakeSocket, fake_rest: FakeRest
) -> AsyncGenerator[BlueCurrentClient, None]:
    """A connected client backed by ``fake_socket`` and ``fake_rest`` — no credentials, no network.

    Auto-reconnect is disabled here so a dropped connection stays terminal, matching what the
    transport/command/lifecycle tests assert. The reconnect supervisor has its own tests (TestReconnect).
    """
    monkeypatch.setattr("pybluecurrent.client.connect", make_fake_connect(fake_socket))
    monkeypatch.setattr("pybluecurrent.client.AsyncClient", make_fake_async_client(fake_rest))
    client = BlueCurrentClient("username", "password")
    client.auto_reconnect = False
    async with client:
        yield client


@fixture(scope="session")
def client_with_auth() -> BlueCurrentClient | None:
    try:
        return BlueCurrentClient(environ["BLUECURRENT_USERNAME"], environ["BLUECURRENT_PASSWORD"])
    except KeyError:
        raise skip("Requires authentication.")


@fixture(scope="session")
async def connected_client() -> AsyncGenerator[BlueCurrentClient, None]:
    try:
        client = BlueCurrentClient(environ["BLUECURRENT_USERNAME"], environ["BLUECURRENT_PASSWORD"])
    except KeyError:
        skip("Requires authentication.")
        return
    async with client:
        yield client


@fixture(scope="session")
async def evse_id(connected_client: BlueCurrentClient) -> str:
    charge_points = await connected_client.get_charge_points()
    if not charge_points:
        skip("No charge points available.")
    return charge_points[0]["evse_id"]  # type: ignore
