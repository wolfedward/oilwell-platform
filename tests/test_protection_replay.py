import copy
import unittest

from backend.studies.contracts import StudyError
from backend.studies.protection import ProtectionReplay, replay_protection


def settings(delay=1000, safe=500):
    return {
        "version": "fixture-1", "basis_reference": "analytical fixture", "approved_by": "fixture-author",
        "effective_from": "2026-10-11T00:00:00+00:00",
        "effective_until": "2026-10-12T00:00:00+00:00",
        "maximum_frame_gap_ms": 2000, "reset_safe_ms": safe,
        "rules": [{"id": "overcurrent", "metric": "current_max_A", "direction": "high",
                   "pickup": 10, "dropout": 8, "delay_ms": delay}],
    }


def frame(ms, amps=0, seq=None, **changes):
    second, remainder = divmod(ms, 1000)
    value = {
        "at": f"2026-10-11T00:00:{second:02d}.{remainder:03d}+00:00",
        "sequence": ms if seq is None else seq, "boot_id": "boot-1", "quality": "valid",
        "configuration_valid": True, "hardware_trip": False, "reset_request": "none",
        "measurements": {"currents_A": [amps, amps, amps], "voltages_V": None,
                         "temperature_C": None, "speed_rpm": None, "frequency_hz": None},
    }
    value.update(changes)
    return value


def replay(frames, config=None):
    return replay_protection({"schema": "motor.protection/1", "settings": config or settings(), "frames": frames})


