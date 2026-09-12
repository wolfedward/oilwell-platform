"""Manual MQTT acknowledgement tests for the central ingestor."""

import unittest

from backend.center.mqtt_consumer import MqttConsumer, MqttConsumerError


class FakeService:
    def __init__(self, error=None):
        self.error = error
        self.calls = []

    def ingest(self, topic, payload):
        self.calls.append((topic, payload))
        if self.error:
            raise self.error
        return object()


class FakeClient:
    def __init__(self, ack_result=0):
        self.ack_result = ack_result
        self.acks = []

    def ack(self, mid, qos):
        self.acks.append((mid, qos))
        return self.ack_result


class FakeMessage:
    topic = "oilfield/v1/GW-001/D-001/telemetry"
    payload = b"{}"
    mid = 17
    qos = 1


class MqttConsumerTests(unittest.TestCase):
    def test_database_commit_happens_before_manual_puback(self):
        service = FakeService()
        client = FakeClient()
        consumer = MqttConsumer(service)
        consumer.handle_message(client, FakeMessage())
        self.assertEqual(service.calls, [(FakeMessage.topic, b"{}")])
        self.assertEqual(client.acks, [(17, 1)])

    def test_failed_processing_is_not_acknowledged_so_broker_can_redeliver(self):
        service = FakeService(ValueError("database unavailable"))
        client = FakeClient()
        with self.assertRaisesRegex(ValueError, "database unavailable"):
            MqttConsumer(service).handle_message(client, FakeMessage())
        self.assertEqual(client.acks, [])

    def test_qos_zero_and_ack_failure_are_rejected(self):
        message = FakeMessage()
        message.qos = 0
        with self.assertRaises(MqttConsumerError):
            MqttConsumer(FakeService()).handle_message(FakeClient(), message)
        message.qos = 1
        with self.assertRaises(MqttConsumerError):
            MqttConsumer(FakeService()).handle_message(FakeClient(ack_result=4), message)


if __name__ == "__main__":
    unittest.main()
