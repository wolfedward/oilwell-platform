"""Gateway cycle orchestration tests."""

from datetime import datetime, timezone
import json
from pathlib import Path
import tempfile
import unittest

from backend.gateway.config import GatewayConfig
from backend.gateway.contracts import PointDefinition, Quality, TelemetrySample
from backend.gateway.mqtt_uplink import MqttConfig, MqttPublishError
from backend.gateway.poller import ModbusDevice, PollError, PollResult
from backend.gateway.runtime import GatewayRuntime


NOW = datetime(2026, 9, 12, 9, 0, tzinfo=timezone.utc)


class FakePoller:
    def __init__(self, result):
        self.result = result

    def poll(self, *, sequence):
        return PollResult(
            self.result.device_id,
            self.result.measured_at,
            sequence,
            tuple(
                TelemetrySample(
                    item.gateway_id, item.device_id, item.point_code, item.measured_at,
                    sequence, item.value, item.quality, item.unit, item.source,
                )
                for item in self.result.samples
            ),
            self.result.errors,
        )


class OfflineUplink:
    def __init__(self, _config, _spool):
        pass

    def publish_pending(self):
        raise MqttPublishError("broker offline")


class GatewayRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        root = Path(self.temporary.name)
        point = PointDefinition("motor.current", 1, 4, 10, "float32", unit="A")
        self.device = ModbusDevice("D-001", "tcp", (point,), host="192.0.2.10")
        self.config = GatewayConfig(
            gateway_id="GW-001",
            spool_database=root / "uplink.db",
            poll_interval_seconds=5,
            topic_prefix="oilfield",
            mqtt=MqttConfig(
                "127.0.0.1", 1883, "GW-001", username="gateway", password="secret"
            ),
            devices=(self.device,),
        )

    def test_cycle_spools_canonical_batch_and_preserves_it_when_broker_is_offline(self):
        sample = TelemetrySample(
            "GW-001", "D-001", "motor.current", NOW, 0, 12.5,
            Quality.GOOD, "A", "modbus-tcp",
        )
        poll_result = PollResult("D-001", NOW, 0, (sample,), ())
        runtime = GatewayRuntime(
            self.config,
            boot_id="boot-test",
            poller_factory=lambda _gateway, _device: FakePoller(poll_result),
            uplink_factory=OfflineUplink,
        )
        self.addCleanup(runtime.close)

        result = runtime.run_cycle()
        self.assertEqual((result.devices_polled, result.samples_collected, result.messages_enqueued), (1, 1, 1))
        self.assertEqual(result.uplink_error, "broker offline")
        self.assertEqual(runtime.spool.counts()["pending"], 1)
        message = runtime.spool.claim(now=10**12)[0]
        body = json.loads(message.payload)
        self.assertEqual(message.topic, "oilfield/v1/GW-001/D-001/telemetry")
        self.assertEqual(body["boot_id"], "boot-test")
        self.assertEqual(body["samples"][0]["sequence"], 1)

    def test_poll_errors_are_reported_without_fabricating_a_telemetry_batch(self):
        poll_result = PollResult(
            "D-001", NOW, 0, (),
            (PollError("connection_failed", "offline", ("motor.current",)),),
        )
        runtime = GatewayRuntime(
            self.config,
            poller_factory=lambda _gateway, _device: FakePoller(poll_result),
            uplink_factory=OfflineUplink,
        )
        self.addCleanup(runtime.close)
        result = runtime.run_cycle()
        self.assertEqual(result.samples_collected, 0)
        self.assertEqual(result.messages_enqueued, 0)
        self.assertEqual(result.poll_errors[0].code, "connection_failed")
        self.assertEqual(runtime.spool.counts()["pending"], 0)


if __name__ == "__main__":
    unittest.main()