class ProtectionTests(unittest.TestCase):
    def test_irregular_continuous_elapsed_time_latches_at_delay(self):
        result = replay([frame(0, 10), frame(400, 11), frame(999, 11), frame(1000, 11)])
        self.assertEqual([row["rules"]["overcurrent"]["state"] for row in result["timeline"]],
                         ["pickup", "pickup", "pickup", "would_trip_latched"])
        self.assertFalse(result["hardware_execution"])

    def test_zero_delay_latches_on_first_equal_pickup(self):
        result = replay([frame(0, 10)], settings(delay=0))
        self.assertEqual(result["final_state"]["rules"]["overcurrent"]["state"], "would_trip_latched")

    def test_hysteresis_retains_pickup_until_safe_dropout(self):
        result = replay([frame(0, 10), frame(500, 9), frame(1000, 9)])
        self.assertEqual(result["final_state"]["rules"]["overcurrent"]["state"], "would_trip_latched")

    def test_dropout_before_delay_clears_pickup(self):
        result = replay([frame(0, 10), frame(500, 8), frame(1000, 10), frame(1500, 10)])
        self.assertEqual(result["final_state"]["rules"]["overcurrent"]["state"], "pickup")

    def test_gap_and_invalid_quality_break_elapsed_time(self):
        config = settings(delay=1000)
        config["maximum_frame_gap_ms"] = 500
        result = replay([frame(0, 11), frame(400, 11), frame(1500, 11), frame(1800, 11),
                         frame(2000, 11, quality="invalid"), frame(2400, 11)], config)
        self.assertEqual(result["final_state"]["rules"]["overcurrent"]["state"], "pickup")
        self.assertEqual(result["timeline"][2]["qualification"], "stale")
        self.assertEqual(result["timeline"][4]["qualification"], "invalid")

    def test_gap_frame_starts_fresh_pickup_without_counting_gap(self):
        config = settings(delay=500)
        config["maximum_frame_gap_ms"] = 500
        result = replay([frame(0, 11), frame(1000, 11), frame(1400, 11), frame(1500, 11)], config)
        self.assertEqual(result["timeline"][1]["qualification"], "stale")
        self.assertEqual([row["rules"]["overcurrent"]["state"] for row in result["timeline"]],
                         ["pickup", "pickup", "pickup", "would_trip_latched"])

    def test_reordered_timestamp_cannot_regress_cursor_or_bridge_elapsed(self):
        result = replay([frame(0, 11, seq=1), frame(900, 11, seq=2), frame(800, 11, seq=3),
                         frame(1100, 11, seq=4), frame(1500, 11, seq=5)])
        self.assertEqual(result["timeline"][2]["qualification"], "invalid")
        self.assertEqual(result["final_state"]["rules"]["overcurrent"]["state"], "pickup")

    def test_boot_change_clears_pickup_but_preserves_latch(self):
        result = replay([frame(0, 11), frame(1000, 11), frame(1200, 11, boot_id="boot-2")])
        self.assertEqual(result["final_state"]["rules"]["overcurrent"]["state"], "would_trip_latched")

    def test_new_boot_sequence_zero_starts_fresh_pickup_timing(self):
        result = replay([frame(0, 11, seq=100), frame(500, 11, seq=101),
                         frame(700, 11, seq=0, boot_id="boot-2"),
                         frame(1200, 11, seq=1, boot_id="boot-2"),
                         frame(1700, 11, seq=2, boot_id="boot-2")])
        self.assertEqual([row["qualification"] for row in result["timeline"]],
                         ["valid", "valid", "stale", "valid", "valid"])
        self.assertEqual([row["rules"]["overcurrent"]["state"] for row in result["timeline"]],
                         ["pickup", "pickup", "pickup", "pickup", "would_trip_latched"])

    def test_new_boot_retains_latch_and_requires_new_safe_reset_interval(self):
        result = replay([frame(0, 11, seq=100), frame(1000, 11, seq=101),
                         frame(1200, 8, seq=0, boot_id="boot-2", reset_request="local"),
                         frame(1500, 8, seq=1, boot_id="boot-2", reset_request="local"),
                         frame(1700, 8, seq=2, boot_id="boot-2", reset_request="local")])
        self.assertEqual([row["qualification"] for row in result["timeline"][2:]],
                         ["stale", "valid", "valid"])
        self.assertEqual([row["reset_decision"] for row in result["timeline"][2:]],
                         ["denied", "denied", "accepted"])
        self.assertEqual(result["timeline"][3]["rules"]["overcurrent"]["state"],
                         "would_trip_latched")
        self.assertEqual(result["final_state"]["rules"]["overcurrent"]["state"], "normal")

    def test_old_boot_timestamp_cannot_regress_new_boot_cursor_or_resume_timer(self):
        engine = ProtectionReplay(settings())
        rows = [engine.step(frame(0, 11, seq=100)),
                engine.step(frame(400, 11, seq=0, boot_id="boot-2")),
                engine.step(frame(300, 11, seq=101))]
        cursor_after_replay = engine.snapshot()
        rows.extend([engine.step(frame(900, 11, seq=1, boot_id="boot-2")),
                     engine.step(frame(1400, 11, seq=2, boot_id="boot-2")),
                     engine.step(frame(1900, 11, seq=3, boot_id="boot-2"))])
        self.assertEqual([row["qualification"] for row in rows],
                         ["valid", "stale", "invalid", "valid", "valid", "valid"])
        self.assertIn("reordered_frame", rows[2]["reasons"])
        self.assertEqual(cursor_after_replay["cursor_boot"], "boot-2")
        self.assertEqual(cursor_after_replay["cursor_sequence"], 0)
        self.assertEqual(cursor_after_replay["cursor_at"], "2026-10-11T00:00:00.400000+00:00")
        self.assertEqual([row["rules"]["overcurrent"]["state"] for row in rows[3:]],
                         ["pickup", "pickup", "would_trip_latched"])
        self.assertEqual(engine.snapshot()["cursor_boot"], "boot-2")
        self.assertEqual(engine.snapshot()["cursor_sequence"], 3)

    def test_revoked_and_expired_settings_are_unavailable(self):
        result = replay([frame(0, 11), frame(1000, 11, configuration_valid=False), frame(2000, 11)])
        self.assertEqual(result["timeline"][1]["qualification"], "config_revoked")
        self.assertEqual(result["final_state"]["rules"]["overcurrent"]["state"], "pickup")
        expired = settings()
        expired["effective_until"] = "2026-10-11T00:00:01+00:00"
        result = replay([frame(1000, 11)], expired)
        self.assertEqual(result["timeline"][0]["qualification"], "unconfigured")

    def test_missing_required_phase_invalidates_whole_frame(self):
        bad = frame(500, 11)
        bad["measurements"]["currents_A"] = None
        result = replay([frame(0, 11), bad, frame(1000, 11)])
        self.assertEqual(result["timeline"][1]["qualification"], "invalid")
        self.assertEqual(result["final_state"]["rules"]["overcurrent"]["state"], "pickup")

    def test_local_reset_requires_continuous_safe_interval_and_no_fault(self):
        result = replay([frame(0, 11), frame(1000, 11), frame(1200, 11, reset_request="local"),
                         frame(1500, 8), frame(1900, 8, reset_request="local"),
                         frame(2000, 8, reset_request="local")])
        self.assertEqual(result["timeline"][2]["reset_decision"], "denied")
        self.assertEqual(result["timeline"][4]["reset_decision"], "denied")
        self.assertEqual(result["timeline"][5]["reset_decision"], "accepted")
        self.assertEqual(result["final_state"]["rules"]["overcurrent"]["state"], "normal")

    def test_hardware_trip_latches_even_when_quality_invalid(self):
        result = replay([frame(0, 0, quality="invalid", hardware_trip=True), frame(500, 0)])
        self.assertTrue(result["final_state"]["hardware_inhibit"])
        self.assertFalse(result["control_eligible"])

    def test_reusable_engine_matches_batch_replay(self):
        rows = [frame(0, 11), frame(500, 11), frame(1000, 11)]
        engine = ProtectionReplay(settings())
        stepped = [engine.step(item) for item in rows]
        self.assertEqual(stepped, replay(rows)["timeline"])

    def test_snapshot_restores_latch_and_cursor_for_adapter_restart(self):
        first = ProtectionReplay(settings())
        first.step(frame(0, 11))
        first.step(frame(1000, 11))
        resumed = ProtectionReplay(settings())
        resumed.restore(first.snapshot())
        row = resumed.step(frame(1500, 11, boot_id="boot-2"))
        self.assertEqual(row["rules"]["overcurrent"]["state"], "would_trip_latched")

    def test_snapshot_refuses_corruption_or_other_settings(self):
        engine = ProtectionReplay(settings())
        engine.step(frame(0, 11))
        snapshot = engine.snapshot()
        corrupt = copy.deepcopy(snapshot)
        corrupt["hardware_inhibit"] = "false"
        with self.assertRaises(StudyError):
            ProtectionReplay(settings()).restore(corrupt)
        corrupt = copy.deepcopy(snapshot)
        corrupt["settings_digest"] = True
        with self.assertRaises(StudyError):
            ProtectionReplay(settings()).restore(corrupt)
        different = settings(delay=0)
        with self.assertRaises(StudyError):
            ProtectionReplay(different).restore(snapshot)
        inconsistent = copy.deepcopy(snapshot)
        inconsistent["frame_index"] = 0
        with self.assertRaises(StudyError):
            ProtectionReplay(settings()).restore(inconsistent)

    def test_empty_snapshot_cannot_claim_a_latch(self):
        empty = ProtectionReplay(settings()).snapshot()
        empty["rules"]["overcurrent"] = "would_trip_latched"
        with self.assertRaises(StudyError):
            ProtectionReplay(settings()).restore(empty)

    def test_restore_discards_nonmature_pickup(self):
        engine = ProtectionReplay(settings())
        engine.step(frame(0, 11))
        engine.step(frame(500, 11))
        resumed = ProtectionReplay(settings())
        resumed.restore(engine.snapshot())
        self.assertEqual(resumed.step(frame(1000, 11))["rules"]["overcurrent"]["state"], "pickup")
        self.assertEqual(resumed.step(frame(1500, 11))["rules"]["overcurrent"]["state"], "pickup")

    def test_restore_discards_safe_reset_timer(self):
        engine = ProtectionReplay(settings())
        engine.step(frame(0, 11))
        engine.step(frame(1000, 11))
        engine.step(frame(1200, 8))
        resumed = ProtectionReplay(settings())
        resumed.restore(engine.snapshot())
        self.assertEqual(resumed.step(frame(1500, 8, reset_request="local"))["reset_decision"], "denied")
        self.assertEqual(resumed.step(frame(2000, 8, reset_request="local"))["reset_decision"], "accepted")

    def test_low_voltage_and_deviation_use_all_three_phases(self):
        config = settings(delay=0)
        config["rules"] = [
            {"id": "under_voltage", "metric": "voltage_min_V", "direction": "low",
             "pickup": 200, "dropout": 220, "delay_ms": 0},
            {"id": "unbalance", "metric": "current_deviation_ratio", "direction": "high",
             "pickup": 0.2, "dropout": 0.1, "delay_ms": 0},
        ]
        sample = frame(0, 0)
        sample["measurements"]["currents_A"] = [10, 10, 20]
        sample["measurements"]["voltages_V"] = [230, 199, 230]
        result = replay([sample], config)
        self.assertEqual(result["final_state"]["rules"]["under_voltage"]["state"], "would_trip_latched")
        self.assertEqual(result["final_state"]["rules"]["unbalance"]["state"], "would_trip_latched")

    def test_zero_phase_mean_does_not_become_zero_deviation(self):
        config = settings(delay=0)
        config["rules"][0].update(metric="current_deviation_ratio", pickup=0.2, dropout=0.1)
        result = replay([frame(0, 0)], config)
        self.assertEqual(result["timeline"][0]["qualification"], "invalid")

    def test_settings_cannot_change_after_engine_creation(self):
        config = settings(delay=1000)
        engine = ProtectionReplay(config)
        config["rules"][0]["delay_ms"] = 0
        self.assertEqual(engine.step(frame(0, 11))["rules"]["overcurrent"]["state"], "pickup")

    def test_missing_settings_and_malformed_values_rejected(self):
        with self.assertRaises(StudyError):
            replay_protection({"schema": "motor.protection/1", "frames": [frame(0)]})
        invalid = settings()
        invalid["rules"][0]["pickup"] = 10**400
        with self.assertRaises(StudyError):
            replay([frame(0)], invalid)
        invalid = settings()
        invalid["rules"][0]["dropout"] = 11
        with self.assertRaises(StudyError):
            replay([frame(0)], invalid)


if __name__ == "__main__":
    unittest.main()
