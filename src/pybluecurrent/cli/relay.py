"""The ``relay`` command: publish what your charge points report to an MQTT broker."""

from asyncio import run as run_async
from logging import DEBUG, INFO, basicConfig, getLogger
from pathlib import Path
from typing import Annotated

from typer import BadParameter, Exit, Option, echo

from pybluecurrent.exceptions import BlueCurrentException


def relay(
    mqtt_host: Annotated[str, Option(envvar="MQTT_HOST", help="Broker to publish to.")] = "localhost",
    mqtt_port: Annotated[int, Option(envvar="MQTT_PORT", help="Broker port.")] = 1883,
    mqtt_username: Annotated[str | None, Option(envvar="MQTT_USERNAME")] = None,
    mqtt_password: Annotated[str | None, Option(envvar="MQTT_PASSWORD")] = None,
    mqtt_client_id: Annotated[
        str, Option(envvar="MQTT_CLIENT_ID", help="Client identifier to connect with.")
    ] = "pybluecurrent-relay",
    mqtt_qos: Annotated[int, Option(envvar="MQTT_QOS", min=0, max=2, help="Quality of service to publish with.")] = 1,
    topic_prefix: Annotated[str, Option(envvar="MQTT_TOPIC_PREFIX", help="Root of every topic.")] = "bluecurrent",
    poll_interval: Annotated[
        float, Option(envvar="POLL_INTERVAL", min=15, help="Seconds between status polls.")
    ] = 30.0,
    settings_interval: Annotated[
        float,
        Option(
            envvar="SETTINGS_INTERVAL",
            min=120,  # the backend blocks an account that asks too often
            help="Seconds between settings and transaction polls; both also run after a change and a command.",
        ),
    ] = 3600.0,
    sync_transactions: Annotated[
        bool, Option(envvar="SYNC_TRANSACTIONS", help="Also publish charging transactions.")
    ] = False,
    transaction_lookback_days: Annotated[
        int,
        Option(envvar="TRANSACTION_LOOKBACK_DAYS", min=0, help="How far the first sync looks back; 0 for all history."),
    ] = 28,
    transaction_rescan_days: Annotated[
        int, Option(envvar="TRANSACTION_RESCAN_DAYS", min=0, help="How far each later sync looks back.")
    ] = 2,
    transaction_state_file: Annotated[
        Path | None,
        Option(envvar="TRANSACTION_STATE_FILE", help="Where to remember published transactions across restarts."),
    ] = None,
    username: Annotated[str | None, Option(envvar="BLUECURRENT_USERNAME")] = None,
    password: Annotated[str | None, Option(envvar="BLUECURRENT_PASSWORD")] = None,
    api_token: Annotated[str | None, Option(envvar="BLUECURRENT_API_TOKEN")] = None,
    debug: Annotated[bool, Option(envvar="BLUECURRENT_DEBUG", help="Log every message exchanged.")] = False,
) -> None:
    """Publish charge point status, settings and grid current to MQTT, and take commands, until stopped."""
    try:
        from pybluecurrent.relay import Settings, run
    except ImportError as error:
        raise BadParameter(f"The relay needs its extra: pip install pybluecurrent[mqtt] ({error}).")
    if api_token is None and (username is None or password is None):
        raise BadParameter("Set BLUECURRENT_USERNAME and BLUECURRENT_PASSWORD, or BLUECURRENT_API_TOKEN.")

    basicConfig(format="%(asctime)s %(levelname)s %(message)s", level=INFO)
    if debug:
        getLogger("pybluecurrent").setLevel(DEBUG)
    settings = Settings(
        username=username,
        password=password,
        api_token=api_token,
        mqtt_host=mqtt_host,
        mqtt_port=mqtt_port,
        mqtt_username=mqtt_username,
        mqtt_password=mqtt_password,
        mqtt_client_id=mqtt_client_id,
        mqtt_qos=mqtt_qos,
        topic_prefix=topic_prefix,
        poll_interval=poll_interval,
        settings_interval=settings_interval,
        sync_transactions=sync_transactions,
        transaction_lookback_days=transaction_lookback_days,
        transaction_rescan_days=transaction_rescan_days,
        transaction_state_file=transaction_state_file,
    )
    try:
        run_async(run(settings))
    except BlueCurrentException as error:
        # Login rejected or backend given up: print one line, not a traceback.
        echo(f"error: {error}", err=True)
        raise Exit(code=1)
