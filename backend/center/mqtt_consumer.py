"""Manual-ack MQTT consumer boundary for durable central ingestion."""


class MqttConsumerError(RuntimeError):
    pass


class MqttConsumer:
    """Acknowledge incoming QoS 1 messages only after repository commit."""

    def __init__(self, service):
        self.service = service

    def handle_message(self, client, message):
        if message.qos != 1:
            raise MqttConsumerError("central telemetry requires MQTT QoS 1")
        result = self.service.ingest(message.topic, bytes(message.payload))
        ack_result = client.ack(message.mid, message.qos)
        if ack_result not in (0, None):
            raise MqttConsumerError(f"manual PUBACK failed with code {ack_result}")
        return result


def create_paho_client(config, consumer: MqttConsumer, *, on_error=None):
    """Build a Paho v5 client with manual acknowledgement enabled."""

    try:
        import paho.mqtt.client as mqtt
    except ImportError as error:
        raise MqttConsumerError(
            "paho-mqtt is not installed; install requirements-production.txt"
        ) from error
    client = mqtt.Client(
        callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
        client_id=config.client_id,
        protocol=mqtt.MQTTv5,
        reconnect_on_failure=True,
        manual_ack=True,
    )
    if config.username:
        client.username_pw_set(config.username, config.password)
    if config.tls_enabled:
        import ssl
        client.tls_set(
            ca_certs=config.ca_cert,
            certfile=config.client_cert,
            keyfile=config.client_key,
            cert_reqs=ssl.CERT_REQUIRED,
            tls_version=ssl.PROTOCOL_TLS_CLIENT,
        )
    def receive(paho_client, _userdata, message):
        try:
            consumer.handle_message(paho_client, message)
        except Exception as error:
            if on_error:
                on_error(error, message)
            # Closing the session without acknowledging causes the broker to
            # redeliver after reconnect instead of losing the database write.
            paho_client.disconnect()

    client.on_message = receive
    return client


def connect_persistent_session(client, config, session_expiry_seconds: int):
    """Resume the durable subscription so the broker queues offline QoS 1 data."""

    try:
        import paho.mqtt.client as mqtt
    except ImportError as error:
        raise MqttConsumerError("paho-mqtt is required") from error
    properties = mqtt.Properties(mqtt.PacketTypes.CONNECT)
    properties.SessionExpiryInterval = session_expiry_seconds
    return client.connect(
        config.host,
        config.port,
        config.keepalive_seconds,
        clean_start=False,
        properties=properties,
    )
