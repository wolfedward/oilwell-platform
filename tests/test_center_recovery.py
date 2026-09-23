"""Service-level recovery tests: network failures must not end ingestion."""

from types import SimpleNamespace
import unittest
from unittest.mock import patch

from backend.center import __main__ as center_main
from backend.gateway.mqtt_uplink import MqttConfig


class ScriptedClient:
    """Replace only blocking transport calls; exercise the real entry point."""

    def __init__(self, *, failures=0, returned_loops=0, subscription_result=1):
        self.failures = failures
        self.returned_loops = returned_loops
        self.subscription_result = subscription_result
        self.connections = []
        self.loop_calls = 0
        self.disconnects = 0
        self.rejection_disconnected = False
        self.on_subscribe = None

    def connect(self, host, port, keepalive, *, clean_start, properties):
        self.connections.append((clean_start, properties.SessionExpiryInterval))
        if len(self.connections) <= self.failures:
            raise ConnectionRefusedError("transport unavailable password=must-not-be-logged")
        return 0

    def subscribe(self, topic, qos):
        return 0, 17

    def loop_forever(self, **_kwargs):
        self.loop_calls += 1
        self.on_connect(self, None, None, SimpleNamespace(is_failure=False), None)
        if self.on_subscribe:
            before = self.disconnects
            self.on_subscribe(self, None, 17, [SimpleNamespace(value=self.subscription_result)], None)
            if self.subscription_result != 1:
                self.rejection_disconnected = self.disconnects > before
        if self.loop_calls <= self.returned_loops:
            return 0  # Paho returns after the ingest callback calls disconnect().
        raise KeyboardInterrupt

    def disconnect(self):
        self.disconnects += 1
        return 0


class CentralRecoveryTests(unittest.TestCase):
    def run_service(self, client):
        config = SimpleNamespace(
            mqtt=MqttConfig(host="127.0.0.1", port=18884, client_id="recovery-test",
                            username="test", password="test-secret"),
            mqtt_session_expiry_seconds=604800, topic_prefix="oilfield",
            postgres={"host": "127.0.0.1"},
        )
        events, delays = [], []
        with patch.object(center_main, "load_center_config", return_value=config), \
                patch.object(center_main, "create_paho_client", return_value=client), \
                patch.object(center_main, "_event", side_effect=lambda name, **fields: events.append((name, fields))), \
                patch("time.sleep", side_effect=delays.append):
            result = center_main.main(["--config", "test-only.toml"])
        return result, events, delays

    def test_broker_unavailable_at_start_is_retried_without_losing_persistent_session(self):
        client = ScriptedClient(failures=2)
        result, events, delays = self.run_service(client)
        self.assertEqual(result, 0)
        self.assertEqual(client.connections, [(False, 604800)] * 3)
        self.assertEqual(delays, [1, 2])
        self.assertNotIn("must-not-be-logged", str(events))

    def test_ingest_triggered_disconnect_resumes_receiving_instead_of_exiting(self):
        client = ScriptedClient(returned_loops=1)
        result, _events, delays = self.run_service(client)
        self.assertEqual(result, 0)
        self.assertEqual(client.loop_calls, 2, "an application disconnect must not terminate the service")
        self.assertEqual(client.connections, [(False, 604800)] * 2)
        self.assertEqual(delays, [1])

    def test_repeated_fast_failures_back_off_with_a_bounded_delay(self):
        client = ScriptedClient(returned_loops=8)
        _result, _events, delays = self.run_service(client)
        self.assertEqual(delays, [1, 2, 4, 8, 16, 30, 30, 30])

    def test_subscription_rejection_is_not_reported_as_ready(self):
        client = ScriptedClient(subscription_result=128, returned_loops=1)
        _result, events, delays = self.run_service(client)
        names = [name for name, _fields in events]
        self.assertNotIn("mqtt_subscribed", names)
        self.assertIn("mqtt_subscribe_rejected", names)
        self.assertTrue(client.rejection_disconnected, "a rejected subscription must close its connection")
        self.assertEqual(len(client.connections), 2)
        self.assertEqual(delays, [1])


if __name__ == "__main__":
    unittest.main()
