"""The extension allowance is an exact vendor contract, never a name escape."""
import unittest
import psycopg
from psycopg import sql
from backend.waveforms.errors import WaveformError
from backend.waveforms.privileges import _permissions,probe_role
from backend.waveforms.registry_dependency import verified_extension_functions
from tests.waveform_support import WaveformDatabaseCase
from tools.provision_waveform_registry_test import sidecar_identity


class RegistryDependencyContractTests(unittest.TestCase):
    def test_frozen_manifest_has_exact_native_definitions(self):
        from backend.waveforms.registry_dependency import expected_functions
        rows=expected_functions()
        self.assertEqual(len(rows),212)
        key=('gbtreekey4_in',('cstring',))
        self.assertEqual(rows[key],('public.gbtreekey4','gbtreekey_in','s'))
        self.assertEqual(rows[('gist_translate_cmptype_btree',('int4',))],('int2','gist_translate_cmptype_btree','s'))
        unsafe={key for key,value in rows.items() if value[2]=='u'}
        self.assertEqual(unsafe,{
            ('gbt_bool_compress',('internal',)),('gbt_bool_fetch',('internal',)),
            ('gbt_bool_consistent',('internal','bool','int2','oid','internal')),
            ('gbt_bool_penalty',('internal','internal','internal')),
            ('gbt_bool_picksplit',('internal','internal')),('gbt_bool_union',('internal','internal')),
            ('gbt_bool_same',('public.gbtreekey2','public.gbtreekey2','internal'))})
        self.assertEqual(rows[('gbt_bool_sortsupport',('internal',))][2],'s')


class RegistryDependencyDatabaseTests(WaveformDatabaseCase):
    def assert_probe_rejects_transaction(self,statements):
        conn=self.admin()
        try:
            for statement in statements: conn.execute(statement)
            conn.execute(sql.SQL('SET LOCAL SESSION AUTHORIZATION {}').format(sql.Identifier(self.manifest['roles']['reader'])))
            self.assertEqual(conn.execute('SELECT current_user,session_user').fetchone(),(self.manifest['roles']['reader'],)*2)
            with self.assertRaises((WaveformError,psycopg.Error)): _permissions(conn,'reader')
        finally:
            conn.rollback()
        # A fresh separately authenticated reader verifies restoration each time.
        probe_role(lambda:self.connect('reader'),'reader')

    def test_exact_cross_owner_views_and_no_base_or_view_writes(self):
        with self.connect('reader') as conn:
            self.assertNotEqual(*conn.execute("SELECT (SELECT nspowner FROM pg_namespace WHERE nspname='waveforms'),(SELECT nspowner FROM pg_namespace WHERE nspname='device_registry')").fetchone())
            conn.execute('SELECT id,gateway_id,device_id,created_at FROM waveforms.registry_devices').fetchall()
        for statement in ('SELECT id FROM device_registry.devices','UPDATE waveforms.registry_devices SET gateway_id=gateway_id',
                          'DELETE FROM waveforms.registry_well_bindings','INSERT INTO waveforms.registry_wells(id) VALUES(gen_random_uuid())'):
            with self.subTest(statement=statement),self.assertRaises(psycopg.errors.InsufficientPrivilege):
                with self.connect('reader') as conn: conn.execute(statement)

    def test_exact_native_function_attribute_mismatches_are_rejected(self):
        target="'public.gbt_bool_compress(internal)'::regprocedure"
        for change in ("probin='$libdir/unreviewed'","prosrc='unreviewed_symbol'", "proparallel='s'",
                       "prolang=(SELECT oid FROM pg_language WHERE lanname='sql')",'prosecdef=true',
                       "proconfig=ARRAY['search_path=public']",'prosupport=1', 'proleakproof=true',
                       "proowner=(SELECT oid FROM pg_roles WHERE rolname='"+sidecar_identity(self.manifest)['role']+"')"):
            with self.subTest(change=change):
                self.assert_probe_rejects_transaction(['UPDATE pg_proc SET '+change+' WHERE oid='+target])

    def test_extra_function_membership_and_extension_identity_rejected(self):
        cases=[
            ["CREATE FUNCTION public.task10_unreviewed() RETURNS integer LANGUAGE SQL AS 'SELECT 1'"],
            ["CREATE FUNCTION public.task10_unreviewed() RETURNS integer LANGUAGE SQL AS 'SELECT 1'",'ALTER EXTENSION btree_gist ADD FUNCTION public.task10_unreviewed()'],
            ["UPDATE pg_extension SET extversion='1.9' WHERE extname='btree_gist'"],
            ["UPDATE pg_extension SET extnamespace='pg_catalog'::regnamespace WHERE extname='btree_gist'"],
            ["ALTER EXTENSION btree_gist DROP FUNCTION public.gbt_bool_compress(internal)"],
        ]
        for statements in cases:
            with self.subTest(statements=statements): self.assert_probe_rejects_transaction(statements)

    def test_grant_option_inherited_owner_and_base_column_escape_rejected(self):
        reader=sql.Identifier(self.manifest['roles']['reader'])
        registry=sql.Identifier(sidecar_identity(self.manifest)['role'])
        owner=sql.Identifier(self.manifest['roles']['migration_owner'])
        for statement in (
            sql.SQL('GRANT EXECUTE ON FUNCTION public.gbt_bool_compress(internal) TO {} WITH GRANT OPTION').format(reader),
            sql.SQL('GRANT {} TO {}').format(registry,reader),
            sql.SQL('GRANT SELECT(id) ON device_registry.devices TO {}').format(reader),
            sql.SQL('GRANT UPDATE(id) ON waveforms.registry_devices TO {}').format(reader),
            sql.SQL('REVOKE SELECT(id) ON device_registry.devices FROM {}').format(owner),
        ):
            with self.subTest(statement=str(statement)): self.assert_probe_rejects_transaction([statement])

    def test_reader_history_revocation_does_not_break_intake_permission_probe(self):
        conn=self.admin()
        try:
            conn.execute(sql.SQL('REVOKE SELECT ON waveforms.registry_devices FROM {}').format(sql.Identifier(self.manifest['roles']['reader'])))
            conn.execute(sql.SQL('SET LOCAL SESSION AUTHORIZATION {}').format(sql.Identifier(self.manifest['roles']['receiver'])))
            _permissions(conn,'receiver')
        finally: conn.rollback()
        probe_role(lambda:self.connect('reader'),'reader')
