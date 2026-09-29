"""Real isolated persistence checks; every test needs explicit fixture config."""
from dataclasses import replace
from contextlib import contextmanager
from hashlib import sha256
from unittest.mock import patch
import unittest
from uuid import uuid4

import psycopg

from backend.waveforms.config import CapacityLimits
from backend.waveforms.errors import WaveformError
from backend.waveforms.migrate import migrate
from backend.waveforms import migrate as migration
from tests.waveform_support import WaveformDatabaseCase, LIMITS, TEST_DATABASE
from tools.provision_waveform_test import provision, ProvisionError


class MigrationTargetTests(unittest.TestCase):
    def test_unpinned_provision_target_never_resolves_secrets(self):
        with patch("tools.provision_waveform_test.read_windows_credential", side_effect=AssertionError("secret lookup before identity gate")):
            with self.assertRaises(ProvisionError): provision("C:/unowned/registry-test/identity.json", "C:/unowned/waveform-test")

    def test_wrong_database_never_mutates(self):
        class Connection:
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def execute(self, statement, params=None):
                if statement != "SELECT current_database()":
                    raise AssertionError("wrong database reached a mutation or lock")
                return self
            def fetchone(self): return ("not_the_requested_database",)
        with patch("backend.waveforms.migrate.psycopg.connect", return_value=Connection()):
            with self.assertRaises(WaveformError) as caught:
                migrate({}, expected_database=TEST_DATABASE, limits=LIMITS)
        self.assertEqual(caught.exception.code, "waveform.migration_target")

    def test_capacity_requires_explicit_consistent_buckets(self):
        with self.assertRaises(ValueError):
            CapacityLimits(global_quota=LIMITS.global_quota, gateways={})
        with self.assertRaises(ValueError):
            CapacityLimits(global_quota=LIMITS.global_quota,
                           gateways={"GW": replace(LIMITS.global_quota, raw_bytes=2**40)})
        with self.assertRaises(ValueError):
            replace(LIMITS.global_quota, facts=True)
        with self.assertRaises(TypeError):
            LIMITS.gateways["OTHER"] = LIMITS.global_quota


