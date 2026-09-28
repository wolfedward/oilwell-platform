"""Central MQTT-to-PostgreSQL ingestion service entry point."""

import argparse
import json
import sys

from .config import CenterConfigurationError, load_center_config
from .ingest import CentralIngestService
from .mqtt_consumer import MqttConsumer, create_paho_client, run_persistent_session
from .postgres import PostgresTelemetryRepository
from backend.ingest_reliability.errors import ReliabilityError


def _event(name, **fields):
    print(json.dumps({"event": name, **fields}, ensure_ascii=False, sort_keys=True), flush=True)


def probe_role(connect, role):
    # Keep optional PostgreSQL dependencies lazy in the legacy disabled path.
    from backend.ingest_reliability.privileges import probe_role as check
    return check(connect, role)


def _reliable_repository(settings):
    try:
        import psycopg
    except ImportError:
        raise ReliabilityError("ingest.dependency_missing", "请安装生产接入依赖后再启用可靠接收") from None
    from backend.ingest_reliability.repository import ReliableRepository
    connect = lambda: psycopg.connect(**settings.postgres_ingest)
    probe_role(connect, "ingestor")
    try:
        with connect() as connection:
            connection.execute("SET TRANSACTION READ ONLY")
            connection.execute("SET LOCAL statement_timeout='3s'")
            connection.execute("SET LOCAL lock_timeout='1s'")
            row = connection.execute("SELECT total_bytes,quarantine_bytes FROM ingest_reliability.capacity WHERE singleton=1").fetchone()
            if row != (settings.limits.total_bytes, settings.limits.quarantine_bytes):
                raise ReliabilityError("ingest.capacity_mismatch", "配置容量与已迁移的数据库容量不一致，未执行更改")
    except psycopg.Error:
        raise ReliabilityError("ingest.connection_unavailable", "接收可靠性数据库连接或容量检查暂不可用") from None
    return connect, ReliableRepository


def main(argv=None):
    parser = argparse.ArgumentParser(description="Oil-well central MQTT telemetry ingestor")
    parser.add_argument("--config", required=True)
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--migrate", action="store_true")
    args = parser.parse_args(argv)
    try:
        config = load_center_config(args.config)
    except ReliabilityError as error:
        _event("configuration_error", code=error.code, error=str(error))
        return 2
    except CenterConfigurationError as error:
        _event("configuration_error", error=str(error))
        return 2
    reliability = getattr(config, "reliability", None)
    if reliability is not None:
        try:
            if args.migrate:
                raise ReliabilityError("ingest.explicit_migration_required", "可靠接收须使用独立迁移账号显式初始化，运行账号不执行迁移")
            connect, repository_type = _reliable_repository(reliability)
            repository = repository_type(connect, prefix=config.topic_prefix,
                                         receipt_gateways=reliability.receipt_gateways, limits=reliability.limits)
        except ReliabilityError as error:
            _event("ingest_startup_failed", code=error.code, error=str(error))
            return 2
    else:
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
    if reliability is not None:
        from backend.ingest_reliability.consumer import ReliableConsumer
        consumer = ReliableConsumer(repository)
    else:
        service = CentralIngestService(repository, topic_prefix=config.topic_prefix)
        consumer = MqttConsumer(service)
    client = create_paho_client(
        config.mqtt,
        consumer,
        on_error=lambda error, message: _event(
            "ingest_failed", error_type=type(error).__name__, mid=getattr(message, "mid", None)
        ),
    )

    pending_subscription = None
    topic = f"{config.topic_prefix}/v1/+/+/telemetry"

    def connected(paho_client, _userdata, _flags, reason_code, _properties=None):
        nonlocal pending_subscription
        pending_subscription = None
        if getattr(reason_code, "is_failure", False):
            _event("mqtt_connection_rejected", code=getattr(reason_code, "value", None))
            paho_client.disconnect()
            return
        if reliability is not None:
            from paho.mqtt.subscribeoptions import SubscribeOptions
            result, mid = paho_client.subscribe(topic, options=SubscribeOptions(qos=1, retainAsPublished=True))
        else:
            result, mid = paho_client.subscribe(topic, qos=1)
        if result != 0:
            _event("mqtt_subscribe_failed", code=result, topic=topic)
            paho_client.disconnect()
        else:
            pending_subscription = mid
            _event("mqtt_subscribe_requested", topic=topic, qos=1)

    def subscribed(paho_client, _userdata, mid, reason_codes, _properties=None):
        nonlocal pending_subscription
        if mid != pending_subscription:
            return
        pending_subscription = None
        granted = [getattr(code, "value", code) for code in reason_codes]
        if granted != [1]:
            _event("mqtt_subscribe_rejected", topic=topic, codes=granted)
            paho_client.disconnect()
            return
        _event("mqtt_subscribed", topic=topic, qos=1)

    client.on_connect = connected
    client.on_subscribe = subscribed
    try:
        run_persistent_session(
            client,
            config.mqtt,
            config.mqtt_session_expiry_seconds,
            on_retry=lambda delay, error_type: _event(
                "mqtt_retry_scheduled", delay_seconds=delay, error_type=error_type
            ),
            **({"receive_maximum": getattr(reliability, "receive_maximum", 20)} if reliability is not None else {}),
        )
    except KeyboardInterrupt:
        client.disconnect()
        _event("ingestor_stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
