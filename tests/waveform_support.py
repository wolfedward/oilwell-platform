"""Pinned live WP-04 fixture; never resets legacy, demo, or registry tables."""
from datetime import datetime, timezone
from contextlib import contextmanager
from dataclasses import fields
from hashlib import sha256
import os
from pathlib import Path
import tomllib
import unittest
from uuid import uuid4
import psycopg
from psycopg import sql
from backend.center.config import _postgres_settings
from backend.secret_store import read_windows_credential
from backend.waveforms.config import CapacityLimits, Quota
from backend.waveforms.contracts import Observation
from backend.waveforms.migrate import migrate, grant_roles
from tools.provision_waveform_test import (OUTPUT, PINNED_IDENTITY, ROLES, TEST_DATABASE,
    config_sections, load_identity, manifest_for, _verify_live)

RUNTIME_ROLES = ROLES[1:]
TEST_LOCK = 499001211
FIXED_TIME = datetime(2026,9,29,tzinfo=timezone.utc)
LIMITS = CapacityLimits(
    global_quota=Quota(raw_bytes=1073741824,facts=100000,attachment_bytes=134217728,
        incomplete_captures=64,orphan_bytes=33554432,quarantine_bytes=16777216,
        metadata_rows=1000000,orphan_bytes_per_capture=524288),
    gateways={"GW": Quota(raw_bytes=268435456,facts=25000,attachment_bytes=33554432,
        incomplete_captures=4,orphan_bytes=2097152,quarantine_bytes=4194304,
        metadata_rows=250000,orphan_bytes_per_capture=524288)})
RESET_TABLES = "dataset_exports dataset_manifest_chunks dataset_files dataset_members dataset_events dataset_versions dataset_revisions dataset_drafts pair_reviews pair_versions quality_runs restrictions evidence_events evidence_versions attachments business_audit business_receipts observations chunk_slots streams chunks capture_conflicts completion assembly_jobs deliveries receipts import_events import_lines imports audit operation_receipts worker_health captures message_outcomes facts".split()


