import sqlite3
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from backend.database import Database
from backend.seed import SeedSummary, _content_hash, _content_hash_from, _previous_content_hash, _task1_current_v4_content_hash, _upgrade_manifest_hash, seed_database


class SeedDatabaseTests(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.database = Database(Path(self.temporary_directory.name) / "platform.db")
        self.database.migrate()

    def test_seed_creates_consistent_project_history(self):
        """Removing the fixture generator must leave the database without the required history."""
        summary = seed_database(self.database)

        self.assertEqual(summary, SeedSummary(4, 8, 48, 48, 48 * 30 * 24))
        self.assertEqual(self.database.scalar("SELECT COUNT(*) FROM wells WHERE is_low_output = 1"), 16)
        self.assertEqual(self.database.scalar("SELECT COUNT(*) FROM production_daily"), 48 * 90)
        self.assertGreaterEqual(self.database.scalar("SELECT COUNT(*) FROM telemetry"), 48 * 30 * 24)
        self.assertEqual(self.database.scalar("SELECT COUNT(*) FROM alarms"), 16)
        self.assertEqual(self.database.scalar("SELECT COUNT(*) FROM work_orders"), 16)
        self.assertEqual(self.database.scalar("SELECT COUNT(*) FROM energy_strategies"), 16)
        self.assertEqual(self.database.scalar("SELECT COUNT(*) FROM system_events"), 48)
        self.assertEqual(self.database.scalar("SELECT COUNT(*) FROM pragma_foreign_key_check"), 0)

    def test_seed_is_idempotent_deterministic_and_traceable(self):
        """Changing the seed path must not alter generated records or overwrite an existing fixture."""
        first = seed_database(self.database)
        first_daily = self.database.query_one(
            "SELECT daily_fluid FROM production_daily ORDER BY well_id, day LIMIT 1"
        )
        first_rows = self.database.scalar("SELECT COUNT(*) FROM production_daily")

        second = seed_database(self.database)

        self.assertEqual(first, second)
        self.assertEqual(round(first_daily["daily_fluid"], 3), 2.846)
        self.assertEqual(self.database.scalar("SELECT COUNT(*) FROM production_daily"), first_rows)
        self.assertEqual(
            self.database.scalar(
                "SELECT COUNT(*) FROM telemetry WHERE source_type <> 'simulator'"
            ),
            0,
        )
        self.assertEqual(
            self.database.scalar(
                "SELECT COUNT(*) FROM units WHERE created_at NOT LIKE '%+08:00'"
            ),
            0,
        )

    def test_partial_simulator_dataset_is_rejected_instead_of_reported_complete(self):
        """Dropping a generated production day must make the next bootstrap visibly fail."""
        seed_database(self.database)
        with self.database.transaction(None, "test.remove_seed_row", "production_daily", None):
            self.database.execute(
                "DELETE FROM production_daily WHERE well_id = ? AND day = ?",
                ("W-001", "2026-04-02"),
            )
            self.database.execute("DELETE FROM seed_manifests WHERE dataset_name = 'acceptance-history'")

        with self.assertRaisesRegex(RuntimeError, "incomplete"):
            seed_database(self.database)

    def test_complete_legacy_simulator_history_is_adopted_with_a_versioned_manifest(self):
        """A complete pre-manifest history must gain a manifest without replacing its records."""
        seed_database(self.database)
        with self.database.transaction(None, "test.remove_manifest", "seed_manifest", None):
            self.database.execute("DELETE FROM seed_manifests WHERE dataset_name = 'acceptance-history'")

        summary = seed_database(self.database)

        self.assertEqual(summary, SeedSummary(4, 8, 48, 48, 48 * 30 * 24))
        self.assertEqual(self.database.scalar("SELECT COUNT(*) FROM production_daily"), 48 * 90)
        manifest = self.database.query_one(
            "SELECT version, source_type, content_hash FROM seed_manifests WHERE dataset_name = 'acceptance-history'"
        )
        self.assertEqual(tuple(manifest)[:2], (2, "simulator"))
        self.assertRegex(manifest["content_hash"], r"^[0-9a-f]{64}$")

    def test_seed_explicitly_marks_its_atomic_audit_entry_as_simulator(self):
        """Omitting seed's audit source argument must not be masked by its action name."""
        seed_database(self.database)

        self.assertEqual(
            self.database.scalar(
                "SELECT source_type FROM audit_logs WHERE action = 'seed.generate'"
            ),
            "simulator",
        )

    def test_manifest_tampering_is_rejected_before_reusing_seeded_history(self):
        """Changing manifest contract fields must never return a cached-looking summary."""
        for column, value in (
            ("dataset_name", "tampered-history"), ("seed", 1), ("summary_json", '{"devices":999}'), ("status", "tampered"),
            ("source_type", "manual"), ("content_hash", "0" * 64),
        ):
            with self.subTest(column=column):
                database = Database(Path(self.temporary_directory.name) / f"manifest-{column}.db")
                database.migrate()
                seed_database(database)
                with database.transaction(None, "test.tamper_manifest", "seed_manifest", None):
                    database.execute(
                        f"UPDATE seed_manifests SET {column} = ? WHERE dataset_name = 'acceptance-history'",
                        (value,),
                    )

                with self.assertRaisesRegex(RuntimeError, "incomplete"):
                    seed_database(database)

    def test_auxiliary_row_deletion_and_key_value_change_break_content_fingerprint(self):
        """Removing a raw message or altering a daily value must invalidate the manifest fingerprint."""
        for statement, parameters in (
            ("DELETE FROM raw_messages WHERE id = (SELECT MIN(id) FROM raw_messages)", ()),
            ("UPDATE production_daily SET power_kwh = power_kwh + 1 WHERE well_id = ? AND day = ?", ("W-001", "2026-04-02")),
        ):
            with self.subTest(statement=statement):
                self.database = Database(Path(self.temporary_directory.name) / f"tamper-{len(parameters)}.db")
                self.database.migrate()
                seed_database(self.database)
                with self.database.transaction(None, "test.tamper_seed", "seed_data", None):
                    self.database.execute(statement, parameters)

                with self.assertRaisesRegex(RuntimeError, "incomplete"):
                    seed_database(self.database)

    def test_manual_identifier_collision_preserves_business_record_and_seeds_isolated_history(self):
        """A manual U-01/CY-01 must not prevent the seed bootstrap from completing."""
        with self.database.transaction(None, "create", "unit", "U-01"):
            self.database.execute(
                "INSERT INTO units(id, code, name, source_type) VALUES(?, ?, ?, 'manual')",
                ("U-01", "CY-01", "现场人工单元"),
            )

        summary = seed_database(self.database)

        self.assertEqual(summary.wells, 48)
        self.assertEqual(
            self.database.query_one("SELECT name FROM units WHERE id = 'U-01'")["name"],
            "现场人工单元",
        )
        manifest = self.database.query_one(
            "SELECT namespace FROM seed_manifests WHERE dataset_name = 'acceptance-history'"
        )
        self.assertTrue(manifest["namespace"])
        self.assertEqual(self.database.scalar("SELECT COUNT(*) FROM wells WHERE source_type = 'simulator'"), 48)

    def test_each_well_day_reconciles_hourly_power_with_daily_energy_and_runtime(self):
        """Changing daily energy independently of hourly telemetry must be caught for every well-day."""
        seed_database(self.database)

        rows = self.database.query_all(
            """
            SELECT p.well_id, p.day, p.power_kwh, p.runtime_hours,
                   COUNT(t.id) AS samples, SUM(t.value) AS telemetry_kwh,
                   SUM(CASE WHEN t.value > 0 THEN 1 ELSE 0 END) AS running_hours
            FROM production_daily AS p
            JOIN telemetry AS t
              ON t.well_id = p.well_id AND substr(t.measured_at, 1, 10) = p.day
            GROUP BY p.well_id, p.day
            ORDER BY p.well_id, p.day
            """
        )

        self.assertEqual(len(rows), 48 * 30)
        self.assertEqual(
            self.database.scalar(
                "SELECT MIN(days), MAX(days) FROM (SELECT well_id, COUNT(*) AS days FROM production_daily GROUP BY well_id)"
            ),
            90,
        )
        self.assertEqual(
            self.database.scalar(
                "SELECT MIN(hours), MAX(hours) FROM (SELECT well_id, COUNT(*) AS hours FROM telemetry GROUP BY well_id)"
            ),
            720,
        )
        for row in rows:
            self.assertEqual(row["samples"], 24)
            self.assertAlmostEqual(row["telemetry_kwh"], row["power_kwh"], places=2)
            self.assertEqual(row["running_hours"], row["runtime_hours"])

    def test_low_output_workflows_cover_consistent_acceptance_states(self):
        """Collapsing every low-output chain to one state loses required acceptance paths."""
        seed_database(self.database)

        status_sets = {
            "alarms": {row[0] for row in self.database.query_all("SELECT DISTINCT status FROM alarms")},
            "strategies": {row[0] for row in self.database.query_all("SELECT DISTINCT status FROM energy_strategies")},
            "approvals": {row[0] for row in self.database.query_all("SELECT DISTINCT status FROM approvals")},
            "commands": {row[0] for row in self.database.query_all("SELECT DISTINCT status FROM device_commands")},
            "work_orders": {row[0] for row in self.database.query_all("SELECT DISTINCT status FROM work_orders")},
        }
        self.assertTrue({"new", "acknowledged", "dispatched", "closed"}.issubset(status_sets["alarms"]))
        self.assertTrue({"draft", "pending", "active", "effective", "rollback"}.issubset(status_sets["strategies"]))
        self.assertTrue({"pending", "approved", "rejected"}.issubset(status_sets["approvals"]))
        self.assertTrue({"pending", "sent", "succeeded", "failed", "rolled_back"}.issubset(status_sets["commands"]))
        self.assertTrue({"pending", "processing", "completed", "closed"}.issubset(status_sets["work_orders"]))
        self.assertEqual(
            self.database.scalar(
                """
                SELECT COUNT(DISTINCT wo.id)
                FROM work_orders AS wo
                JOIN alarms AS a ON a.id = wo.alarm_id AND a.well_id = wo.well_id
                JOIN diagnoses AS d ON d.id = a.diagnosis_id AND d.well_id = wo.well_id
                JOIN energy_strategies AS es ON es.well_id = wo.well_id
                JOIN strategy_versions AS sv ON sv.strategy_id = es.id
                JOIN approvals AS ap ON ap.object_type = 'energy_strategy' AND ap.object_id = es.id
                """
            ),
            16,
        )
        self.assertEqual(
            self.database.scalar(
                """
                SELECT COUNT(*)
                FROM approvals AS ap
                JOIN energy_strategies AS es ON es.id = ap.object_id
                LEFT JOIN strategy_versions AS sv ON sv.strategy_id = es.id
                LEFT JOIN device_commands AS c ON c.strategy_version_id = sv.id
                WHERE ap.status IN ('pending', 'rejected') AND c.id IS NOT NULL
                """
            ),
            0,
        )
        self.assertEqual(
            self.database.scalar(
                """
                SELECT COUNT(*)
                FROM approvals AS ap
                JOIN energy_strategies AS es ON es.id = ap.object_id
                LEFT JOIN strategy_versions AS sv ON sv.strategy_id = es.id
                LEFT JOIN device_commands AS c ON c.strategy_version_id = sv.id
                WHERE ap.status = 'approved' AND c.id IS NULL
                """
            ),
            0,
        )
        self.assertEqual(self.database.scalar("SELECT COUNT(*) FROM pragma_foreign_key_check"), 0)

    def test_concurrent_legacy_manifest_upgrade_accepts_the_winner_hash_once(self):
        """A losing CAS must accept the exact hash another starter just installed."""
        seed_database(self.database)
        expected = _previous_content_hash(self.database, 202606, "")
        replacement = _content_hash(self.database, 202606, "")
        with self.database.transaction(None, "test.legacy_hash", "seed_manifest", "acceptance-history"):
            self.database.execute(
                "UPDATE seed_manifests SET content_hash=? WHERE dataset_name='acceptance-history'", (expected,)
            )
        before = self.database.scalar("SELECT COUNT(*) FROM audit_logs WHERE action='seed.upgrade_manifest_hash'")
        with ThreadPoolExecutor(max_workers=2) as workers:
            results = list(workers.map(
                lambda _: _upgrade_manifest_hash(Database(self.database.path), expected, replacement), range(2)
            ))
        self.assertEqual(results, [None, None])
        self.assertEqual(
            self.database.scalar("SELECT content_hash FROM seed_manifests WHERE dataset_name='acceptance-history'"), replacement
        )
        self.assertEqual(
            self.database.scalar("SELECT COUNT(*) FROM audit_logs WHERE action='seed.upgrade_manifest_hash'"), before + 1
        )

    def test_device_config_projection_excludes_well_lifecycle_status_and_version(self):
        """The device projection must retain the lifecycle v3 immutable well contract."""
        seed_database(self.database)
        before = _content_hash(self.database, 202606, "")
        with self.database.transaction(None, "test.mutable_well_lifecycle", "well", "W-001"):
            self.database.execute(
                "UPDATE wells SET status='stopped', version=version+1 WHERE id='W-001'"
            )
        self.assertEqual(_content_hash(self.database, 202606, ""), before)
        with self.database.transaction(None, "test.immutable_well_identity", "well", "W-001"):
            self.database.execute("UPDATE wells SET name='tampered' WHERE id='W-001'")
        self.assertNotEqual(_content_hash(self.database, 202606, ""), before)

    def test_future_device_config_projection_inherits_lifecycle_exclusions(self):
        """A future device-config projection must not silently reintroduce lifecycle fields."""
        seed_database(self.database)
        with self.database.connection() as connection:
            before = _content_hash_from(connection, 202606, "", projection="device-config-v5")
        with self.database.transaction(None, "test.mutable_well_lifecycle", "well", "W-001"):
            self.database.execute("UPDATE wells SET status='stopped', version=version+1 WHERE id='W-001'")
        with self.database.connection() as connection:
            after = _content_hash_from(connection, 202606, "", projection="device-config-v5")
        self.assertEqual(after, before)

    def test_current_v4_manifest_is_upgraded_to_fixed_lifecycle_projection(self):
        """The ec75f85 v4 hash remains restart-compatible during the semantic repair."""
        seed_database(self.database)
        old_v4 = _task1_current_v4_content_hash(self.database, 202606, "")
        with self.database.transaction(None, "test.current_v4_manifest", "seed_manifest", "acceptance-history"):
            self.database.execute(
                "UPDATE seed_manifests SET content_hash=? WHERE dataset_name='acceptance-history'",
                (old_v4,),
            )
        seed_database(self.database)
        self.assertEqual(self.database.scalar("SELECT content_hash FROM seed_manifests WHERE dataset_name='acceptance-history'"), _content_hash(self.database, 202606, ""))

    def test_v4_upgrade_preserves_status_change_and_rejects_identity_tampering(self):
        """A v4-compatible restart migrates once, then preserves mutable lifecycle only."""
        seed_database(self.database)
        old_v4 = _task1_current_v4_content_hash(self.database, 202606, "")
        with self.database.transaction(None, "test.current_v4_manifest", "seed_manifest", "acceptance-history"):
            self.database.execute(
                "UPDATE seed_manifests SET content_hash=? WHERE dataset_name='acceptance-history'",
                (old_v4,),
            )
        seed_database(self.database)
        with self.database.transaction(None, "test.mutable_well_lifecycle", "well", "W-001"):
            self.database.execute("UPDATE wells SET status='stopped', version=version+1 WHERE id='W-001'")
        self.assertEqual(seed_database(self.database).wells, 48)
        with self.database.transaction(None, "test.immutable_well_identity", "well", "W-001"):
            self.database.execute("UPDATE wells SET name='tampered' WHERE id='W-001'")
        with self.assertRaisesRegex(RuntimeError, "content fingerprint"):
            seed_database(self.database)


if __name__ == "__main__":
    unittest.main()