class WaveformSchemaTests(WaveformDatabaseCase):
    @contextmanager
    def migration_sandbox(self, *, v1=False):
        """Real DDL in a rollback-only transaction; not commit evidence.

        setUp has already verified pinned cluster ownership and holds the reset
        guard. Only the owned waveforms namespace is temporarily renamed.
        The connection wrapper neither commits nor ends the outer transaction.
        """
        with self.connect("migration_owner") as check:
            before = check.execute("SELECT * FROM waveforms.schema_migrations ORDER BY version").fetchall()
            facts = check.execute("SELECT * FROM waveforms.facts ORDER BY fact_id").fetchall()
            structure = migration.structure_digest(check)
        conn = self.connect("migration_owner")
        class BorrowedConnection:
            def __enter__(self): return conn
            def __exit__(self, *args): return False
        try:
            conn.execute("ALTER SCHEMA waveforms RENAME TO task9_held_waveforms")
            with patch.object(migration.psycopg, "connect", return_value=BorrowedConnection()):
                if v1:
                    with patch.object(migration, "MIGRATIONS", migration.MIGRATIONS[:1]):
                        migrate({}, expected_database=TEST_DATABASE, limits=LIMITS)
                yield conn
        finally:
            conn.rollback()
            with self.connect("migration_owner") as check:
                self.assertEqual(check.execute("SELECT * FROM waveforms.schema_migrations ORDER BY version").fetchall(), before)
                self.assertEqual(check.execute("SELECT * FROM waveforms.facts ORDER BY fact_id").fetchall(), facts)
                self.assertEqual(migration.structure_digest(check), structure)
                self.assertIsNone(check.execute("SELECT to_regnamespace('task9_held_waveforms')").fetchone()[0])

    def test_fresh_chain_and_populated_v1_preserve_history_and_bytes(self):
        for v1 in (False, True):
            with self.subTest(v1=v1), self.migration_sandbox(v1=v1) as conn:
                history = []
                if v1:
                    history = conn.execute("SELECT * FROM waveforms.schema_migrations").fetchall()
                    raw = b"synthetic raw before additive upgrade"
                    fact = uuid4()
                    conn.execute("INSERT INTO waveforms.facts(fact_id,intake_gateway_id,raw_payload,payload_sha256,payload_bytes,received_at) VALUES(%s,'GW',%s,%s,%s,CURRENT_TIMESTAMP)", (fact, raw, sha256(raw).hexdigest(), len(raw)))
                    conn.execute("INSERT INTO waveforms.message_outcomes(outcome_id,fact_id,intake_gateway_id,validator_version,status,reason_code) VALUES(%s,%s,'GW','OWP-WAVE/1.0','quarantined','json_invalid')", (uuid4(),fact))
                    before = conn.execute("SELECT * FROM waveforms.facts").fetchall()
                    outcomes = conn.execute("SELECT * FROM waveforms.message_outcomes").fetchall()
                    with self.assertRaises(WaveformError): migration.check_schema(conn, expected_database=TEST_DATABASE)
                self.assertTrue(migrate({}, expected_database=TEST_DATABASE, limits=LIMITS)["applied"])
                self.assertEqual(migration.check_schema(conn, expected_database=TEST_DATABASE)["version"], "2")
                self.assertFalse(migrate({}, expected_database=TEST_DATABASE, limits=LIMITS)["applied"])
                if v1:
                    self.assertEqual(conn.execute("SELECT * FROM waveforms.schema_migrations WHERE version=1").fetchall(), history)
                    self.assertEqual(conn.execute("SELECT * FROM waveforms.facts").fetchall(), before)
                    self.assertEqual(conn.execute("SELECT * FROM waveforms.message_outcomes").fetchall(), outcomes)

    def test_legacy_import_upgrade_refuses_before_ddl_and_preserves_rows(self):
        with self.migration_sandbox(v1=True) as conn:
            conn.execute("INSERT INTO waveforms.imports(import_id,intake_gateway_id,actor,file_sha256,file_bytes,state,line_count,recorded_at) VALUES(gen_random_uuid(),'GW','{\"domain\":\"test\",\"user_id\":\"1\",\"username\":\"synthetic\",\"role_code\":\"operator\"}',%s,1,'recorded',1,CURRENT_TIMESTAMP)", ("a"*64,))
            before = conn.execute("SELECT * FROM waveforms.imports").fetchall()
            history = conn.execute("SELECT * FROM waveforms.schema_migrations").fetchall()
            structure = migration.structure_digest(conn)
            with self.assertRaises(WaveformError) as caught:
                migrate({}, expected_database=TEST_DATABASE, limits=LIMITS)
            self.assertEqual(caught.exception.code, "waveform.legacy_imports")
            self.assertEqual(conn.execute("SELECT * FROM waveforms.imports").fetchall(), before)
            self.assertEqual(conn.execute("SELECT * FROM waveforms.schema_migrations").fetchall(), history)
            self.assertEqual(migration.structure_digest(conn), structure)

    def test_prefix_source_structure_and_capacity_tampering_refuse_pending_ddl(self):
        mutations = ("UPDATE waveforms.schema_migrations SET version=7", "UPDATE waveforms.schema_migrations SET sha256=repeat('f',64)",
                     "ALTER TABLE waveforms.facts ADD COLUMN unexpected int", "UPDATE waveforms.capacity SET facts=facts+1 WHERE bucket='GW'")
        for mutation in mutations:
            with self.subTest(mutation=mutation), self.migration_sandbox(v1=True) as conn:
                if mutation.startswith("UPDATE waveforms.schema_migrations"):
                    conn.execute("ALTER TABLE waveforms.schema_migrations DISABLE TRIGGER immutable_content")
                conn.execute(mutation)
                if mutation.startswith("UPDATE waveforms.schema_migrations"):
                    conn.execute("ALTER TABLE waveforms.schema_migrations ENABLE TRIGGER immutable_content")
                with self.assertRaises(WaveformError): migrate({}, expected_database=TEST_DATABASE, limits=LIMITS)
                self.assertIsNone(conn.execute("SELECT to_regclass('waveforms.import_events')").fetchone()[0])

    def test_failed_pending_chain_rolls_back_real_production_transaction(self):
        # This uses the actual connect/commit/rollback implementation, unlike
        # migration_sandbox. Inject a future pending stage after validated002.
        before = self.scalar("SELECT json_agg(t ORDER BY version) FROM waveforms.schema_migrations t", role="migration_owner")
        fact = self.insert_fact(payload=b"synthetic failure preservation")
        source = migration._source
        def broken(filename="001_capture.sql"):
            if filename == "task9_failure.sql":
                text = "CREATE TABLE waveforms.task9_must_rollback(id int); SELECT task9_missing_function();"
                return text, sha256(text.encode()).hexdigest()
            return source(filename)
        with patch.object(migration, "MIGRATIONS", (*migration.MIGRATIONS, (3,"task9_failure.sql"))), patch.object(migration, "_source", side_effect=broken):
            with self.assertRaises(WaveformError):
                migrate(self.settings["migration_owner"], expected_database=TEST_DATABASE, limits=LIMITS)
        self.assertIsNone(self.scalar("SELECT to_regclass('waveforms.task9_must_rollback')", role="migration_owner"))
        self.assertEqual(self.scalar("SELECT json_agg(t ORDER BY version) FROM waveforms.schema_migrations t", role="migration_owner"), before)
        self.assertEqual(self.scalar("SELECT raw_payload FROM waveforms.facts WHERE fact_id=%s", (fact,), role="receiver"), b"synthetic failure preservation")

    def test_migration_idempotent(self):
        fact = self.insert_fact(payload=b" exact bytes \n")
        for _ in range(2):
            result = migrate(self.settings["migration_owner"], expected_database=TEST_DATABASE, limits=LIMITS)
            self.assertFalse(result["applied"])
        self.assertEqual(self.scalar("SELECT raw_payload FROM waveforms.facts WHERE fact_id=%s", (fact,), role="receiver"),
                         b" exact bytes \n")

    def test_facts_immutable(self):
        fact = self.insert_fact()
        for role in ("receiver", "migration_owner"):
            for statement in ("UPDATE waveforms.facts SET raw_payload='bad' WHERE fact_id=%s",
                              "DELETE FROM waveforms.facts WHERE fact_id=%s"):
                with self.subTest(role=role, statement=statement):
                    with self.assertRaises(psycopg.Error):
                        with self.connect(role) as conn: conn.execute(statement, (fact,))

    def test_digest_and_exact_length_checked(self):
        for override in ({"payload_sha256": "0"*64}, {"payload_bytes": 99}, {"raw_payload": b"x"*262145}):
            with self.subTest(override=tuple(override)):
                with self.assertRaises(psycopg.errors.CheckViolation): self.insert_fact(**override)

    def test_gateway_bucket_is_separate_from_claimed_identity(self):
        fact = self.insert_fact()
        self.insert_outcome(fact,gateway_id="UNTRUSTED")
        self.assertEqual(self.scalar("SELECT intake_gateway_id FROM waveforms.facts WHERE fact_id=%s", (fact,), role="receiver"), "GW")
        with self.assertRaises(psycopg.errors.ForeignKeyViolation): self.insert_fact(intake_gateway_id="UNCONFIGURED")

    def test_capacity_and_leases_constrained(self):
        with self.assertRaises(psycopg.errors.CheckViolation):
            with self.connect("receiver") as conn:
                conn.execute("UPDATE waveforms.capacity SET used_raw_bytes=raw_bytes+1 WHERE bucket='GW'")
        with self.assertRaises(psycopg.errors.CheckViolation):
            with self.connect("migration_owner") as conn:
                conn.execute("INSERT INTO waveforms.worker_health(worker_id,component,gateway_id,last_seen) VALUES (%s,'receiver','GW','infinity')", (uuid4(),))

    def test_global_usage_derived_and_never_charged(self):
        with self.connect("receiver") as conn:
            conn.execute("UPDATE waveforms.capacity SET used_raw_bytes=2048,used_facts=2 WHERE bucket='GW'")
        self.assertEqual(self.scalar("SELECT used_raw_bytes FROM waveforms.capacity_metadata WHERE bucket='*'"),2048)
        self.assertEqual(self.scalar("SELECT used_raw_bytes FROM waveforms.capacity WHERE bucket='*'",role="receiver"),0)
        with self.assertRaises(psycopg.errors.CheckViolation):
            with self.connect("receiver") as conn: conn.execute("UPDATE waveforms.capacity SET used_raw_bytes=1 WHERE bucket='*'")

    def test_duplicate_metadata_insert_does_not_consume_capacity(self):
        fact = self.insert_fact()
        outcome = self.insert_outcome(fact)
        with self.connect("receiver") as conn:
            args=(uuid4(),fact,outcome,uuid4())
            statement="INSERT INTO waveforms.observations(observation_id,fact_id,outcome_id,intake_gateway_id,transport,source_ref,first_seen,last_seen) VALUES(%s,%s,%s,'GW','file',%s,CURRENT_TIMESTAMP,CURRENT_TIMESTAMP) ON CONFLICT DO NOTHING"
            conn.execute(statement,args)
            conn.execute(statement,args)
        self.assertEqual(self.scalar("SELECT used_metadata_rows FROM waveforms.capacity WHERE bucket='GW'",role="receiver"),2)

    def test_transport_verdict_never_poison_shared_raw_content(self):
        for statuses in (("quarantined","stored"),("stored","quarantined")):
            with self.subTest(order=statuses):
                fact=self.insert_fact(payload=("synthetic-"+statuses[0]).encode())
                outcomes=[]
                for status in statuses:
                    outcome=self.insert_outcome(fact,status=status,reason_code="accepted" if status=="stored" else "retained_publish",
                        kind="manifest",gateway_id="GW",device_id="D",capture_id=uuid4())
                    outcomes.append(outcome)
                    with self.connect("receiver") as conn:
                        conn.execute("INSERT INTO waveforms.receipts(receipt_id,intake_gateway_id,fact_id,outcome_id,scope,topic,payload,recorded_at) VALUES(%s,'GW',%s,%s,'message','oilfield/v2/GW/D/waveform-ack',%s,CURRENT_TIMESTAMP)",(uuid4(),fact,outcome,b"{}"))
                self.assertEqual(self.scalar("SELECT count(*) FROM waveforms.facts WHERE fact_id=%s",(fact,),role="receiver"),1)
                self.assertEqual(self.scalar("SELECT count(*) FROM waveforms.message_outcomes WHERE fact_id=%s",(fact,),role="receiver"),2)
                self.assertEqual(self.scalar("SELECT count(*) FROM waveforms.receipts WHERE fact_id=%s",(fact,),role="receiver"),2)
                with self.assertRaises(psycopg.Error):
                    with self.connect("receiver") as conn: conn.execute("UPDATE waveforms.message_outcomes SET reason_code='accepted' WHERE outcome_id=%s",(outcomes[0],))

    def test_configuration_change_not_silently_overwritten(self):
        with self.assertRaises(WaveformError):
            migrate(self.settings["migration_owner"], expected_database=TEST_DATABASE,
                    limits=CapacityLimits(global_quota=replace(LIMITS.global_quota, facts=100001), gateways=dict(LIMITS.gateways)))

    def test_orphan_candidates_cannot_take_wrong_manifest_slot(self):
        capture=self.insert_capture()
        with self.connect("receiver") as conn:
            conn.execute("INSERT INTO waveforms.streams(capture_pk,intake_gateway_id,stream_id,manifest_fact_id,specification,chunk_count,decoded_bytes,data_sha256) VALUES(%s,'GW','S',%s,'{}',1,5,%s)",(capture["capture_pk"],capture["manifest_fact_id"],"a"*64))
        candidates=[]
        for manifest_sha in ("0"*64,capture["manifest_sha256"]):
            fact=self.insert_fact(payload=("synthetic chunk "+manifest_sha).encode())
            outcome=self.insert_outcome(fact,status="stored",reason_code="accepted",kind="chunk",gateway_id="GW",device_id="D",capture_id=capture["capture_id"])
            with self.connect("receiver") as conn:
                conn.execute("INSERT INTO waveforms.chunks(fact_id,outcome_id,capture_pk,intake_gateway_id,manifest_sha256,stream_id,chunk_index,sample_start,sample_count,decoded_bytes,data_sha256) VALUES(%s,%s,%s,'GW',%s,'S',0,0,1,5,%s)",(fact,outcome,capture["capture_pk"],manifest_sha,"a"*64))
            candidates.append(fact)
        slot="INSERT INTO waveforms.chunk_slots(capture_pk,intake_gateway_id,stream_id,chunk_index,fact_id) VALUES(%s,'GW','S',0,%s)"
        with self.assertRaises(psycopg.errors.CheckViolation):
            with self.connect("receiver") as conn: conn.execute(slot,(capture["capture_pk"],candidates[0]))
        with self.connect("receiver") as conn: conn.execute(slot,(capture["capture_pk"],candidates[1]))
        self.assertEqual(self.scalar("SELECT fact_id FROM waveforms.chunk_slots WHERE capture_pk=%s",(capture["capture_pk"],),role="receiver"),candidates[1])
        with self.assertRaises(psycopg.errors.InsufficientPrivilege):
            with self.connect("receiver") as conn: conn.execute("UPDATE waveforms.chunk_slots SET fact_id=%s WHERE capture_pk=%s",(candidates[0],capture["capture_pk"]))

    def test_job_lease_requires_consistent_generation(self):
        capture=self.insert_capture()
        with self.connect("receiver") as conn:
            conn.execute("INSERT INTO waveforms.assembly_jobs(capture_pk,intake_gateway_id) VALUES(%s,'GW')",(capture["capture_pk"],))
        for update in ("lease_token=gen_random_uuid()", "finished_generation=2", "lease_token=gen_random_uuid(),lease_generation=2,lease_until=CURRENT_TIMESTAMP"):
            with self.assertRaises(psycopg.errors.CheckViolation):
                with self.connect("assembler") as conn: conn.execute("UPDATE waveforms.assembly_jobs SET "+update+" WHERE capture_pk=%s",(capture["capture_pk"],))
        with self.connect("assembler") as conn:
            conn.execute("UPDATE waveforms.assembly_jobs SET lease_token=gen_random_uuid(),lease_generation=1,lease_until=CURRENT_TIMESTAMP WHERE capture_pk=%s",(capture["capture_pk"],))

    def test_completion_immutable_and_late_conflict_appendable(self):
        capture=self.insert_capture()
        with self.connect("assembler") as conn:
            conn.execute("INSERT INTO waveforms.completion(capture_pk,intake_gateway_id,manifest_fact_id,status,reason_code,result,completed_at) VALUES(%s,'GW',%s,'complete','accepted','{}',CURRENT_TIMESTAMP)",(capture["capture_pk"],capture["manifest_fact_id"]))
            conn.execute("UPDATE waveforms.captures SET state='complete' WHERE capture_pk=%s",(capture["capture_pk"],))
        conflict=self.insert_fact(payload=b"late conflicting manifest")
        with self.connect("receiver") as conn:
            conn.execute("INSERT INTO waveforms.capture_conflicts(conflict_id,capture_pk,intake_gateway_id,fact_id,reason_code,recorded_at) VALUES(%s,%s,'GW',%s,'idempotency_conflict',CURRENT_TIMESTAMP)",(uuid4(),capture["capture_pk"],conflict))
        self.assertTrue(self.scalar("SELECT has_conflicts FROM waveforms.capture_metadata WHERE capture_pk=%s",(capture["capture_pk"],)))
        with self.assertRaises(psycopg.Error):
            with self.connect("assembler") as conn: conn.execute("UPDATE waveforms.completion SET status='invalid' WHERE capture_pk=%s",(capture["capture_pk"],))
        self.assertEqual(self.scalar("SELECT status FROM waveforms.completion WHERE capture_pk=%s",(capture["capture_pk"],),role="assembler"),"complete")

    def test_content_entities_have_immutable_guards(self):
        with self.connect("migration_owner") as conn:
            tables = {row[0] for row in conn.execute("SELECT c.relname FROM pg_trigger t JOIN pg_class c ON c.oid=t.tgrelid JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname='waveforms' AND t.tgname='immutable_content'")}
        self.assertTrue({"facts", "message_outcomes", "streams", "chunks", "completion", "receipts", "import_lines", "audit", "operation_receipts", "capture_conflicts"} <= tables)
