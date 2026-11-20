"""The commands the relay runs when asked on its command topic.

A fixed registry: only a command listed in COMMANDS runs, and no method is looked up by name.
"""

from json import loads
from typing import Any, Awaitable, Callable

from pybluecurrent.client import BlueCurrentClient


async def _set_card(client: BlueCurrentClient, request: dict[str, Any]) -> None:
    """Set the plug-and-charge card, only while every socket is available. A missing or null uid
    means no card.

    The status is read fresh: the status topic can be a poll behind, as the pushes can stop.
    """
    evse_id, uid = request["evse_id"], request.get("uid")
    if uid is not None and (not isinstance(uid, str) or not uid):
        raise ValueError("uid must be a card UID, or null for no card.")
    try:
        statuses = await client.get_charge_point_statuses(evse_id)
    except Exception as error:  # short, for the answer: the full error names the URL
        raise ValueError(f"Could not read the status of {evse_id}: {type(error).__name__}") from error
    if not statuses:
        raise ValueError(f"{evse_id} reports no sockets")
    for status in statuses:
        if status["activity"] != "available":  # the card is for the whole charge point
            raise ValueError(f"{evse_id} socket {status['socket_id']} is {status['activity']}")
    await client.set_plug_and_charge_charge_card(evse_id, uid)


COMMANDS: dict[str, Callable[[BlueCurrentClient, dict[str, Any]], Awaitable[None]]] = {"set_card": _set_card}


async def run_command(client: BlueCurrentClient, payload: Any) -> dict[str, Any]:
    """Run one request from the command topic and return the answer to publish.

    Anything that goes wrong, from bad JSON to a refusal by the backend, becomes an "error" answer.
    """
    answer: dict[str, Any] = {"command": None, "id": None, "evse_id": None}
    try:
        request = loads(payload)
        if not isinstance(request, dict):
            raise ValueError("A command must be a JSON object.")
        answer.update({key: request.get(key) for key in answer})
        handler = COMMANDS.get(request.get("command"))
        if handler is None:
            raise ValueError(f"Unknown command: {request.get('command')!r}.")
        if not isinstance(request.get("evse_id"), str):
            raise ValueError("evse_id must be a string.")
        await handler(client, request)
    except Exception as error:
        return {**answer, "status": "error", "error": str(error) or type(error).__name__}
    return {**answer, "status": "ok"}
