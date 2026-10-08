"""Real isolated PG grants/DDL changes must make the startup probe fail closed."""
from contextlib import contextmanager
import importlib
import json
import os
from pathlib import Path
import tempfile
import tomllib

import psycopg
from psycopg import sql

from backend.center.config import _postgres_settings
from backend.registry.errors import RegistryError
from backend.secret_store import read_windows_credential
from tests.registry_support import RegistryDatabaseCase, TEST_DATABASE


class RegistryPrivilegeTests(RegistryDatabaseCase):
    def setUp(self):
        super().setUp()
        try:
            self.runtime = importlib.import_module("backend.registry.runtime")
        except ModuleNotFoundError as error:
            if error.name != "backend.registry.runtime":
                raise
            self.fail("Read-only registry privilege preflight has not been implemented")

    def probe(self):
        return self.runtime.probe_privileges(lambda: self.connect("writer"), lambda: self.connect("reader"))

    def admin(self):
        # Explicit separate test-cluster credentials, never reuse a business admin.
        raw = tomllib.loads(self.config_path.read_text(encoding="utf-8"))
        self.assertIn("postgres_test_admin", raw, "Isolated privilege tests require an explicit test administrator")
        settings = _postgres_settings(raw["postgres_test_admin"], self.config_path, os.environ, read_windows_credential)
        for key in ("host", "port", "dbname"):
            self.assertEqual(settings[key], self.settings["migrator"][key])
        self.assertEqual(settings["dbname"], TEST_DATABASE)
        conn = psycopg.connect(**(settings | {"connect_timeout": 3}))
        self.connections.append(conn)
        self.assertEqual(conn.execute("SELECT current_database()").fetchone()[0], TEST_DATABASE)
        conn.rollback()
        return conn

    @contextmanager
    def changed(self, setup, restore, *, admin=False):
        conn = self.admin() if admin else self.connect("migrator")
        conn.execute(setup)
        conn.commit()
        try:
            yield
        finally:
            conn.rollback()
            conn.execute(restore)
            conn.commit()

    def denied(self):
        with self.assertRaises(RegistryError) as caught:
            self.probe()
        self.assertEqual(caught.exception.status, 503)
        self.assertIn(caught.exception.code, {"registry.privilege_unsafe", "registry.schema_incomplete",
                                            "registry.schema_missing", "registry.schema_mismatch"})

    def test_minimum_roles_pass_without_catalog_writes(self):
        self.assertIsNone(self.probe())
        self.assertEqual(self.scalar("SELECT count(*) FROM device_registry.devices"), 0)
        self.assertEqual(self.scalar("SELECT revision FROM device_registry.registry_head"), 1)
        def readonly(role):
            conn = self.connect(role)
            conn.execute("SET default_transaction_read_only=on")
            conn.commit()
            return conn
        self.assertIsNone(self.runtime.probe_privileges(lambda: readonly("writer"), lambda: readonly("reader")))

    def test_reader_column_update_is_rejected_and_not_automatically_revoked(self):
        role = sql.Identifier(self.settings["reader"]["user"])
        with self.changed(sql.SQL("GRANT UPDATE(name) ON device_registry.wells TO {}").format(role),
                          sql.SQL("REVOKE UPDATE(name) ON device_registry.wells FROM {}").format(role)):
            self.denied()
            self.assertTrue(self.scalar("SELECT has_column_privilege(current_user,'device_registry.wells','name','UPDATE')"))
        self.probe()

    def test_public_column_grant_is_rejected(self):
        with self.changed("GRANT UPDATE(name) ON device_registry.wells TO PUBLIC",
                          "REVOKE UPDATE(name) ON device_registry.wells FROM PUBLIC"):
            self.denied()

    def test_writer_raw_column_insert_is_rejected(self):
        role = sql.Identifier(self.settings["writer"]["user"])
        with self.changed(sql.SQL("GRANT INSERT(value) ON public.telemetry_samples TO {}").format(role),
                          sql.SQL("REVOKE INSERT(value) ON public.telemetry_samples FROM {}").format(role)):
            self.denied()

    def test_writer_raw_partition_write_is_rejected(self):
        partition = self.scalar("SELECT inhrelid::regclass::text FROM pg_inherits WHERE inhparent='public.telemetry_samples'::regclass ORDER BY inhrelid LIMIT 1")
        role = sql.Identifier(self.settings["writer"]["user"])
        table = sql.Identifier("public", partition.split(".")[-1])
        with self.changed(sql.SQL("GRANT DELETE ON {} TO {}").format(table, role),
                          sql.SQL("REVOKE DELETE ON {} FROM {}").format(table, role)):
            self.denied()

    def test_writer_receipt_mutation_and_identity_column_grants_are_rejected(self):
        role = sql.Identifier(self.settings["writer"]["user"])
        for table, column in (("operation_receipts", "result"), ("devices", "gateway_id")):
            with self.subTest(table=table), self.changed(
                sql.SQL("GRANT UPDATE({}) ON device_registry.{} TO {}").format(sql.Identifier(column), sql.Identifier(table), role),
                sql.SQL("REVOKE UPDATE({}) ON device_registry.{} FROM {}").format(sql.Identifier(column), sql.Identifier(table), role)):
                self.denied()

    def test_missing_required_writer_column_permission_is_rejected(self):
        role = sql.Identifier(self.settings["writer"]["user"])
        with self.changed(sql.SQL("REVOKE UPDATE(name) ON device_registry.wells FROM {}").format(role),
                          sql.SQL("GRANT UPDATE(name) ON device_registry.wells TO {}").format(role)):
            self.denied()

    def test_missing_reader_raw_select_is_rejected(self):
        role = sql.Identifier(self.settings["reader"]["user"])
        with self.changed(sql.SQL("REVOKE SELECT ON public.telemetry_samples FROM {}").format(role),
                          sql.SQL("GRANT SELECT ON public.telemetry_samples TO {}").format(role)):
            self.denied()

    def test_writer_schema_owner_is_rejected(self):
        with self.changed(sql.SQL("ALTER SCHEMA device_registry OWNER TO {}").format(sql.Identifier(self.settings["writer"]["user"])),
                          sql.SQL("ALTER SCHEMA device_registry OWNER TO {}").format(sql.Identifier(self.settings["migrator"]["user"])), admin=True):
            self.denied()

    def test_inherited_owner_and_set_role_only_owner_are_rejected(self):
        owner = sql.Identifier(self.settings["migrator"]["user"])
        writer = sql.Identifier(self.settings["writer"]["user"])
        for options in ("WITH INHERIT TRUE, SET FALSE", "WITH INHERIT FALSE, SET TRUE", "WITH ADMIN TRUE, INHERIT FALSE, SET FALSE"):
            with self.subTest(options=options), self.changed(
                sql.SQL("GRANT {} TO {} " + options).format(owner, writer),
                sql.SQL("REVOKE {} FROM {}").format(owner, writer), admin=True):
                self.denied()

    def test_reader_can_set_writer_is_not_read_only(self):
        writer = sql.Identifier(self.settings["writer"]["user"])
        reader = sql.Identifier(self.settings["reader"]["user"])
        with self.changed(sql.SQL("GRANT {} TO {} WITH INHERIT FALSE, SET TRUE").format(writer, reader),
                          sql.SQL("REVOKE {} FROM {}").format(writer, reader), admin=True):
            self.denied()

    def test_privileged_role_flags_are_rejected(self):
        role = sql.Identifier(self.settings["writer"]["user"])
        for flag in ("SUPERUSER", "CREATEDB", "CREATEROLE", "REPLICATION", "BYPASSRLS"):
            with self.subTest(flag=flag), self.changed(sql.SQL("ALTER ROLE {} " + flag).format(role),
                                                     sql.SQL("ALTER ROLE {} NO" + flag).format(role), admin=True):
                self.denied()

    def test_trigger_bypass_permission_is_rejected(self):
        role = sql.Identifier(self.settings["writer"]["user"])
        with self.changed(sql.SQL("GRANT SET ON PARAMETER session_replication_role TO {}").format(role),
                          sql.SQL("REVOKE SET ON PARAMETER session_replication_role FROM {}").format(role), admin=True):
            self.denied()

    def test_absent_constraint_is_rejected_even_with_valid_migration_digest(self):
        definition = self.scalar("SELECT pg_get_constraintdef(oid) FROM pg_constraint WHERE conrelid='device_registry.well_bindings'::regclass AND conname='binding_no_overlap'")
        with self.changed("ALTER TABLE device_registry.well_bindings DROP CONSTRAINT binding_no_overlap",
                          sql.SQL("ALTER TABLE device_registry.well_bindings ADD CONSTRAINT binding_no_overlap " + definition)):
            self.denied()

    def test_disabled_immutability_trigger_is_rejected(self):
        with self.changed("ALTER TABLE device_registry.audit_events DISABLE TRIGGER audit_append_only",
                          "ALTER TABLE device_registry.audit_events ENABLE TRIGGER audit_append_only"):
            self.denied()

    def test_missing_schema_is_rejected_without_running_migrations(self):
        with self.changed("ALTER SCHEMA device_registry RENAME TO registry_probe_test_saved",
                          "ALTER SCHEMA registry_probe_test_saved RENAME TO device_registry"):
            self.denied()
            self.assertIsNone(self.scalar("SELECT to_regnamespace('device_registry')", role="migrator"))

    def runtime_config(self, *, port=None):
        raw = tomllib.loads(self.config_path.read_text(encoding="utf-8"))
        sections = {"registry": {"enabled": True, "allowed_origins": ["http://127.0.0.1:8765"]},
                    "postgres": dict(raw["postgres_writer"], user="test_ingest_identity_not_resolved"),
                    "postgres_registry": raw["postgres_writer"], "postgres_query": raw["postgres_reader"]}
        if port:
            for name in ("postgres", "postgres_registry", "postgres_query"):
                sections[name]["port"] = port
        temp = tempfile.TemporaryDirectory(prefix="registry-runtime-")
        self.addCleanup(temp.cleanup)
        path = Path(temp.name) / "config.toml"
        path.write_text("\n".join("[" + name + "]\n" + "\n".join(k + " = " + json.dumps(v)
                        for k, v in data.items()) for name, data in sections.items()), encoding="utf-8")
        return path

    def test_runtime_ready_exposes_real_read_and_write_services(self):
        runtime = self.runtime.build_registry_runtime(self.runtime_config())
        self.assertEqual((runtime.status, runtime.code), ("ready", None))
        self.assertIsNotNone(runtime.service)
        self.assertEqual(runtime.query.read("devices", {})["items"], [])
        self.assertEqual(runtime.allowed_origins, ("http://127.0.0.1:8765",))

    def test_runtime_unavailable_keeps_bad_privileges_unchanged(self):
        role = sql.Identifier(self.settings["reader"]["user"])
        with self.changed(sql.SQL("GRANT UPDATE(name) ON device_registry.wells TO {}").format(role),
                          sql.SQL("REVOKE UPDATE(name) ON device_registry.wells FROM {}").format(role)):
            with self.assertLogs('backend.registry.runtime',level='WARNING') as captured:
                runtime = self.runtime.build_registry_runtime(self.runtime_config())
            self.assertEqual([r.getMessage() for r in captured.records],['Registry unavailable: registry.privilege_unsafe'])
            self.assertEqual((runtime.status, runtime.code), ("unavailable", "registry.privilege_unsafe"))
            self.assertIsNone(runtime.service)
            self.assertIsNone(runtime.query)

    def test_runtime_connection_failure_is_sanitized_and_isolated(self):
        with self.assertLogs('backend.registry.runtime',level='WARNING') as captured:
            runtime = self.runtime.build_registry_runtime(self.runtime_config(port=1))
        self.assertEqual([r.getMessage() for r in captured.records],['Registry unavailable: registry.connection_unavailable'])
        self.assertEqual((runtime.status, runtime.code), ("unavailable", "registry.connection_unavailable"))
        self.assertIsNone(runtime.service)
        for settings in self.settings.values():
            self.assertNotIn(settings["password"], repr(runtime))
            self.assertNotIn(settings["password"], ''.join(captured.output))
