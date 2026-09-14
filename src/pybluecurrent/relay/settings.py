"""Everything the relay is configured with, in one place."""

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Settings:
    """The relay's configuration, as the command line and the environment supply it."""

    username: str | None = None
    password: str | None = None
    api_token: str | None = None
    mqtt_host: str = "localhost"
    mqtt_port: int = 1883
    mqtt_username: str | None = None
    mqtt_password: str | None = None
    mqtt_client_id: str = "pybluecurrent-relay"
    mqtt_qos: int = 1
    topic_prefix: str = "bluecurrent"
    poll_interval: float = 30.0
    settings_interval: float = 300.0
    sync_transactions: bool = False
    transaction_lookback_days: int = 28
    transaction_rescan_days: int = 2
    transaction_state_file: Path | None = None

    # Not configurable: the heartbeat the health topic is read by, the wait between MQTT reconnects,
    # and the budget for the last message on the way out (the rest of the grace period is needed to
    # close the connections).
    heartbeat_interval: float = 60.0
    mqtt_reconnect_interval: float = 30.0
    shutdown_publish_timeout: float = 5.0

    def status_topic(self, evse_id: str, socket_id: int) -> str:
        return f"{self.topic_prefix}/{evse_id}/{socket_id}/status"

    def settings_topic(self, evse_id: str) -> str:
        return f"{self.topic_prefix}/{evse_id}/settings"

    def grid_topic(self, evse_id: str) -> str:
        return f"{self.topic_prefix}/{evse_id}/grid"

    def transactions_topic(self, evse_id: str) -> str:
        return f"{self.topic_prefix}/{evse_id}/transactions"

    @property
    def health_topic(self) -> str:
        return f"{self.topic_prefix}/backend_health"
