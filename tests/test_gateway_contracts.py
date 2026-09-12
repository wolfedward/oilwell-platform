"""Production gateway data-contract tests."""

from datetime import datetime, timezone
import json
import math
import unittest

from backend.gateway.contracts import (
    ContractValidationError,
    PointDefinition,
    Quality,
    TelemetrySample,
    UplinkBatch,
)


class PointDefinitionTests(unittest.TestCase):
    def test_definition_exposes_register_width_and_rejects_unsafe_maps(self):
        point = PointDefinition(
            point_code="motor.active_power",
            unit_id=1,
            function_code=3,
            address=120,
            data_type="float32",
            scale=0.1,
            unit="kW",
        )
        self.assertEqual(point.register_width, 2)

        invalid = (
            {"unit_id": 0},
            {"function_code": 6},
            {"address": 65535, "data_type": "float32"},
            {"word_order": "sideways"},
            {"scale": math.inf},
            {"writable": True},
        )
        for overrides in invalid:
            values = {
                "point_code": "motor.current",
                "unit_id": 1,
                "function_code": 4,
                "address": 10,
                "data_type": "uint16",
            }
            values.update(overrides)
            with self.subTest(overrides=overrides), self.assertRaises(ContractValidationError):
                PointDefinition(**values)

    def test_point_codes_cannot_escape_mqtt_topic_or_metric_namespace(self):
        for code in ("", "motor/current", "motor+#", " motor.current"):
            with self.subTest(code=code), self.assertRaises(ContractValidationError):
                PointDefinition(code, 1, 3, 0, "uint16")


class UplinkContractTests(unittest.TestCase):
    def sample(self, *, sequence=10, value=12.5):
        return TelemetrySample(
            gateway_id="GW-001",
            device_id="D-001",
            point_code="motor.current",
            measured_at=datetime(2026, 9, 12, 7, 30, tzinfo=timezone.utc),
            sequence=sequence,
            value=value,
            quality=Quality.GOOD,
            unit="A",
            source="modbus-tcp",
        )

    def test_sample_requires_utc_aware_time_and_finite_value(self):
        with self.assertRaises(ContractValidationError):
            TelemetrySample(
                "GW-001", "D-001", "motor.current",
                datetime(2026, 9, 12, 7, 30), 1, 1.0,
            )
        with self.assertRaises(ContractValidationError):
            self.sample(value=float("nan"))

    def test_batch_has_stable_schema_identity_and_canonical_json(self):
        samples = (self.sample(sequence=10), self.sample(sequence=11, value=12.8))
        batch = UplinkBatch.create(
            gateway_id="GW-001",
            device_id="D-001",
            boot_id="boot-a",
            samples=samples,
            message_id="018f-test-message",
            created_at=datetime(2026, 9, 12, 7, 31, tzinfo=timezone.utc),
        )
        body = json.loads(batch.to_json())
        self.assertEqual(body["schema"], "oilwell.telemetry.v1")
        self.assertEqual(body["sequence"], {"first": 10, "last": 11})
        self.assertEqual(body["samples"][0]["quality"], "good")
        self.assertEqual(batch.topic("oilfield"), "oilfield/v1/GW-001/D-001/telemetry")
        self.assertEqual(batch.to_json(), batch.to_json())

    def test_batch_rejects_mixed_device_or_non_monotonic_samples(self):
        other = TelemetrySample(
            "GW-001", "D-002", "motor.current",
            datetime(2026, 9, 12, 7, 30, tzinfo=timezone.utc), 11, 12.8,
        )
        with self.assertRaises(ContractValidationError):
            UplinkBatch.create("GW-001", "D-001", "boot", (self.sample(), other))
        with self.assertRaises(ContractValidationError):
            UplinkBatch.create(
                "GW-001", "D-001", "boot",
                (self.sample(sequence=11), self.sample(sequence=10)),
            )


if __name__ == "__main__":
    unittest.main()
