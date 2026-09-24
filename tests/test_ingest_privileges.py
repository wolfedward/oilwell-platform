"""Real grants, inheritance and schema drift must make startup fail closed."""

from contextlib import contextmanager
import importlib

import psycopg
from psycopg import sql

from backend.ingest_reliability.errors import ReliabilityError
from tests.ingest_support import IngestDatabaseCase


class PrivilegeTests(IngestDatabaseCase):
    def setUp(self):
        super().setUp()
        self.privileges = importlib.import_module("backend.ingest_reliability.privileges")

    def probe(self, role):
        return self.privileges.probe_role(lambda: self.connect(role), role)

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

    def denied(self, role):
        with self.assertRaises(ReliabilityError) as caught:
            self.probe(role)
        self.assertEqual(caught.exception.status, 503)
        self.assertIn(caught.exception.code, {"ingest.privilege_unsafe", "ingest.schema_missing",
                                             "ingest.schema_mismatch", "ingest.schema_incomplete"})

    def test_minimum_runtime_roles_pass_without_ddl_or_privilege_writes(self):
        for role in ("ingestor", "publisher", "reader", "operator"):
            with self.subTest(role=role):
                def readonly():
                    conn = self.connect(role)
                    conn.execute("SET default_transaction_read_only=on")
                    conn.commit()
                    return conn
                self.assertIsNone(self.privileges.probe_role(readonly, role))

    def test_runtime_roles_cannot_mutate_raw_evidence_or_create_tables(self):
        for role in ("ingestor", "publisher", "reader", "operator"):
            statements = ("DELETE FROM ingest_reliability.facts",
                          "UPDATE ingest_reliability.facts SET raw_payload='changed'::bytea",
                          "UPDATE ingest_reliability.receipts SET payload='changed'::bytea",
                          "CREATE TABLE ingest_reliability.forbidden_test(id integer)",
                          "CREATE TABLE public.forbidden_test(id integer)",
                          "CREATE TEMP TABLE forbidden_test(id integer)")
            for statement in statements:
                with self.subTest(role=role, statement=statement), self.assertRaises(psycopg.errors.InsufficientPrivilege):
                    with self.connect(role) as conn:
                        conn.execute(statement)

    def test_publisher_reader_and_operator_cannot_write_telemetry(self):
        for role in ("publisher", "reader", "operator"):
            for statement in ("UPDATE public.telemetry_samples SET value=0", "DELETE FROM public.ingest_messages",
                              "INSERT INTO public.ingest_messages(message_id) VALUES ('forbidden')"):
                with self.subTest(role=role, statement=statement), self.assertRaises(psycopg.errors.InsufficientPrivilege):
                    with self.connect(role) as conn:
                        conn.execute(statement)

    def test_reader_has_no_direct_or_public_raw_access(self):
        role = sql.Identifier(self.settings["reader"]["user"])
        for who in (role, sql.SQL("PUBLIC")):
            with self.subTest(who=str(who)), self.changed(
                sql.SQL("GRANT SELECT(raw_payload) ON ingest_reliability.facts TO {}").format(who),
                sql.SQL("REVOKE SELECT(raw_payload) ON ingest_reliability.facts FROM {}").format(who)):
                self.denied("reader")

    def test_mutation_column_grant_is_rejected_without_silent_repair(self):
        role = sql.Identifier(self.settings["publisher"]["user"])
        with self.changed(sql.SQL("GRANT UPDATE(payload) ON ingest_reliability.receipts TO {}").format(role),
                          sql.SQL("REVOKE UPDATE(payload) ON ingest_reliability.receipts FROM {}").format(role)):
            self.denied("publisher")
            self.assertTrue(self.scalar("SELECT has_column_privilege(current_user,"
                                        "'ingest_reliability.receipts','payload','UPDATE')", role="publisher"))

    def test_reachable_owner_or_operator_privileges_are_rejected(self):
        reader = sql.Identifier(self.settings["reader"]["user"])
        for source in ("migrator", "operator"):
            parent = sql.Identifier(self.settings[source]["user"])
            for options in ("WITH INHERIT TRUE, SET FALSE", "WITH INHERIT FALSE, SET TRUE",
                            "WITH ADMIN TRUE, INHERIT FALSE, SET FALSE"):
                with self.subTest(source=source, options=options), self.changed(
                    sql.SQL("GRANT {} TO {} " + options).format(parent, reader),
                    sql.SQL("REVOKE {} FROM {}").format(parent, reader), admin=True):
                    self.denied("reader")

    def test_management_flags_and_trigger_bypass_are_rejected(self):
        role = sql.Identifier(self.settings["ingestor"]["user"])
        for flag in ("SUPERUSER", "CREATEDB", "CREATEROLE", "REPLICATION", "BYPASSRLS"):
            with self.subTest(flag=flag), self.changed(sql.SQL("ALTER ROLE {} " + flag).format(role),
                                                     sql.SQL("ALTER ROLE {} NO" + flag).format(role), admin=True):
                self.denied("ingestor")
        with self.changed(sql.SQL("GRANT SET ON PARAMETER session_replication_role TO {}").format(role),
                          sql.SQL("REVOKE SET ON PARAMETER session_replication_role FROM {}").format(role), admin=True):
            self.denied("ingestor")

    def test_disabled_evidence_trigger_is_rejected_even_with_migration_digest(self):
        with self.changed("ALTER TABLE ingest_reliability.facts DISABLE TRIGGER facts_immutable",
                          "ALTER TABLE ingest_reliability.facts ENABLE TRIGGER facts_immutable"):
            self.denied("ingestor")

    def test_changed_constraint_definition_is_rejected_not_just_its_name(self):
        definition = self.scalar("SELECT pg_get_constraintdef(oid) FROM pg_constraint WHERE "
                                 "conrelid='ingest_reliability.facts'::regclass AND conname='facts_payload_bytes_check'",
                                 role="migrator")
        with self.changed("ALTER TABLE ingest_reliability.facts DROP CONSTRAINT facts_payload_bytes_check; "
                          "ALTER TABLE ingest_reliability.facts ADD CONSTRAINT facts_payload_bytes_check CHECK (payload_bytes>=0)",
                          sql.SQL("ALTER TABLE ingest_reliability.facts DROP CONSTRAINT facts_payload_bytes_check; "
                                  "ALTER TABLE ingest_reliability.facts ADD CONSTRAINT facts_payload_bytes_check " + definition)):
            self.denied("ingestor")

    def test_direct_partition_write_grant_is_rejected(self):
        partition = self.scalar("SELECT c.relname FROM pg_inherits i JOIN pg_class c ON c.oid=i.inhrelid "
                                "WHERE i.inhparent='public.telemetry_samples'::regclass ORDER BY c.relname LIMIT 1",
                                role="migrator")
        table = sql.Identifier("public", partition)
        role = sql.Identifier(self.settings["operator"]["user"])
        with self.changed(sql.SQL("GRANT DELETE ON {} TO {}").format(table, role),
                          sql.SQL("REVOKE DELETE ON {} FROM {}").format(table, role)):
            self.denied("operator")

    def test_missing_required_privileges_are_not_silently_repaired(self):
        role = sql.Identifier(self.settings["ingestor"]["user"])
        with self.changed(sql.SQL("REVOKE INSERT ON ingest_reliability.facts FROM {}").format(role),
                          sql.SQL("GRANT INSERT ON ingest_reliability.facts TO {}").format(role)):
            self.denied("ingestor")

    def test_unnecessary_metadata_and_raw_select_are_rejected(self):
        for runtime, table, column in (("reader", "deliveries", "lease_token"),
                                       ("operator", "receipts", "payload"),
                                       ("publisher", "facts", "raw_payload")):
            role = sql.Identifier(self.settings[runtime]["user"])
            target = sql.Identifier("ingest_reliability", table)
            with self.subTest(runtime=runtime), self.changed(
                sql.SQL("GRANT SELECT({}) ON {} TO {}").format(sql.Identifier(column), target, role),
                sql.SQL("REVOKE SELECT({}) ON {} FROM {}").format(sql.Identifier(column), target, role)):
                self.denied(runtime)

    def test_disabled_internal_foreign_key_trigger_is_rejected(self):
        with self.changed("ALTER TABLE ingest_reliability.facts DISABLE TRIGGER ALL",
                          "ALTER TABLE ingest_reliability.facts ENABLE TRIGGER ALL", admin=True):
            self.denied("ingestor")

    def test_trigger_function_execute_and_temporary_ddl_grants_are_rejected(self):
        role = sql.Identifier(self.settings["reader"]["user"])
        with self.changed(sql.SQL("GRANT EXECUTE ON FUNCTION ingest_reliability.immutable_row() TO {}").format(role),
                          sql.SQL("REVOKE EXECUTE ON FUNCTION ingest_reliability.immutable_row() FROM {}").format(role)):
            self.denied("reader")
        with self.changed(sql.SQL("GRANT TEMP ON DATABASE oilwell_ingest_reliability_test TO {}").format(role),
                          sql.SQL("REVOKE TEMP ON DATABASE oilwell_ingest_reliability_test FROM {}").format(role)):
            self.denied("reader")
