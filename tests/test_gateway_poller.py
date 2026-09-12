"""Hardware-independent Modbus polling tests."""

from datetime import datetime, timezone
import unittest

from backend.gateway.contracts import PointDefinition, Quality
from backend.gateway.poller import ModbusDevice, ModbusPoller


NOW = datetime(2026, 9, 12, 8, 0, tzinfo=timezone.utc)


class FakeResponse:
    def __init__(self, registers=(), *, error=False):
        self.registers = list(registers)
        self._error = error

    def isError(self):
        return self._error


class FakeClient:
    def __init__(self, responses, *, connects=True):
        self.responses = list(responses)
        self.connects = connects
        self.calls = []

    def connect(self):
        self.calls.append(("connect",))
        return self.connects

    def close(self):
        self.calls.append(("close",))

    def read_holding_registers(self, address, *, count, device_id):
        self.calls.append(("holding", address, count, device_id))
        return self.responses.pop(0)

    def read_input_registers(self, address, *, count, device_id):
        self.calls.append(("input", address, count, device_id))
        return self.responses.pop(0)


class ModbusPollerTests(unittest.TestCase):
    def setUp(self):
        self.device = ModbusDevice(
            device_id="D-001",
            transport="tcp",
            host="192.0.2.10",
            port=502,
            timeout_seconds=2,
            points=(
                PointDefinition("motor.current", 1, 4, 10, "float32", unit="A"),
                PointDefinition("motor.speed", 1, 3, 20, "uint16", scale=0.1, unit="rpm"),
            ),
        )

    def test_poll_uses_correct_functions_and_returns_timestamped_samples(self):
        client = FakeClient([FakeResponse([12345]), FakeResponse([0x4148, 0])])
        poller = ModbusPoller(
            "GW-001", self.device, client_factory=lambda _device: client, clock=lambda: NOW
        )
        result = poller.poll(sequence=7)
        self.assertEqual(client.calls, [
            ("connect",), ("holding", 20, 1, 1), ("input", 10, 2, 1), ("close",),
        ])
        self.assertEqual([sample.point_code for sample in result.samples], ["motor.current", "motor.speed"])
        self.assertEqual([sample.value for sample in result.samples], [12.5, 1234.5])
        self.assertTrue(all(sample.sequence == 7 and sample.measured_at == NOW for sample in result.samples))
        self.assertEqual(result.errors, ())

    def test_protocol_error_is_isolated_and_reported_with_bad_quality(self):
        client = FakeClient([FakeResponse([12345]), FakeResponse(error=True)])
        result = ModbusPoller(
            "GW-001", self.device, client_factory=lambda _device: client, clock=lambda: NOW
        ).poll(sequence=8)
        self.assertEqual([sample.point_code for sample in result.samples], ["motor.speed"])
        self.assertEqual(result.errors[0].quality, Quality.BAD)
        self.assertEqual(result.errors[0].point_codes, ("motor.current",))

    def test_connection_failure_is_reported_for_all_points_and_client_is_closed(self):
        client = FakeClient([], connects=False)
        result = ModbusPoller(
            "GW-001", self.device, client_factory=lambda _device: client, clock=lambda: NOW
        ).poll(sequence=9)
        self.assertEqual(result.samples, ())
        self.assertEqual(result.errors[0].code, "connection_failed")
        self.assertEqual(set(result.errors[0].point_codes), {"motor.current", "motor.speed"})
        self.assertEqual(client.calls, [("connect",), ("close",)])


if __name__ == "__main__":
    unittest.main()
