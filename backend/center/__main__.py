"""Central MQTT-to-PostgreSQL ingestion service entry point."""

import argparse
import json
import sys

from .config import CenterConfigurationError, load_center_config
from .ingest import CentralIngestService
from .mqtt_consumer import MqttConsumer, connect_persistent_session, create_paho_client
from .postgres import PostgresTelemetryRepository


def _event(name, **fields):
    print(json.dumps({"event": name, **fields}, ensure_ascii=False, sort_keys=True), flush=True)


def main(argv=None):
    parser = argparse.ArgumentParser(description="Oil-well central MQTT telemetry ingestor")
    parser.add_argument("--config", required=True)
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--migrate", action="store_true")
    args = parser.parse_args(argv)
    try:
        config = load_center_config(args.config)
    except CenterConfigurationError as error:
        _event("configuration_error", error=str(error))
        return 2
    repository = PostgresTelemetryRepository(connect_kwargs=config.postgres)
    if args.check:
        _event(
            "configuration_valid",
            mqtt_host=config.mqtt.host,
            mqtt_port=config.mqtt.port,
            postgres_host=config.postgres["host"],
            postgres_database=config.postgres["dbname"],
        )
        return 0
    if args.migrate:
        repository.migrate()
        _event("database_migrated")
        return 0
    service = CentralIngestService(repository, topic_prefix=config.topic_prefix)
    consumer = MqttConsumer(service)
    client = create_paho_client(
        config.mqtt,
        consumer,
        on_error=lambda error, message: _event(
            "ingest_failed", error=str(error), topic=getattr(message, "topic", None)
        ),
    )

    def connected(paho_client, _userdata, _flags, reason_code, _properties=None):
        if getattr(reason_code, "is_failure", False):
            _event("mqtt_connection_rejected", reason=str(reason_code))
            return
        topic = f"{config.topic_prefix}/v1/+/+/telemetry"
        result, _mid = paho_client.subscribe(topic, qos=1)
        if result != 0:
            _event("mqtt_subscribe_failed", code=result, topic=topic)
            paho_client.disconnect()
        else:
            _event("mqtt_subscribed", topic=topic, qos=1)

    client.on_connect = connected
    try:
        connect_persistent_session(
            client,
            config.mqtt,
            config.mqtt_session_expiry_seconds,
        )
        client.loop_forever(retry_first_connection=True)
    except KeyboardInterrupt:
        client.disconnect()
    return 0


if __name__ == "__main__":
    sys.exit(main())
