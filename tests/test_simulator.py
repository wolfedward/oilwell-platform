"""Protocol, virtual-terminal, and server-owned simulator regression tests."""

from datetime import datetime, timedelta, timezone
import unittest

from backend.protocol import (
    FrameChecksumError,
    FrameFormatError,
    decode_frame,
    encode_frame,
)
from backend.simulator import VirtualTerminal, profile_for


SHANGHAI = timezone(timedelta(hours=8))


def at(value: str) -> datetime:
    return datetime.fromisoformat(value)


def telemetry(frame_bytes: bytes) -> dict:
    return decode_frame(frame_bytes).payload["telemetry"]


class ProtocolTests(unittest.TestCase):
    def test_frame_round_trip_exposes_strict_header_fields(self):
        stamp = at("2026-06-22T10:00:00+08:00")
        raw = encode_frame("D-001", 7, {"current": 18.4, "stroke": 3.2}, timestamp=stamp)
        frame = decode_frame(raw)
        self.assertEqual(frame.device_id, "D-001")
        self.assertEqual(frame.sequence, 7)
        self.assertEqual(frame.timestamp, stamp)
        self.assertEqual(frame.payload, {"current": 18.4, "stroke": 3.2})
        self.assertEqual(frame.protocol_version, 1)
        self.assertEqual(frame.payload_length, len(b'{"current":18.4,"stroke":3.2}'))
        self.assertEqual(len(frame.checksum), 32)

    def test_frame_rejects_checksum_and_trailing_data(self):
        raw = encode_frame("D-001", 7, {"current": 18.4}, timestamp=at("2026-06-22T10:00:00+08:00"))
        corrupted = bytearray(raw)
        corrupted[-1] ^= 1
        with self.assertRaises(FrameChecksumError):
            decode_frame(bytes(corrupted))
        with self.assertRaises(FrameFormatError):
            decode_frame(raw + b"x")

    def test_default_timestamp_is_aware_shanghai_time(self):
        frame = decode_frame(encode_frame("D-001", 1, {"kind": "heartbeat"}))
        self.assertIsNotNone(frame.timestamp.tzinfo)
        self.assertEqual(frame.timestamp.utcoffset(), timedelta(hours=8))


class VirtualTerminalTests(unittest.TestCase):
    def test_supply_shortage_changes_fillage_not_identity(self):
        terminal = VirtualTerminal(profile_for("D-001"), seed=11)
        normal = terminal.tick(at("2026-06-22T10:00:00+08:00"))
        terminal.apply_scenario("supply_shortage", 30)
        abnormal = terminal.tick(at("2026-06-22T10:01:00+08:00"))
        self.assertLess(telemetry(abnormal[1])["fillage"], telemetry(normal[1])["fillage"])
        self.assertEqual(decode_frame(abnormal[0]).device_id, "D-001")

    def test_all_scenarios_are_deterministic_and_expire(self):
        now = at("2026-06-22T10:00:00+08:00")
        for scenario in ("normal", "supply_shortage", "pump_leak", "empty_pump", "sensor_drift", "packet_loss", "offline"):
            with self.subTest(scenario=scenario):
                first, second = (VirtualTerminal(profile_for("D-001"), seed=11) for _ in range(2))
                first.apply_scenario(scenario, 1)
                second.apply_scenario(scenario, 1)
                self.assertEqual(first.tick(now), second.tick(now))
                first.tick(now + timedelta(minutes=2))
                self.assertEqual(first.scenario.name, "normal")

    def test_packet_loss_consumes_sequence_and_offline_emits_no_business_frame(self):
        now = at("2026-06-22T10:00:00+08:00")
        terminal = VirtualTerminal(profile_for("D-001"), seed=11)
        first = terminal.tick(now)
        terminal.apply_scenario("packet_loss", 10)
        lost = terminal.tick(now + timedelta(minutes=1))
        after = terminal.tick(now + timedelta(minutes=2))
        self.assertLess(len(lost), len(first))
        self.assertGreater(decode_frame(after[0]).sequence, decode_frame(first[-1]).sequence + 1)
        terminal.apply_scenario("offline", 10)
        self.assertEqual(terminal.tick(now + timedelta(minutes=3)), [])
        self.assertEqual(terminal.state, "offline")


if __name__ == "__main__":
    unittest.main()
