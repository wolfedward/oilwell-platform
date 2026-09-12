"""Authenticated MQTT uplink which acknowledges the spool only after PUBACK."""

from dataclasses import dataclass, field
import ipaddress
from pathlib import Path
import ssl
import threading
import time


class MqttConfigurationError(ValueError):
    pass


class MqttPublishError(RuntimeError):
    pass


def _is_loopback(host: str) -> bool:
    if host.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


@dataclass(frozen=True)
class MqttConfig:
    host: str
    port: int
    client_id: str
    username: str | None = None
    password: str | None = field(default=None, repr=False)
    ca_cert: str | None = None
    client_cert: str | None = None
    client_key: str | None = None
    keepalive_seconds: int = 30
    connect_timeout_seconds: float = 10.0
    publish_timeout_seconds: float = 10.0

    def __post_init__(self):
        if not self.host or not isinstance(self.port, int) or not 1 <= self.port <= 65535:
            raise MqttConfigurationError("broker host or port is invalid")
        if not self.client_id or any(character in self.client_id for character in "/+#"):
            raise MqttConfigurationError("client_id is invalid")
        password_auth = bool(self.username) and self.password is not None
        mutual_tls = bool(self.client_cert) and bool(self.client_key)
        if bool(self.client_cert) != bool(self.client_key):
            raise MqttConfigurationError("client_cert and client_key must be configured together")
        if not password_auth and not mutual_tls:
            raise MqttConfigurationError("anonymous MQTT connections are disabled")
        if not _is_loopback(self.host) and not self.ca_cert:
            raise MqttConfigurationError("remote MQTT connections require a trusted CA certificate")
        if self.keepalive_seconds < 10 or self.connect_timeout_seconds <= 0 or self.publish_timeout_seconds <= 0:
            raise MqttConfigurationError("MQTT timeout settings are invalid")

    @property
    def tls_enabled(self) -> bool:
        return bool(self.ca_cert)


def _paho_client_factory(config: MqttConfig):
    try:
        import paho.mqtt.client as mqtt
    except ImportError as error:
        raise MqttConfigurationError(
            "paho-mqtt is not installed; install requirements-production.txt"
        ) from error
    return mqtt.Client(
        callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
        client_id=config.client_id,
        protocol=mqtt.MQTTv5,
        reconnect_on_failure=True,
    )


class MqttUplink:
    def __init__(self, config: MqttConfig, spool, *, client_factory=None):
        self.config = config
        self.spool = spool
        self.client = (client_factory or _paho_client_factory)(config)
        self._connack = threading.Event()
        self._connection_error = None
        self._publish_condition = threading.Condition()
        self._publish_results = {}
        self.client.on_connect = self._on_connect
        self.client.on_publish = self._on_publish
        if config.username:
            self.client.username_pw_set(config.username, config.password)
        if config.tls_enabled:
            self.client.tls_set(
                ca_certs=str(Path(config.ca_cert)),
                certfile=str(Path(config.client_cert)) if config.client_cert else None,
                keyfile=str(Path(config.client_key)) if config.client_key else None,
                cert_reqs=ssl.CERT_REQUIRED,
                tls_version=ssl.PROTOCOL_TLS_CLIENT,
            )

    def _on_connect(self, _client, _userdata, _flags, reason_code, _properties=None):
        if getattr(reason_code, "is_failure", False):
            self._connection_error = f"broker rejected connection: {reason_code}"
        else:
            self._connection_error = None
        self._connack.set()

    def _on_publish(self, _client, _userdata, mid, reason_code, _properties=None):
        reason_text = str(reason_code)
        error = (
            reason_text
            if getattr(reason_code, "is_failure", False) or reason_text == "No matching subscribers"
            else None
        )
        with self._publish_condition:
            self._publish_results[mid] = error
            self._publish_condition.notify_all()

    def publish_pending(self, *, now: float | None = None, limit: int = 50) -> dict[str, int]:
        published = failed = 0
        connected = False
        transport_open = False
        loop_started = False
        try:
            self._connack.clear()
            self._connection_error = None
            try:
                result = self.client.connect(
                    self.config.host, self.config.port, self.config.keepalive_seconds
                )
            except Exception as error:
                raise MqttPublishError(f"broker connection failed: {error}") from error
            if result not in (0, None):
                raise MqttPublishError(f"broker connection failed with code {result}")
            transport_open = True
            self.client.loop_start()
            loop_started = True
            if not self._connack.wait(self.config.connect_timeout_seconds):
                raise MqttPublishError("broker did not return CONNACK before the timeout")
            if self._connection_error:
                raise MqttPublishError(self._connection_error)
            connected = True
            # Claim one record at a time. If the connection fails mid-batch,
            # later records remain immediately available instead of waiting for
            # an in-flight lease they never actually used.
            for _index in range(limit):
                claimed = self.spool.claim(now=now, limit=1)
                if not claimed:
                    break
                message = claimed[0]
                try:
                    info = self.client.publish(
                        message.topic, message.payload, qos=message.qos, retain=False
                    )
                    if getattr(info, "rc", 0) != 0:
                        raise MqttPublishError(f"publish rejected with code {info.rc}")
                    info.wait_for_publish(timeout=self.config.publish_timeout_seconds)
                    if not info.is_published():
                        raise MqttPublishError("PUBACK was not received before the timeout")
                    deadline = time.monotonic() + self.config.publish_timeout_seconds
                    with self._publish_condition:
                        while info.mid not in self._publish_results:
                            remaining = deadline - time.monotonic()
                            if remaining <= 0:
                                raise MqttPublishError(
                                    "PUBACK result was not delivered before the timeout"
                                )
                            self._publish_condition.wait(remaining)
                        publish_error = self._publish_results.pop(info.mid)
                    if publish_error:
                        raise MqttPublishError(f"broker rejected publish: {publish_error}")
                    if not self.spool.ack(message.message_id, now=now):
                        raise MqttPublishError("spool lease was lost before acknowledgement")
                    published += 1
                except Exception as error:
                    failed += 1
                    self.spool.fail(message.message_id, str(error), now=now)
                    if isinstance(error, MqttPublishError):
                        raise
                    raise MqttPublishError(str(error)) from error
        finally:
            if transport_open:
                try:
                    self.client.disconnect()
                finally:
                    if loop_started:
                        self.client.loop_stop()
        return {"published": published, "failed": failed}
