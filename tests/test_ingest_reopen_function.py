"""The receiver may reopen recurrences, never acquire general case mutation."""

from datetime import timedelta
import importlib
from uuid import uuid4

import psycopg

from backend.ingest_reliability.errors import ReliabilityError
from tests.ingest_support import IngestDatabaseCase
from tests.ingest_vectors import FIXED_TIME


class ReopenFunctionTests(IngestDatabaseCase):
    def setUp(self):
        super().setUp()
        self.fact = self.insert_fact()
        with self.connect("ingestor") as conn:
            self.case = conn.execute("INSERT INTO ingest_reliability.cases(fact_id) VALUES (%s) RETURNING case_id",
                                     (self.fact,)).fetchone()[0]
            conn.execute("INSERT INTO ingest_reliability.fact_observations(fact_id,first_seen,last_seen,duplicate_count) "
                         "VALUES (%s,%s,%s,1)", (self.fact, FIXED_TIME, FIXED_TIME))
        with self.connect("operator") as conn:
            conn.execute("UPDATE ingest_reliability.cases SET state='closed',version=2 WHERE case_id=%s", (self.case,))

    def reopen(self, conn, *, fact=None, now=FIXED_TIME):
        return conn.execute("SELECT ingest_reliability.reopen_repeated_case(%s,%s)",
                             (fact or self.fact, now)).fetchone()[0]

    def test_receiver_can_only_reopen_a_repeated_closed_case_and_records_system_actor(self):
        with self.connect("ingestor") as conn:
            self.assertTrue(self.reopen(conn))
            self.assertFalse(self.reopen(conn))
        self.assertEqual(self.scalar("SELECT state FROM ingest_reliability.cases WHERE case_id=%s", (self.case,), role="operator"), "open")
        self.assertEqual(self.scalar("SELECT count(*) FROM ingest_reliability.audit_events", role="migrator"), 1)
        self.assertEqual(self.scalar("SELECT actor->>'domain' FROM ingest_reliability.audit_events", role="migrator"), "system")
        with self.assertRaises(psycopg.errors.InsufficientPrivilege), self.connect("ingestor") as conn:
            conn.execute("UPDATE ingest_reliability.cases SET state='closed' WHERE case_id=%s", (self.case,))

    def test_call_rollback_restores_case_and_removes_new_audit(self):
        with self.assertRaisesRegex(RuntimeError, "injected"):
            with self.connect("ingestor") as conn:
                self.reopen(conn)
                raise RuntimeError("injected rollback")
        self.assertEqual(self.scalar("SELECT state FROM ingest_reliability.cases", role="operator"), "closed")
        self.assertEqual(self.scalar("SELECT count(*) FROM ingest_reliability.audit_events", role="migrator"), 0)

    def test_other_runtime_roles_cannot_execute_the_owner_function(self):
        for role in ("publisher", "reader", "operator"):
            with self.subTest(role=role), self.assertRaises(psycopg.errors.InsufficientPrivilege), self.connect(role) as conn:
                self.reopen(conn)

    def test_unknown_fact_and_not_yet_repeated_case_cannot_be_reopened(self):
        with self.connect("migrator") as conn:
            conn.execute("UPDATE ingest_reliability.fact_observations SET duplicate_count=0")
        with self.connect("ingestor") as conn:
            self.assertFalse(self.reopen(conn, fact=uuid4()))
            self.assertFalse(self.reopen(conn))
        self.assertEqual(self.scalar("SELECT state FROM ingest_reliability.cases", role="operator"), "closed")
        self.assertEqual(self.scalar("SELECT count(*) FROM ingest_reliability.audit_events", role="migrator"), 0)

    def test_owner_function_rejects_nonfinite_time_and_does_not_regress_timestamp(self):
        with self.assertRaises(psycopg.Error), self.connect("ingestor") as conn:
            self.reopen(conn, now="infinity")
        with self.connect("migrator") as conn:
            conn.execute("UPDATE ingest_reliability.cases SET updated_at=%s", (FIXED_TIME,))
        with self.connect("ingestor") as conn:
            self.assertTrue(self.reopen(conn, now=FIXED_TIME-timedelta(seconds=60)))
        self.assertEqual(self.scalar("SELECT updated_at FROM ingest_reliability.cases", role="operator"), FIXED_TIME)

    def test_audit_failure_rolls_back_reopen_inside_the_function(self):
        # Test-only failure trigger, removed in finally before the next fixture.
        with self.connect("migrator") as conn:
            conn.execute("CREATE FUNCTION ingest_reliability.reject_test_audit() RETURNS trigger "
                         "LANGUAGE plpgsql AS $$ BEGIN RAISE EXCEPTION 'injected audit failure'; END $$")
            conn.execute("CREATE TRIGGER reject_test_audit BEFORE INSERT ON ingest_reliability.audit_events "
                         "FOR EACH ROW EXECUTE FUNCTION ingest_reliability.reject_test_audit()")
            conn.commit()
            try:
                with self.assertRaises(psycopg.Error), self.connect("ingestor") as runtime:
                    self.reopen(runtime)
                self.assertEqual(self.scalar("SELECT state FROM ingest_reliability.cases", role="operator"), "closed")
            finally:
                conn.execute("DROP TRIGGER reject_test_audit ON ingest_reliability.audit_events")
                conn.execute("DROP FUNCTION ingest_reliability.reject_test_audit()")

    def test_preflight_accepts_exact_function_grant_but_rejects_public_exposure(self):
        probe = importlib.import_module("backend.ingest_reliability.privileges").probe_role
        for role in ("ingestor", "publisher", "reader", "operator"):
            probe(lambda: self.connect(role), role)
        with self.connect("migrator") as conn:
            conn.execute("GRANT EXECUTE ON FUNCTION ingest_reliability.reopen_repeated_case(uuid,timestamptz) TO PUBLIC")
            conn.commit()
            try:
                with self.assertRaises(ReliabilityError):
                    probe(lambda: self.connect("reader"), "reader")
            finally:
                conn.execute("REVOKE EXECUTE ON FUNCTION ingest_reliability.reopen_repeated_case(uuid,timestamptz) FROM PUBLIC")
