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
    poll_interval: Annotated[float, Option(envvar="POLL_INTERVAL", min=1, help="Seconds between status polls.")] = 30.0,
    settings_interval: Annotated[
        float, Option(envvar="SETTINGS_INTERVAL", min=1, help="Seconds between settings polls.")
    ] = 300.0,
    sync_transactions: Annotated[
        bool, Option(envvar="SYNC_TRANSACTIONS", help="Also publish charging transactions.")
    ] = False,
    transaction_lookback_days: Annotated[
        int, Option(envvar="TRANSACTION_LOOKBACK_DAYS", help="How far the first sync looks back; 0 for all history.")
    ] = 28,
    transaction_rescan_days: Annotated[
        int, Option(envvar="TRANSACTION_RESCAN_DAYS", help="How far each later sync looks back.")
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
    """Publish charge point status, settings and grid current to MQTT, until stopped."""
    try:
        from pybluecurrent.relay import Settings, run
    except ImportError as error:  # pragma: no cover - depends on how the package was installed
        raise BadParameter(f"The relay needs its extra: pip install pybluecurrent[mqtt] ({error}).")

    basicConfig(format="%(asctime)s %(levelname)s %(message)s", level=INFO)
    if debug:
        getLogger("pybluecurrent").setLevel(DEBUG)
    try:
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
    except ValueError as error:  # no credentials, or an unusable combination of them
        raise BadParameter(str(error))
    try:
        run_async(run(settings))
    except BlueCurrentException as error:
        # Gave up on the backend, or the credentials were rejected: say so and leave it to whatever
        # restarts the relay, rather than printing a traceback at a reader who cannot act on it.
        echo(f"error: {error}", err=True)
        raise Exit(code=1)
