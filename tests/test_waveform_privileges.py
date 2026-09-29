"""Actual independently credentialed role and controlled raw-access checks."""
from uuid import uuid4
import psycopg
from psycopg import sql
from psycopg.types.json import Jsonb
from backend.waveforms.errors import WaveformError
from backend.waveforms.privileges import probe_role
from tests.waveform_support import WaveformDatabaseCase, RUNTIME_ROLES

ACTOR = {"domain": "local", "user_id": "1", "username": "tester", "role_code": "reader"}


class WaveformPrivilegeTests(WaveformDatabaseCase):
    def test_actual_runtime_roles_pass_readonly_probe(self):
        for role in RUNTIME_ROLES:
            with self.subTest(role=role): probe_role(lambda: self.connect(role), role)
        self.assertEqual(self.scalar("SELECT count(*) FROM waveforms.audit", role="migration_owner"), 0)

    def test_reader_no_raw_table_access(self):
        fact = self.insert_fact()
        for role in ("reader", "operator", "receipt_publisher"):
            with self.subTest(role=role):
                with self.assertRaises(psycopg.errors.InsufficientPrivilege):
                    with self.connect(role) as conn: conn.execute("SELECT raw_payload FROM waveforms.facts WHERE fact_id=%s", (fact,))
        self.assertEqual(self.scalar("SELECT count(*) FROM waveforms.fact_metadata"), 1)

    def test_roles_cannot_change_facts_or_audit_directly(self):
        fact = self.insert_fact()
        for role in RUNTIME_ROLES:
            for statement in ("UPDATE waveforms.facts SET raw_payload='bad' WHERE fact_id=%s",
                              "DELETE FROM waveforms.facts WHERE fact_id=%s"):
                with self.subTest(role=role,statement=statement):
                    with self.assertRaises(psycopg.errors.InsufficientPrivilege):
                        with self.connect(role) as conn: conn.execute(statement,(fact,))
        for role in ("reader","assembler","receipt_publisher"):
            with self.assertRaises(psycopg.errors.InsufficientPrivilege):
                with self.connect(role) as conn:
                    conn.execute("INSERT INTO waveforms.audit(intake_gateway_id,fact_id,actor,action) VALUES('GW',%s,%s,'raw.read')",(fact,Jsonb(ACTOR)))

    def test_raw_audit_rollback_is_atomic_and_unknown_id_fails(self):
        fact=self.insert_fact()
        with self.connect("reader") as conn:
            conn.execute("SELECT waveforms.audited_raw(%s,%s)",(fact,Jsonb(ACTOR)))
            conn.rollback()
        self.assertEqual(self.scalar("SELECT count(*) FROM waveforms.audit",role="migration_owner"),0)
        self.assertEqual(self.scalar("SELECT used_metadata_rows FROM waveforms.capacity WHERE bucket='GW'",role="migration_owner"),0)
        with self.assertRaises(psycopg.errors.NoDataFound):
            with self.connect("reader") as conn: conn.execute("SELECT waveforms.audited_raw(%s,%s)",(uuid4(),Jsonb(ACTOR)))

    def test_publisher_can_update_health_and_cannot_insert_content(self):
        worker=uuid4()
        with self.connect("receipt_publisher") as conn:
            conn.execute("INSERT INTO waveforms.worker_health(worker_id,component,gateway_id,last_seen) VALUES(%s,'receipt_publisher','GW',CURRENT_TIMESTAMP)",(worker,))
            conn.execute("UPDATE waveforms.worker_health SET last_success=CURRENT_TIMESTAMP WHERE worker_id=%s",(worker,))
        self.assertTrue(self.scalar("SELECT last_success IS NOT NULL FROM waveforms.worker_health WHERE worker_id=%s",(worker,),role="receipt_publisher"))
        for role in ("assembler","receipt_publisher","reader","operator"):
            with self.assertRaises(psycopg.errors.InsufficientPrivilege): self.insert_fact(role=role)

    def test_operator_can_write_bounded_audit_and_operation_receipt(self):
        fact=self.insert_fact()
        key=uuid4()
        with self.connect("operator") as conn:
            conn.execute("INSERT INTO waveforms.audit(intake_gateway_id,fact_id,actor,action) VALUES('GW',%s,%s,'case.note')",(fact,Jsonb(ACTOR)))
            conn.execute("INSERT INTO waveforms.operation_receipts(intake_gateway_id,actor_domain,actor_id,idempotency_key,request_hash,result) VALUES('GW','local','1',%s,%s,'{}')",(key,"a"*64))
            conn.execute("UPDATE waveforms.state_head SET version=version+1 WHERE singleton=1")
        self.assertEqual(self.scalar("SELECT count(*) FROM waveforms.operation_receipts WHERE idempotency_key=%s",(key,),role="operator"),1)
        self.assertEqual(self.scalar("SELECT version FROM waveforms.state_head",role="operator"),2)
        with self.assertRaises(psycopg.errors.InsufficientPrivilege):
            with self.connect("operator") as conn: conn.execute("DELETE FROM waveforms.operation_receipts WHERE idempotency_key=%s",(key,))

    def test_probe_rejects_public_function_and_grant_option(self):
        with self.admin() as conn: conn.execute("GRANT EXECUTE ON FUNCTION waveforms.audited_raw(uuid,jsonb) TO PUBLIC")
        try:
            with self.assertRaises(WaveformError) as caught: probe_role(lambda:self.connect("reader"),"reader")
            self.assertEqual(caught.exception.code,"waveform.privilege_unsafe")
        finally:
            with self.admin() as conn: conn.execute("REVOKE EXECUTE ON FUNCTION waveforms.audited_raw(uuid,jsonb) FROM PUBLIC")
        reader=self.settings["reader"]["user"]
        with self.admin() as conn: conn.execute(sql.SQL("GRANT SELECT ON waveforms.fact_metadata TO {} WITH GRANT OPTION").format(sql.Identifier(reader)))
        try:
            with self.assertRaises(WaveformError) as caught: probe_role(lambda:self.connect("reader"),"reader")
            self.assertEqual(caught.exception.code,"waveform.privilege_unsafe")
        finally:
            with self.admin() as conn: conn.execute(sql.SQL("REVOKE GRANT OPTION FOR SELECT ON waveforms.fact_metadata FROM {}").format(sql.Identifier(reader)))

    def test_runtime_cannot_ddl(self):
        for role in RUNTIME_ROLES:
            for statement in ("CREATE TABLE waveforms.forbidden(id int)", "CREATE TABLE public.forbidden(id int)",
                              "CREATE TEMP TABLE forbidden(id int)", "CREATE SCHEMA forbidden"):
                with self.subTest(role=role, statement=statement):
                    with self.assertRaises(psycopg.errors.InsufficientPrivilege):
                        with self.connect(role) as conn: conn.execute(statement)

    def test_exact_raw_read_commits_audit_and_rejects_bad_actor(self):
        fact = self.insert_fact(payload=b"original")
        with self.connect("reader") as conn:
            self.assertEqual(conn.execute("SELECT waveforms.audited_raw(%s,%s)", (fact, Jsonb(ACTOR))).fetchone()[0], b"original")
        self.assertEqual(self.scalar("SELECT count(*) FROM waveforms.audit WHERE fact_id=%s AND action='raw.read'", (fact,), role="migration_owner"), 1)
        for actor in (None, [], "bad", {}, ACTOR | {"display_name": "extra"}, ACTOR | {"user_id": True}, ACTOR | {"username": "x"*129}, ACTOR | {"username":"\n"}, ACTOR | {"username":" "}):
            with self.assertRaises(psycopg.errors.CheckViolation):
                with self.connect("reader") as conn: conn.execute("SELECT waveforms.audited_raw(%s,%s)", (fact, Jsonb(actor)))
        with self.assertRaises(psycopg.errors.InsufficientPrivilege):
            with self.connect("operator") as conn: conn.execute("SELECT waveforms.audited_raw(%s,%s)", (fact, Jsonb(ACTOR)))

    def test_raw_read_fails_when_audit_capacity_exhausted(self):
        fact = self.insert_fact()
        with self.connect("migration_owner") as conn:
            conn.execute("UPDATE waveforms.capacity SET used_metadata_rows=metadata_rows WHERE bucket='GW'")
        with self.assertRaises(psycopg.errors.CheckViolation):
            with self.connect("reader") as conn: conn.execute("SELECT waveforms.audited_raw(%s,%s)", (fact, Jsonb(ACTOR)))
        self.assertEqual(self.scalar("SELECT count(*) FROM waveforms.audit", role="migration_owner"), 0)

    def test_probe_rejects_public_column_leak(self):
        with self.admin() as conn: conn.execute("GRANT SELECT(raw_payload) ON waveforms.facts TO PUBLIC")
        try:
            with self.assertRaises(WaveformError): probe_role(lambda: self.connect("reader"), "reader")
        finally:
            with self.admin() as conn: conn.execute("REVOKE SELECT(raw_payload) ON waveforms.facts FROM PUBLIC")

    def test_probe_rejects_owner_membership(self):
        owner, reader = (self.settings[r]["user"] for r in ("migration_owner", "reader"))
        with self.admin() as conn: conn.execute(sql.SQL("GRANT {} TO {} WITH INHERIT FALSE, SET TRUE").format(sql.Identifier(owner), sql.Identifier(reader)))
        try:
            with self.assertRaises(WaveformError): probe_role(lambda: self.connect("reader"), "reader")
        finally:
            with self.admin() as conn: conn.execute(sql.SQL("REVOKE {} FROM {}").format(sql.Identifier(owner), sql.Identifier(reader)))

    def test_probe_rejects_superuser_and_disabled_fk(self):
        with self.assertRaises(WaveformError): probe_role(self.admin, "reader")
        with self.admin() as conn: conn.execute("ALTER TABLE waveforms.observations DISABLE TRIGGER ALL")
        try:
            with self.assertRaises(WaveformError): probe_role(lambda: self.connect("receiver"), "receiver")
        finally:
            with self.admin() as conn: conn.execute("ALTER TABLE waveforms.observations ENABLE TRIGGER ALL")
