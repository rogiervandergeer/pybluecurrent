from asyncio import get_event_loop_policy, run
from os import environ
from typing import AsyncGenerator

from pytest import fixture, skip
from pytest_asyncio import fixture as async_fixture

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
def event_loop():
    policy = get_event_loop_policy()
    loop = policy.new_event_loop()
    yield loop
    loop.close()


@async_fixture(scope="session")
async def connected_client() -> AsyncGenerator[BlueCurrentClient, None]:
    try:
        client = BlueCurrentClient(environ["BLUECURRENT_USERNAME"], environ["BLUECURRENT_PASSWORD"])
    except KeyError:
        skip("Requires authentication.")
        return
    async with client:
        yield client


@fixture(scope="session")
def evse_id(client_with_auth: BlueCurrentClient) -> str:
    async def _get_charge_points(client: BlueCurrentClient) -> list:
        async with client:
            charge_points = await client.get_charge_points()
        return charge_points

    result: list = run(_get_charge_points(client_with_auth))
    if len(result) == 0:
        skip("No charge points available.")
    return result[0]["evse_id"]
