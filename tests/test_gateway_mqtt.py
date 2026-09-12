"""Authenticated MQTT QoS 1 uplink tests."""

from pathlib import Path
import tempfile
import unittest

from backend.gateway.mqtt_uplink import MqttConfig, MqttConfigurationError, MqttPublishError, MqttUplink
from backend.gateway.spool import DurableSpool


class FakeInfo:
    def __init__(self, *, published=True, rc=0):
        self.rc = rc
        self._published = published
        self.wait_timeout = None

    def wait_for_publish(self, timeout=None):
        self.wait_timeout = timeout

    def is_published(self):
        return self._published


class FakeClient:
    def __init__(self, info=None, *, connect_error=None, connack_failure=False):
        self.info = info or FakeInfo()
        self.connect_error = connect_error
        self.connack_failure = connack_failure
        self.calls = []
        self.on_connect = None

    def username_pw_set(self, username, password):
        self.calls.append(("credentials", username, password))

    def tls_set(self, **kwargs):
        self.calls.append(("tls", kwargs))

    def connect(self, host, port, keepalive):
        self.calls.append(("connect", host, port, keepalive))
        if self.connect_error:
            raise self.connect_error
        return 0

    def loop_start(self):
        self.calls.append(("loop_start",))
        reason = type("Reason", (), {
            "is_failure": self.connack_failure,
            "__str__": lambda self: "Not authorized" if self.is_failure else "Success",
        })()
        self.on_connect(self, None, None, reason, None)

    def loop_stop(self):
        self.calls.append(("loop_stop",))

    def disconnect(self):
        self.calls.append(("disconnect",))

    def publish(self, topic, payload, qos, retain):
        self.calls.append(("publish", topic, payload, qos, retain))
        return self.info


class MqttConfigurationTests(unittest.TestCase):
    def test_remote_broker_requires_tls_and_authentication(self):
        with self.assertRaises(MqttConfigurationError):
            MqttConfig(host="broker.example.com", port=1883, client_id="GW-001")
        with self.assertRaises(MqttConfigurationError):
            MqttConfig(
                host="broker.example.com", port=8883, client_id="GW-001",
                ca_cert="ca.pem",
            )
        config = MqttConfig(
            host="broker.example.com", port=8883, client_id="GW-001",
            username="gateway", password="secret", ca_cert="ca.pem",
        )
        self.assertTrue(config.tls_enabled)

    def test_local_development_still_requires_explicit_credentials(self):
        with self.assertRaises(MqttConfigurationError):
            MqttConfig(host="127.0.0.1", port=1883, client_id="GW-001")


class MqttUplinkTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.spool = DurableSpool(Path(self.temporary.name) / "uplink.db")
        self.addCleanup(self.spool.close)
        self.spool.enqueue(
            "msg-1", "oilfield/v1/GW-001/D-001/telemetry", b"payload", now=0.0
        )
        self.config = MqttConfig(
            host="127.0.0.1", port=1883, client_id="GW-001",
            username="gateway", password="secret", publish_timeout_seconds=3,
        )

    def test_puback_is_required_before_spool_ack_and_telemetry_is_not_retained(self):
        client = FakeClient()
        uplink = MqttUplink(self.config, self.spool, client_factory=lambda _config: client)
        result = uplink.publish_pending(now=10.0)
        publish = next(call for call in client.calls if call[0] == "publish")
        self.assertEqual(publish[1:], ("oilfield/v1/GW-001/D-001/telemetry", b"payload", 1, False))
        self.assertEqual(result, {"published": 1, "failed": 0})
        self.assertEqual(self.spool.counts()["acked"], 1)

    def test_missing_puback_keeps_message_for_retry(self):
        client = FakeClient(FakeInfo(published=False))
        uplink = MqttUplink(self.config, self.spool, client_factory=lambda _config: client)
        with self.assertRaises(MqttPublishError):
            uplink.publish_pending(now=10.0)
        self.assertEqual(self.spool.counts()["pending"], 1)

    def test_connection_failure_does_not_claim_pending_messages(self):
        client = FakeClient(connect_error=OSError("refused"))
        uplink = MqttUplink(self.config, self.spool, client_factory=lambda _config: client)
        with self.assertRaisesRegex(MqttPublishError, "broker connection failed"):
            uplink.publish_pending(now=10.0)
        self.assertEqual(self.spool.counts(), {"pending": 1, "inflight": 0, "acked": 0, "dead": 0})

    def test_rejected_credentials_are_reported_before_any_message_is_claimed(self):
        client = FakeClient(connack_failure=True)
        uplink = MqttUplink(self.config, self.spool, client_factory=lambda _config: client)
        with self.assertRaisesRegex(MqttPublishError, "Not authorized"):
            uplink.publish_pending(now=10.0)
        self.assertFalse(any(call[0] == "publish" for call in client.calls))
        self.assertEqual(self.spool.counts()["pending"], 1)


if __name__ == "__main__":
    unittest.main()
