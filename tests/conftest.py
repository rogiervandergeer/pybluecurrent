from os import environ
from typing import AsyncGenerator

from pytest import fixture, skip

from pybluecurrent import BlueCurrentClient


@fixture(scope="function")
def client() -> BlueCurrentClient:
    return BlueCurrentClient("username", "password")


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
