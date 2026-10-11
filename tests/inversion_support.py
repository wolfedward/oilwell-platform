"""Pinned fixture: full live gate before any migration, grants or named reset."""
import importlib.util
import os
from pathlib import Path
import unittest
from uuid import uuid4
import psycopg
from psycopg import sql
from backend.center.config import _postgres_settings
from backend.secret_store import read_windows_credential
from tools.provision_inversion_test import (SIDECAR, ROLES, TEST_DATABASE, TEST_LOCK,
    load_sidecar, config_sections, verify_live, check_roles)

RESET_TABLES = 'result_chunks results attempt_events attempts run_events run_state run_requests restrictions result_reviews selections reviews parameter_versions assignment_events assignment_state assignments assembly_versions assemblies model_reviews model_versions audit receipts reservations'.split()
RESET_TABLES = ['result_exports', 'mechanical_events', 'mechanical_state', *RESET_TABLES]


class InversionDatabaseCase(unittest.TestCase):
    def setUp(self):
        self._validate_inversion_fixture()
        self.guard.execute('SELECT pg_advisory_lock(%s)',(TEST_LOCK,))
        self._migrate_and_grant_inversion_under_lock()
        self._reset_inversion_under_lock()

    def _validate_inversion_fixture(self):
        path = os.environ.get('OILWELL_INVERSION_TEST_CONFIG')
        self.assertTrue(path, 'Explicit isolated inversion configuration required')
        self.assertEqual(Path(path).resolve(), (SIDECAR/'inversion-test.local.toml').resolve())
        self.identity,self.parent,self.sidecar = load_sidecar()
        admin = read_windows_credential(self.identity['credential_targets']['admin'])
        self.assertEqual(admin.username,self.identity['roles']['admin'])
        self.admin_settings = dict(host='127.0.0.1',port=3822,dbname=TEST_DATABASE,user=admin.username,password=admin.password,sslmode='disable',connect_timeout=3)
        self.guard = psycopg.connect(**self.admin_settings,autocommit=True)
        self.addCleanup(self.guard.close)
        verify_live(self.guard,self.identity,self.parent,expected_system_identifier=self.sidecar['system_identifier'])
        check_roles(self.guard,self.sidecar)
        config = config_sections(self.sidecar)
        self.settings = {r:_postgres_settings(config['postgres_'+r],Path(path),os.environ,read_windows_credential)|{'connect_timeout':3} for r in ROLES}
        self.guard.execute("SET statement_timeout='3s'")
        self.guard.execute("SET lock_timeout='1s'")
    def _migrate_and_grant_inversion_under_lock(self):
        self.assertIsNotNone(importlib.util.find_spec('backend.inversion.migrate'),'inversion schema foundation required')
        from backend.inversion.migrate import migrate, grant_roles
        from backend.inversion.capacity import InversionLimits
        self.limits = InversionLimits(total_storage_bytes=2*1024**3)
        owner=sql.Identifier(self.sidecar['roles']['owner'])
        try:
            self.guard.execute(sql.SQL('GRANT CREATE ON DATABASE {} TO {}').format(sql.Identifier(TEST_DATABASE),owner))
            migrate(self.settings['owner'],expected_database=TEST_DATABASE,limits=self.limits)
        finally:
            self.guard.execute(sql.SQL('REVOKE CREATE ON DATABASE {} FROM {}').format(sql.Identifier(TEST_DATABASE),owner))
        with self.connect('owner') as conn:
            grant_roles(conn,roles=self.sidecar['roles'])

    def _reset_inversion_under_lock(self):
        with self.connect('owner') as conn:
            conn.execute(sql.SQL('TRUNCATE {}').format(sql.SQL(',').join(sql.Identifier('inversion',name) for name in RESET_TABLES)))
            conn.execute('UPDATE inversion.capacity SET queued=0,running=0,reserved_bytes=0,used_bytes=0,version=version+1 WHERE singleton=1')
            conn.execute('UPDATE inversion.state_head SET version=1 WHERE singleton=1')

    def connect(self,role='operator'):
        return psycopg.connect(**self.settings[role])

    def scalar(self,statement,params=(),role='reader'):
        with self.connect(role) as conn: return conn.execute(statement,params).fetchone()[0]

    def assembly(self,conn=None):
        identifier=uuid4()
        statement="INSERT INTO inversion.assemblies(id,body,content_sha256) VALUES(%s,'{}','"+'a'*64+"')"
        if conn is not None: conn.execute(statement,(identifier,))
        else:
            with self.connect() as target: target.execute(statement,(identifier,))
        return identifier