class WaveformDatabaseCase(unittest.TestCase):
    def setUp(self):
        self._validate_waveform_fixture()
        self._lock_waveform_fixture()
        self._migrate_and_grant_waveform_under_lock()
        self._reset_waveform_under_lock()

    def _validate_waveform_fixture(self):
        config = os.environ.get("OILWELL_WAVEFORM_TEST_CONFIG")
        self.assertTrue(config, "Waveform tests require explicit isolated OILWELL_WAVEFORM_TEST_CONFIG")
        self.config_path = Path(config).resolve()
        self.assertEqual(self.config_path, (OUTPUT/"waveform-test.local.toml").resolve())
        self.identity = load_identity(PINNED_IDENTITY)
        self.assertTrue((OUTPUT/"identity.json").is_file(), "Refusing unprovisioned fixture")
        self.manifest = manifest_for(OUTPUT.resolve(), self.identity, database_exists=True)
        self.raw_config = tomllib.loads(self.config_path.read_text(encoding="utf-8"))
        self.assertEqual(self.raw_config, config_sections(self.identity,self.manifest), "Never resolve secrets from modified configuration")
        self.settings = {role:_postgres_settings(self.raw_config["postgres_"+role], self.config_path, os.environ,
                                              read_windows_credential) | {"connect_timeout":3} for role in ROLES}
        self.connections=[]
        self.addCleanup(self._close_connections)
        # Strong admin verification occurs before migration or reset DDL.
        with self.admin() as conn:
            _verify_live(conn,self.identity,database=TEST_DATABASE)
            marker="oilwell-waveform-test:"+self.manifest["fixture_id"]
            self.assertEqual(conn.execute("SELECT pg_get_userbyid(datdba),shobj_description(oid,'pg_database') FROM pg_database WHERE datname=current_database()").fetchone(), (self.settings["migration_owner"]["user"],marker))
    def _lock_waveform_fixture(self):
        self.guard=self.connect("migration_owner")
        self.guard.autocommit=True
        self.guard.execute("SET statement_timeout='30s'")
        self.guard.execute("SELECT pg_advisory_lock(%s)",(TEST_LOCK,))
    def _migrate_and_grant_waveform_under_lock(self):
        migrate(self.settings["migration_owner"],expected_database=TEST_DATABASE,limits=LIMITS,shared_accounting_bucket='GW')
        with self.connect("migration_owner") as conn:
            grant_roles(conn,roles={role:self.settings[role]["user"] for role in ROLES})
    def _reset_waveform_under_lock(self):
        with self.connect("migration_owner") as conn:
            self._check_endpoint(conn)
            conn.execute(sql.SQL("TRUNCATE {} ").format(sql.SQL(",").join(sql.Identifier("waveforms",name) for name in RESET_TABLES)))
            conn.execute("UPDATE waveforms.capacity SET used_raw_bytes=0,reserved_raw_bytes=0,used_facts=0,used_attachment_bytes=0,used_incomplete_captures=0,used_orphan_bytes=0,used_quarantine_bytes=0,used_metadata_rows=0,last_clock_seen=NULL")
            conn.execute("UPDATE waveforms.state_head SET version=1 WHERE singleton=1")

    def _close_connections(self):
        for conn in reversed(self.connections): conn.close()

    def _check_endpoint(self, conn):
        self.assertEqual(conn.execute("SELECT current_database(),host(inet_server_addr()),inet_server_port()").fetchone(), (TEST_DATABASE,"127.0.0.1",3822))

    def connect(self, role="receiver"):
        if role not in ROLES: raise ValueError("Unknown waveform test role")
        conn=psycopg.connect(**self.settings[role])
        self.connections.append(conn)
        self._check_endpoint(conn)
        self.assertEqual(conn.execute("SELECT current_user").fetchone()[0],self.manifest["roles"][role])
        conn.rollback()
        return conn

    def admin(self):
        settings=_postgres_settings(self.raw_config["postgres_test_admin"],self.config_path,os.environ,read_windows_credential)
        conn=psycopg.connect(**(settings|{"connect_timeout":3}))
        self.connections.append(conn)
        self._check_endpoint(conn)
        conn.rollback()
        return conn

    def scalar(self, statement, params=(), *, role="reader"):
        with self.connect(role) as conn: return conn.execute(statement,params).fetchone()[0]

    def insert_fact(self, *, payload=b"bad", role="receiver", **overrides):
        values=dict(fact_id=uuid4(),intake_gateway_id="GW",raw_payload=payload,payload_sha256=sha256(payload).hexdigest(),
                    payload_bytes=len(payload),received_at=FIXED_TIME)
        values.update(overrides)
        with self.connect(role) as conn:
            conn.execute(sql.SQL("INSERT INTO waveforms.facts ({}) VALUES ({})").format(sql.SQL(",").join(map(sql.Identifier,values)),sql.SQL(",").join(sql.Placeholder() for _ in values)),tuple(values.values()))
        return values["fact_id"]

    def insert_outcome(self, fact, *, role="receiver", **overrides):
        values=dict(outcome_id=uuid4(),fact_id=fact,intake_gateway_id="GW",validator_version="OWP-WAVE/1.0",status="quarantined",
                    reason_code="json_invalid",kind=None,gateway_id=None,device_id=None,capture_id=None)
        values.update(overrides)
        with self.connect(role) as conn:
            conn.execute(sql.SQL("INSERT INTO waveforms.message_outcomes ({}) VALUES ({})").format(sql.SQL(",").join(map(sql.Identifier,values)),sql.SQL(",").join(sql.Placeholder() for _ in values)),tuple(values.values()))
        return values["outcome_id"]

    def insert_capture(self):
        capture_id,capture_pk=uuid4(),uuid4()
        payload=b"synthetic manifest "+capture_id.bytes
        fact=self.insert_fact(payload=payload)
        outcome=self.insert_outcome(fact,status="stored",reason_code="accepted",kind="manifest",gateway_id="GW",device_id="D",capture_id=capture_id)
        with self.connect("receiver") as conn:
            conn.execute("INSERT INTO waveforms.captures(capture_pk,intake_gateway_id,gateway_id,device_id,capture_id,manifest_fact_id,manifest_outcome_id,manifest_sha256,first_seen,last_valid_at) VALUES(%s,'GW','GW','D',%s,%s,%s,%s,%s,%s)",
                         (capture_pk,capture_id,fact,outcome,sha256(payload).hexdigest(),FIXED_TIME,FIXED_TIME))
        return dict(capture_pk=capture_pk,capture_id=capture_id,manifest_fact_id=fact,manifest_sha256=sha256(payload).hexdigest())

    def count_facts_for(self, observation: Observation) -> int:
        # Both digest and exact bytes are checked; no reconstruction of wire JSON.
        return self.scalar("SELECT count(*) FROM waveforms.facts WHERE payload_sha256=%s AND raw_payload=%s",
                           (sha256(observation.payload).hexdigest(),observation.payload),role="receiver")

    @contextmanager
    def capacity_fixture(self, limits):
        """Scoped fixture-only budgets under the already-held reset lock.

        Always restore persisted baseline before the next migration check.
        Never relax runtime grants or migration configuration verification.
        """
        def install(configuration):
            with self.connect("migration_owner") as conn:
                self._check_endpoint(conn)
                conn.execute(sql.SQL("TRUNCATE {}").format(sql.SQL(",").join(sql.Identifier("waveforms", name) for name in RESET_TABLES)))
                # Keep immutable business payer binding while varying source quotas.
                conn.execute("UPDATE waveforms.capacity SET used_raw_bytes=0,reserved_raw_bytes=0,used_facts=0,used_attachment_bytes=0,used_incomplete_captures=0,used_orphan_bytes=0,used_quarantine_bytes=0,used_metadata_rows=0,last_clock_seen=NULL")
                buckets={'*':configuration.global_quota,**configuration.gateways}
                buckets.setdefault('GW',LIMITS.gateways['GW'])
                conn.execute('DELETE FROM waveforms.capacity WHERE NOT (bucket=ANY(%s))',(list(buckets),))
                names = [field.name for field in fields(Quota)]
                for bucket, quota in buckets.items():
                    conn.execute(sql.SQL("INSERT INTO waveforms.capacity(bucket,{}) VALUES({}) ON CONFLICT(bucket) DO UPDATE SET {}").format(
                        sql.SQL(",").join(map(sql.Identifier, names)), sql.SQL(",").join(sql.Placeholder() for _ in range(9)),
                        sql.SQL(',').join(sql.SQL('{}=EXCLUDED.{}').format(sql.Identifier(n),sql.Identifier(n)) for n in names)),
                        (bucket, *(getattr(quota, name) for name in names)))
                conn.execute("UPDATE waveforms.state_head SET version=1 WHERE singleton=1")
        try:
            install(limits)
            yield limits
        finally:
            install(LIMITS)
