"""Removing each constraint/grant below must allow a forbidden real SQL operation."""
from datetime import datetime, timedelta, timezone
import hashlib
import os
import subprocess
import sys
from uuid import uuid4

import psycopg
from psycopg import sql
from psycopg.types.json import Jsonb

from backend.registry.errors import RegistryError
from backend.registry.point_maps import canonical_point_map
from tests.registry_support import RegistryDatabaseCase, ROOT, TEST_DATABASE


T0 = datetime(2026, 9, 23, tzinfo=timezone.utc)
T1 = T0 + timedelta(hours=1)
T2 = T1 + timedelta(hours=1)


class RegistrySchemaTests(RegistryDatabaseCase):
    def seed_sql(self, conn):
        well, device, event = uuid4(), uuid4(), uuid4()
        conn.execute("INSERT INTO device_registry.wells(id,area_code,well_code,name) VALUES (%s,'A',%s,'测试井')",
                     (well, str(well)))
        conn.execute("INSERT INTO device_registry.devices(id,gateway_id,device_id,name,transport,usage) "
                     "VALUES (%s,'GW',%s,'测试终端','mqtt_direct','commissioning')", (device, str(device)))
        conn.execute("INSERT INTO device_registry.audit_events(id,actor,action,object_type,object_id,"
                     "reason,before_data,after_data,request_id) VALUES (%s,'{}','test','device',%s,"
                     "'隔离库验证','{}','{}',%s)", (event, device, uuid4()))
        content, raw, digest = canonical_point_map("mqtt_direct", [{"point_code": "load", "unit": "kN"}])
        conn.execute("INSERT INTO device_registry.point_map_versions(device_id,version,transport,"
                     "canonical_rule,canonical_bytes,content,sha256,created_event_id) "
                     "VALUES (%s,1,'mqtt_direct',%s,%s,%s,%s,%s)",
                     (device, content["rule"], raw, Jsonb(content), digest, event))
        return well, device, event

    def period(self, conn, table, fixture, start=T0, end=None):
        well, device, event = fixture
        extra = {"well_bindings": ("well_id", well), "device_state_periods": ("status", "pending"),
                 "point_map_assignments": ("map_version", 1)}[table]
        return conn.execute(sql.SQL("INSERT INTO device_registry.{}(device_id,{},valid_from,valid_to,"
                                    "audit_event_id) VALUES (%s,%s,%s,%s,%s) RETURNING id")
                            .format(sql.Identifier(table), sql.Identifier(extra[0])),
                            (device, extra[1], start, end, event)).fetchone()[0]

    def test_fresh_schema_has_no_production_catalog_entries(self):
        self.assertEqual(self.scalar("SELECT count(*) FROM device_registry.wells"), 0)
        self.assertEqual(self.scalar("SELECT count(*) FROM device_registry.devices"), 0)
        self.assertEqual(self.scalar("SELECT revision FROM device_registry.registry_head"), 1)
        self.assertEqual(self.scalar("SELECT count(*) FROM device_registry.schema_migrations"), 1)
        self.assertEqual(self.scalar("SELECT relkind FROM pg_class WHERE oid='public.telemetry_samples'::regclass"), 'p')

    def test_fresh_ddl_creates_empty_catalog_before_any_fixture_reset(self):
        # Exercise the full DDL in a rollback-only namespace swap, on the
        # explicitly isolated DB. No schema/database is dropped or persisted.
        conn = self.connect("migrator")
        try:
            conn.execute("ALTER SCHEMA device_registry RENAME TO registry_prior_test")
            conn.execute((ROOT / "backend/registry/sql/001_registry.sql").read_text(encoding="utf-8"))
            self.assertEqual(conn.execute("SELECT count(*) FROM device_registry.wells").fetchone()[0], 0)
            self.assertEqual(conn.execute("SELECT count(*) FROM device_registry.devices").fetchone()[0], 0)
            self.assertEqual(conn.execute("SELECT revision FROM device_registry.registry_head").fetchone()[0], 1)
        finally:
            conn.rollback()
        self.assertIsNone(conn.execute("SELECT to_regnamespace('registry_prior_test')").fetchone()[0])

    def test_all_temporal_ranges_reject_overlap_but_allow_exact_adjacency(self):
        with self.connect("migrator") as conn:
            fixture = self.seed_sql(conn)
            for table in ("well_bindings", "device_state_periods", "point_map_assignments"):
                with self.subTest(table=table):
                    self.period(conn, table, fixture, T0, T1)
                    with self.assertRaises(psycopg.errors.ExclusionViolation), conn.transaction():
                        self.period(conn, table, fixture, T0 + timedelta(minutes=30))
                    self.period(conn, table, fixture, T1)

    def test_periods_reject_empty_backward_and_infinite_starts(self):
        with self.connect("migrator") as conn:
            fixture = self.seed_sql(conn)
            for table in ("well_bindings", "device_state_periods", "point_map_assignments"):
                for start, end in ((T0, T0), (T1, T0), ("-infinity", None), (T0, "infinity")):
                    with self.subTest(table=table, start=start, end=end):
                        with self.assertRaises(psycopg.Error), conn.transaction():
                            self.period(conn, table, fixture, start, end)

    def test_open_period_can_only_be_closed_once_without_rewriting_other_fields(self):
        with self.connect("migrator") as conn:
            fixture = self.seed_sql(conn)
            for table in ("well_bindings", "device_state_periods", "point_map_assignments"):
                key = self.period(conn, table, fixture)
                for statement in ("UPDATE device_registry.{} SET valid_from=%s WHERE id=%s",
                                  "UPDATE device_registry.{} SET valid_to=%s WHERE id=%s"):
                    with self.assertRaises(psycopg.Error), conn.transaction():
                        conn.execute(sql.SQL(statement).format(sql.Identifier(table)), (T0, key))
                conn.execute(sql.SQL("UPDATE device_registry.{} SET valid_to=%s WHERE id=%s")
                             .format(sql.Identifier(table)), (T1, key))
                for statement, params in (("UPDATE device_registry.{} SET valid_to=%s WHERE id=%s", (T2, key)),
                                          ("DELETE FROM device_registry.{} WHERE id=%s", (key,))):
                    with self.assertRaises(psycopg.Error), conn.transaction():
                        conn.execute(sql.SQL(statement).format(sql.Identifier(table)), params)

    def test_audit_reference_is_deferred_but_must_exist_at_commit(self):
        with self.connect("migrator") as conn:
            well, device, event = self.seed_sql(conn)
        missing = uuid4()
        conn = self.connect("migrator")
        conn.execute("INSERT INTO device_registry.well_bindings(device_id,well_id,valid_from,audit_event_id) "
                     "VALUES (%s,%s,%s,%s)", (device, well, T0, missing))
        with self.assertRaises(psycopg.errors.ForeignKeyViolation):
            conn.commit()
        conn.rollback()
        self.assertEqual(self.scalar("SELECT count(*) FROM device_registry.well_bindings"), 0)
        with conn:
            conn.execute("INSERT INTO device_registry.well_bindings(device_id,well_id,valid_from,audit_event_id) "
                         "VALUES (%s,%s,%s,%s)", (device, well, T0, missing))
            conn.execute("INSERT INTO device_registry.audit_events(id,actor,action,object_type,object_id,"
                         "reason,before_data,after_data,request_id) VALUES (%s,'{}','test','device',%s,"
                         "'隔离库验证','{}','{}',%s)", (missing, device, uuid4()))
        self.assertEqual(self.scalar("SELECT count(*) FROM device_registry.well_bindings"), 1)

    def test_map_parent_and_assignment_cannot_reference_another_device(self):
        with self.connect("migrator") as conn:
            first = self.seed_sql(conn)
            second = self.seed_sql(conn)
            document, raw, digest = canonical_point_map("mqtt_direct", [{"point_code": "power"}])
            conn.execute("INSERT INTO device_registry.point_map_versions(device_id,version,parent_version,transport,"
                         "canonical_rule,canonical_bytes,content,sha256,created_event_id) VALUES (%s,2,1,'mqtt_direct',%s,%s,%s,%s,%s)",
                         (first[1], document["rule"], raw, Jsonb(document), digest, first[2]))
            with self.assertRaises(psycopg.errors.ForeignKeyViolation), conn.transaction():
                conn.execute("INSERT INTO device_registry.point_map_assignments(device_id,map_version,valid_from,"
                             "audit_event_id) VALUES (%s,2,%s,%s)", (second[1], T0, second[2]))
            with self.assertRaises(psycopg.errors.ForeignKeyViolation), conn.transaction():
                conn.execute("INSERT INTO device_registry.point_map_versions(device_id,version,parent_version,transport,"
                             "canonical_rule,canonical_bytes,content,sha256,created_event_id) VALUES (%s,3,2,'mqtt_direct',%s,%s,%s,%s,%s)",
                             (second[1], document["rule"], raw, Jsonb(document), digest, second[2]))

    def test_point_snapshot_content_is_immutable_even_before_review(self):
        with self.connect("migrator") as conn:
            well, device, event = self.seed_sql(conn)
            for column, value in (("content", Jsonb({})), ("canonical_bytes", b'{}'), ("sha256", 'a' * 64),
                                  ("transport", "modbus_tcp"), ("parent_version", 1), ("created_at", T0)):
                with self.subTest(column=column), self.assertRaises(psycopg.Error), conn.transaction():
                    conn.execute(sql.SQL("UPDATE device_registry.point_map_versions SET {}=%s WHERE device_id=%s")
                                 .format(sql.Identifier(column)), (value, device))
            with self.assertRaises(psycopg.Error), conn.transaction():
                conn.execute("DELETE FROM device_registry.point_map_versions WHERE device_id=%s", (device,))

    def test_point_review_is_final_and_has_required_evidence(self):
        with self.connect("migrator") as conn:
            well, device, event = self.seed_sql(conn)
            with self.assertRaises(psycopg.Error), conn.transaction():
                conn.execute("UPDATE device_registry.point_map_versions SET status='approved' WHERE device_id=%s", (device,))
            conn.execute("UPDATE device_registry.point_map_versions SET status='approved',reviewed_at=clock_timestamp(),"
                         "review_event_id=%s WHERE device_id=%s", (event, device))
            for decision in ('approved', 'rejected', 'draft'):
                with self.assertRaises(psycopg.Error), conn.transaction():
                    conn.execute("UPDATE device_registry.point_map_versions SET status=%s WHERE device_id=%s", (decision, device))

    def test_directory_identity_locked_usage_and_retirement_are_permanent(self):
        with self.connect("migrator") as conn:
            well, device, event = self.seed_sql(conn)
            for column, value in (("gateway_id", "NEW"), ("device_id", "NEW"), ("transport", "modbus_rtu"),
                                  ("created_at", T0), ("id", uuid4())):
                with self.subTest(column=column), self.assertRaises(psycopg.Error), conn.transaction():
                    conn.execute(sql.SQL("UPDATE device_registry.devices SET {}=%s WHERE id=%s")
                                 .format(sql.Identifier(column)), (value, device))
            with self.assertRaises(psycopg.Error), conn.transaction():
                conn.execute("UPDATE device_registry.wells SET well_code='OTHER' WHERE id=%s", (well,))
            conn.execute("UPDATE device_registry.devices SET usage_locked=true WHERE id=%s", (device,))
            for statement in ("SET usage_locked=false", "SET usage='production'"):
                with self.assertRaises(psycopg.Error), conn.transaction():
                    conn.execute("UPDATE device_registry.devices " + statement + " WHERE id=%s", (device,))
            conn.execute("UPDATE device_registry.devices SET status='retired' WHERE id=%s", (device,))
            for statement in ("SET name='changed'", "SET status='pending'"):
                with self.assertRaises(psycopg.Error), conn.transaction():
                    conn.execute("UPDATE device_registry.devices " + statement + " WHERE id=%s", (device,))
            with self.assertRaises(psycopg.Error), conn.transaction():
                conn.execute("DELETE FROM device_registry.devices WHERE id=%s", (device,))

    def test_duplicate_identity_and_invalid_versions_are_rejected(self):
        with self.connect("migrator") as conn:
            well, device, event = self.seed_sql(conn)
            with self.assertRaises(psycopg.errors.UniqueViolation), conn.transaction():
                conn.execute("INSERT INTO device_registry.devices(id,gateway_id,device_id,name,transport,usage) "
                             "SELECT %s,gateway_id,device_id,name,transport,usage FROM device_registry.devices WHERE id=%s",
                             (uuid4(), device))
            for table in ("devices", "wells"):
                with self.assertRaises(psycopg.Error), conn.transaction():
                    conn.execute(sql.SQL("UPDATE device_registry.{} SET version=0").format(sql.Identifier(table)))

    def test_audit_and_receipt_rows_are_append_only_even_for_table_owner(self):
        with self.connect("migrator") as conn:
            self.seed_sql(conn)
            conn.execute("INSERT INTO device_registry.operation_receipts(actor_domain,actor_id,key,request_hash,result) "
                         "VALUES ('test','101',%s,%s,'{}')", (uuid4(), "a" * 64))
            for table, column in (("audit_events", "reason"), ("operation_receipts", "actor_id")):
                for statement in (sql.SQL("UPDATE device_registry.{} SET {}='changed'")
                                  .format(sql.Identifier(table), sql.Identifier(column)),
                                  sql.SQL("DELETE FROM device_registry.{}").format(sql.Identifier(table))):
                    with self.assertRaises(psycopg.Error), conn.transaction():
                        conn.execute(statement)

    def test_runtime_roles_are_least_privilege_and_writer_can_do_required_updates(self):
        with self.connect("writer") as conn:
            well, device, event = self.seed_sql(conn)
            conn.execute("UPDATE device_registry.devices SET name='更新名称',version=2 WHERE id=%s", (device,))
        for role in ("writer", "reader"):
            with self.connect(role) as conn:
                self.assertFalse(conn.execute("SELECT rolsuper FROM pg_roles WHERE rolname=current_user").fetchone()[0])
                self.assertEqual(conn.execute("SELECT count(*) FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace "
                                              "WHERE n.nspname='device_registry' AND c.relowner=(SELECT oid FROM pg_roles WHERE rolname=current_user)")
                                 .fetchone()[0], 0)
                for statement in ("UPDATE device_registry.audit_events SET reason='illegal'", "DELETE FROM device_registry.audit_events",
                                  "DELETE FROM device_registry.devices", "TRUNCATE device_registry.wells",
                                  "CREATE TABLE device_registry.illegal(id int)", "UPDATE public.telemetry_samples SET value=0"):
                    with self.subTest(role=role, statement=statement):
                        with self.assertRaises(psycopg.errors.InsufficientPrivilege), conn.transaction():
                            conn.execute(statement)
        with self.connect("reader") as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM public.telemetry_samples").fetchone()[0], 0)
            with self.assertRaises(psycopg.errors.InsufficientPrivilege), conn.transaction():
                conn.execute("UPDATE device_registry.devices SET name='reader-write'")
        with self.connect("writer") as conn:
            with self.assertRaises(psycopg.errors.InsufficientPrivilege), conn.transaction():
                conn.execute("UPDATE device_registry.devices SET device_id='new-id'")

    def test_repeated_migration_is_noop_and_checksum_mismatch_is_rejected(self):
        before = self.scalar("SELECT applied_at FROM device_registry.schema_migrations WHERE version=1")
        result = self.migrations.migrate(self.settings["migrator"], expected_database=TEST_DATABASE)
        self.assertFalse(result["applied"])
        self.assertEqual(self.scalar("SELECT applied_at FROM device_registry.schema_migrations WHERE version=1"), before)
        digest = self.scalar("SELECT sha256 FROM device_registry.schema_migrations WHERE version=1")
        source = (ROOT / "backend/registry/sql/001_registry.sql").read_text(encoding="utf-8")
        self.assertEqual(digest, hashlib.sha256(source.encode("utf-8")).hexdigest())
        with self.connect("migrator") as conn:
            conn.execute("UPDATE device_registry.schema_migrations SET sha256=%s WHERE version=1", ("0" * 64,))
        try:
            with self.assertRaises(RegistryError):
                self.migrations.migrate(self.settings["migrator"], expected_database=TEST_DATABASE)
        finally:
            with self.connect("migrator") as conn:
                conn.execute("UPDATE device_registry.schema_migrations SET sha256=%s WHERE version=1", (digest,))
        with self.connect("migrator") as conn:
            conn.execute("UPDATE device_registry.schema_migrations SET version=2 WHERE version=1")
        try:
            with self.assertRaises(RegistryError):
                self.migrations.migrate(self.settings["migrator"], expected_database=TEST_DATABASE)
        finally:
            with self.connect("migrator") as conn:
                conn.execute("UPDATE device_registry.schema_migrations SET version=1 WHERE version=2")

    def test_migration_target_must_match_connected_database(self):
        with self.assertRaises(RegistryError):
            self.migrations.migrate(self.settings["migrator"], expected_database="oilwell_platform")
        self.assertEqual(self.scalar("SELECT count(*) FROM device_registry.schema_migrations"), 1)

    def test_check_cli_is_read_only_and_reports_actual_version(self):
        command = [sys.executable, "-m", "backend.registry.migrate", "--config", str(self.config_path),
                   "--expected-database", TEST_DATABASE, "--writer-role", self.settings["writer"]["user"],
                   "--reader-role", self.settings["reader"]["user"], "--check"]
        result = subprocess.run(command, cwd=ROOT, capture_output=True, text=True, timeout=20,
                                env=os.environ | {"PGOPTIONS": "-c default_transaction_read_only=on"})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('"version": "1"', result.stdout)
        self.assertEqual(self.scalar("SELECT revision FROM device_registry.registry_head"), 1)

    def test_grant_roles_rejects_same_role_or_owner(self):
        for writer, reader in ((self.settings["writer"]["user"], self.settings["writer"]["user"]),
                               (self.settings["migrator"]["user"], self.settings["reader"]["user"])):
            with self.connect("migrator") as conn:
                with self.assertRaises(RegistryError):
                    self.migrations.grant_roles(conn, writer=writer, reader=reader)
