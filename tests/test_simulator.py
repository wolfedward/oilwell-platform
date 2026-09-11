"""Protocol, virtual-terminal, and server-owned simulator regression tests."""

from datetime import datetime, timedelta, timezone
import hashlib
import math
from pathlib import Path
import struct
import tempfile
import unittest

from backend.protocol import (
    FrameChecksumError,
    FrameError,
    FrameFormatError,
    decode_frame,
    encode_frame,
)
from backend.protocol import MAGIC, PROTOCOL_VERSION
from backend.simulator import VirtualTerminal, profile_for


SHANGHAI = timezone(timedelta(hours=8))


def at(value: str) -> datetime:
    return datetime.fromisoformat(value)


def telemetry(frame_bytes: bytes) -> dict:
    return decode_frame(frame_bytes).payload["telemetry"]


def signed_frame(payload: bytes) -> bytes:
    device, stamp = b"D-001", b"2026-06-22T10:00:00+08:00"
    body = struct.pack("!4sBBQBH", MAGIC, PROTOCOL_VERSION, len(device), 1, len(stamp), len(payload)) + device + stamp + payload
    return body + hashlib.sha256(body).digest()


class ProtocolTests(unittest.TestCase):
    def test_json_attack_inputs_only_raise_frame_errors(self):
        deep = {"leaf": 1}
        for _ in range(65):
            deep = {"nested": deep}
        for payload in ({"value": float("nan")}, {"value": float("inf")}, {"value": "\ud800"}, deep):
            with self.subTest(payload=repr(payload)[:40]):
                with self.assertRaises(FrameError):
                    encode_frame("D-001", 1, payload)
        for payload in (b'{"value":NaN}', b'{"value":"\\ud800"}', b'{"nested":' + b'{"nested":' * 65 + b'1' + b'}' * 66):
            with self.subTest(payload=payload[:24]):
                with self.assertRaises(FrameError):
                    decode_frame(signed_frame(payload))

    def test_payload_over_limit_is_a_frame_error(self):
        with self.assertRaises(FrameError):
            encode_frame("D-001", 1, {"payload": "x" * 65_536})

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
    def test_preview_is_pure_and_plans_terminal_state_and_expiry(self):
        terminal = VirtualTerminal(profile_for("D-001"), seed=11)
        now = at("2026-06-22T10:00:00+08:00")
        terminal.apply_scenario("offline", 5, now=now)

        planned = terminal.preview_scenario("supply_shortage", 30, now=now)

        self.assertEqual(terminal.scenario.name, "offline")
        self.assertEqual(terminal.state, "offline")
        self.assertEqual(planned.terminal_state, "online")
        self.assertEqual(planned.expires_at, now + timedelta(minutes=30))
        terminal.commit_planned_scenario(planned)
        self.assertEqual(terminal.state, planned.terminal_state)

    def test_supply_shortage_changes_fillage_not_identity(self):
        terminal = VirtualTerminal(profile_for("D-001"), seed=11)
        normal = terminal.tick(at("2026-06-22T10:00:00+08:00"))
        terminal.apply_scenario("supply_shortage", 30)
        abnormal = terminal.tick(at("2026-06-22T10:01:00+08:00"))
        self.assertLess(telemetry(abnormal[1])["fillage"], telemetry(normal[1])["fillage"])
        self.assertEqual(decode_frame(abnormal[0]).device_id, "D-001")

    def test_electric_card_uses_a_complete_degree_indexed_cycle(self):
        terminal = VirtualTerminal(profile_for("D-001"), seed=11)
        frames = terminal.tick(at("2026-06-22T10:00:00+08:00"))
        card = next(
            decode_frame(raw).payload for raw in frames
            if decode_frame(raw).payload["kind"] == "electric_card"
        )
        phases = [sample["phase"] for sample in card["electric_samples"]]
        self.assertEqual(phases, list(range(0, 360, 3)))
        self.assertEqual(set(card["electric_samples"][0]), {"phase", "current", "voltage", "power"})

    def test_assigned_card_profiles_and_active_scenario_override(self):
        from backend.algorithms.dynagraph import convert_electric_card
        from backend.services.analysis import AnalysisService

        def area(frame):
            card = decode_frame(frame).payload
            return convert_electric_card(AnalysisService._samples(card), AnalysisService._parameters(card)).features.area_kn_m

        now = at("2026-06-22T10:00:00+08:00")
        areas = []
        for index in range(1, 7):
            terminal = VirtualTerminal(profile_for(f"D-{index:03d}"))
            areas.append(area(terminal.tick(now)[2]))
        self.assertEqual(len({round(value, 1) for value in areas}), 6)
        self.assertGreater(areas[0], 50.0)
        self.assertLess(areas[4], 5.0)
        terminal = VirtualTerminal(profile_for("D-005"))
        terminal.apply_scenario("pump_leak", 1, now=now)
        self.assertGreater(area(terminal.tick(now)[2]), 20.0)
        self.assertLess(area(terminal.tick(now + timedelta(minutes=2))[2]), 5.0)
        terminal.apply_scenario("normal", 1, now=now + timedelta(minutes=2))
        self.assertGreater(area(terminal.tick(now + timedelta(minutes=2, seconds=1))[2]), 50.0)

    def test_live_profile_cards_pass_acquisition_and_signed_power_limits(self):
        """Rejecting regeneration or leaking profile metadata loses every live card."""
        from backend.database import Database
        from backend.seed import seed_database
        from backend.services.acquisition import AcquisitionService

        with tempfile.TemporaryDirectory() as directory:
            database = Database(Path(directory) / "platform.db")
            database.migrate()
            seed_database(database)
            now = at("2026-06-22T10:00:00+08:00")
            service = AcquisitionService(database, clock=lambda: now)
            for index in range(1, 7):
                terminal = VirtualTerminal(profile_for(f"D-{index:03d}"))
                card = terminal.tick(now)[2]
                result = service.ingest(card)
                self.assertTrue(result.accepted, result)
                payload = decode_frame(card).payload
                self.assertEqual(set(payload), {"kind", "status", "connection_mode", "hardware_connected", "electric_samples", "mechanism", "profile_code", "profile_label", "sensor_samples"})
                self.assertEqual(len(payload["sensor_samples"]), 120)
                self.assertEqual(payload["mechanism"]["static_rod_load_kn"], 36)
            payload["electric_samples"][0]["power"] = -100.0
            self.assertTrue(service.ingest(encode_frame("D-006", 4, payload, timestamp=now)).accepted)
            payload["electric_samples"][0]["power"] = -100.001
            self.assertFalse(service.ingest(encode_frame("D-006", 5, payload, timestamp=now)).accepted)

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

    def test_sensor_drift_still_biases_the_electric_card(self):
        """Replacing the sample builder must not silently remove sensor fault injection."""
        now = at("2026-06-22T10:00:00+08:00")
        normal = VirtualTerminal(profile_for("D-001"), seed=11)
        drift = VirtualTerminal(profile_for("D-001"), seed=11)
        drift.apply_scenario("sensor_drift", 1, now=now)
        baseline = decode_frame(normal.tick(now)[2]).payload["electric_samples"]
        biased = decode_frame(drift.tick(now)[2]).payload["electric_samples"]
        for actual, original in zip(biased, baseline):
            self.assertAlmostEqual(actual["power"] - original["power"], .45, places=5)
            self.assertAlmostEqual(abs(actual["power"]), math.sqrt(3) * actual["voltage"] * actual["current"] * .88 / 1000, places=5)
        later = now + timedelta(minutes=2)
        self.assertEqual(normal.tick(later), drift.tick(later))

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
