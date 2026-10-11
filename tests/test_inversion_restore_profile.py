"""Fresh recovery boundary tests; no database, discovery or retained-evidence edits."""
from copy import deepcopy
import importlib
import unittest


class FreshRecoveryProfileTests(unittest.TestCase):
    def module(self):
        self.assertIsNotNone(importlib.util.find_spec('tools.inversion_restore_profile'),
            'Fresh mixed-schema recovery must have an explicit closed profile')
        return importlib.import_module('tools.inversion_restore_profile')

    def authority(self):
        return dict(parent=dict(fixture_id='a'*32,roles=dict(migration_owner='wave_owner',reader='wave_reader')),
            sidecar=dict(role='registry_owner'),cluster=dict(roles=dict(admin='test_admin')),
            inversion=dict(parent_fixture_id='a'*32,system_identifier='7688689458460612184',
                roles=dict(owner='inv_owner',reader='inv_reader',operator='inv_operator')))

    def acl(self):
        return [('wave_owner','wave_owner',p,False) for p in ('CONNECT','CREATE','TEMPORARY')]+[
            ('wave_owner',role,'CONNECT',False) for role in ('wave_reader','registry_owner','inv_owner','inv_reader','inv_operator')]

    def fingerprint_security(self):
        value={key:[] for key in ('schemas','relations','columns','constraints','indexes','views','triggers','functions','defaults','extension','database_acl','relation_properties')}
        value.update(database_acl=self.acl(),extension=[['btree_gist','1.8','public','registry_owner']])
        return value

    def fingerprint_boundary(self):
        return dict(schemas=['public','waveforms','device_registry','inversion'],
            relations=[['inversion','results','r',False],['inversion','result_chunks','r',False]],unsupported=[])

    def test_only_fixed_profiles_select_inventory_scope(self):
        module=self.module()
        self.assertEqual(module.schemas('wp04'),('waveforms','device_registry'))
        self.assertEqual(module.schemas('wp05a'),('waveforms','device_registry','inversion'))
        for value in ('all','inversion',None,[],{'schema':'inversion'}):
            with self.subTest(value=value),self.assertRaises(ValueError): module.schemas(value)

    def test_mixed_acl_is_refused_by_legacy_profile(self):
        with self.assertRaises(ValueError): self.module().validate_database_acl(self.acl(),self.authority(),'wp04')

    def test_added_roles_have_exact_connect_grantor_and_no_option(self):
        module=self.module()
        module.validate_database_acl(self.acl(),self.authority(),'wp05a')
        bad=[self.acl()+[('wave_owner','fourth_role','CONNECT',False)],
            self.acl()+[('wave_owner','inv_reader','CREATE',False)],
            self.acl()+[('wave_owner','inv_owner','TEMPORARY',False)],
            self.acl()[:-1],self.acl()+[self.acl()[-1]]]
        for grantor,privilege,grantable in [('test_admin','CONNECT',False),('wave_owner','CONNECT',True)]:
            value=self.acl(); value[-1]=(grantor,'inv_operator',privilege,grantable); bad.append(value)
        for rows in bad:
            with self.subTest(rows=rows),self.assertRaises(ValueError):
                module.validate_database_acl(rows,self.authority(),'wp05a')

    def test_boundary_refuses_unknown_schema_public_user_object_and_unsupported_kind(self):
        module=self.module()
        good=dict(schemas=['public','waveforms','device_registry','inversion'],
            relations=[['inversion','results','r',False],['public','gist_support','i',True]],unsupported=[])
        module.validate_boundary(good,'wp05a')
        bad=[]
        for change in (['new_schema'],['pg_shadow_copy']):
            value=deepcopy(good); value['schemas']+=change; bad.append(value)
        for row in (['public','unmeasured','r',False],['inversion','remote','f',False],['inversion','cache','m',False]):
            value=deepcopy(good); value['relations'].append(row); bad.append(value)
        for kind in ('policy','event_trigger','large_object','foreign_server','publication','user_type'):
            value=deepcopy(good); value['unsupported']=[kind]; bad.append(value)
        for value in bad:
            with self.subTest(value=value),self.assertRaises(ValueError): module.validate_boundary(value,'wp05a')

    def test_binding_cannot_downgrade_or_change_sidecar_archive_or_run(self):
        module=self.module()
        expected=dict(version=2,profile='wp05a',run_id='b'*32,archive_sha256='c'*64,
            parent_fixture_id='a'*32,system_identifier='7688689458460612184',
            roles=dict(owner='inv_owner',reader='inv_reader',operator='inv_operator'))
        module.validate_binding(expected,self.authority(),'b'*32,'c'*64)
        for key,value in [('version',1),('profile','wp04'),('run_id','d'*32),('archive_sha256','e'*64),
            ('parent_fixture_id','f'*32),('system_identifier','1'),('roles',dict(owner='other',reader='inv_reader',operator='inv_operator'))]:
            bad=deepcopy(expected); bad[key]=value
            with self.subTest(key=key),self.assertRaises(ValueError): module.validate_binding(bad,self.authority(),'b'*32,'c'*64)
        with self.assertRaises(ValueError): module.validate_binding(None,self.authority(),'b'*32,'c'*64)

    def test_export_delta_refuses_added_removed_or_modified_inversion_content(self):
        module=self.module()
        before={'inversion.results':{'rows':1,'sha256':'a'*64},'inversion.result_chunks':{'rows':4,'sha256':'b'*64},
            'waveforms.audit':{'rows':1,'sha256':'c'*64}}
        after=deepcopy(before); after['waveforms.audit']={'rows':2,'sha256':'d'*64}
        self.assertEqual(module.content_changes(before,after,'wp05a'),{'waveforms.audit'})
        bad=[]
        value=deepcopy(after); value['inversion.results']['sha256']='e'*64; bad.append(value)
        value=deepcopy(after); value['inversion.result_chunks']['sha256']='f'*64; bad.append(value)
        value=deepcopy(after); del value['inversion.result_chunks']; bad.append(value)
        value=deepcopy(after); value['inversion.extra']={'rows':0,'sha256':'0'*64}; bad.append(value)
        for value in bad:
            with self.subTest(value=value),self.assertRaises(ValueError): module.content_changes(before,value,'wp05a')

    def test_sidecar_and_parent_mismatch_refuse_before_any_credential_lookup(self):
        from unittest.mock import patch
        from tools.provision_inversion_test import sidecar_identity
        module=self.module()
        self.assertTrue(hasattr(module,'add_authority'))
        authority=self.authority(); authority['settings']={}; authority['parent']['data']='C:/pinned/data'
        sidecar=sidecar_identity(authority['parent'],'7688689458460612184')
        variants=[]
        variants.append((dict(authority['cluster'],unexpected=True),authority['parent'],sidecar))
        variants.append((authority['cluster'],dict(authority['parent'],fixture_id='b'*32),sidecar))
        wrong=deepcopy(sidecar); wrong['roles']['reader']='unowned_reader'
        variants.append((authority['cluster'],authority['parent'],wrong))
        wrong=deepcopy(sidecar); wrong['system_identifier']='1'
        variants.append((authority['cluster'],authority['parent'],wrong))
        for value in variants:
            with self.subTest(value=value),patch('tools.provision_inversion_test.load_sidecar',return_value=value),\
                patch('backend.secret_store.read_windows_credential',side_effect=AssertionError('Credential lookup before identity gate')):
                with self.assertRaises(ValueError): module.add_authority(deepcopy(authority),resolve=True)
        with patch('tools.provision_inversion_test.load_sidecar',side_effect=FileNotFoundError),\
            patch('backend.secret_store.read_windows_credential',side_effect=AssertionError('Unexpected credential lookup')):
            with self.assertRaises(FileNotFoundError): module.add_authority(deepcopy(authority),resolve=True)

    def test_sidecar_load_requires_explicit_fixed_cluster_and_only_runtime_credentials(self):
        from unittest.mock import patch
        from types import SimpleNamespace
        from tools.provision_inversion_test import sidecar_identity
        module=self.module(); self.assertTrue(hasattr(module,'add_authority'))
        authority=self.authority(); authority['settings']={}; authority['parent']['data']='C:/pinned/data'
        sidecar=sidecar_identity(authority['parent'],'7688689458460612184'); requests=[]
        def load(*,expected_system_identifier):
            self.assertEqual(expected_system_identifier,'7688689458460612184')
            return authority['cluster'],authority['parent'],sidecar
        def credential(target):
            requests.append(target)
            role=next(role for role,value in sidecar['credential_targets'].items() if value==target)
            self.assertNotEqual(role,'owner')
            return SimpleNamespace(username=sidecar['roles'][role],password='local-test-only')
        with patch('tools.provision_inversion_test.load_sidecar',side_effect=load),\
            patch('backend.secret_store.read_windows_credential',side_effect=credential):
            module.add_authority(authority,resolve=True)
        self.assertEqual(set(authority['settings']),{'inversion_reader','inversion_operator'})
        self.assertEqual(len(requests),2)

    def test_inventory_profile_expands_every_application_security_family_without_changing_legacy_shape(self):
        from tools.waveform_restore import security_inventory
        class Catalog:
            def __init__(self): self.queries=[]
            def execute(self,query,params=()): self.queries.append(query); return self
            def fetchall(self): return []
        legacy=Catalog(); original=security_inventory(legacy)
        expanded=Catalog(); measured=security_inventory(expanded,profile='wp05a')
        for name in ('schemas','relations','columns','constraints','indexes','views','triggers','functions'):
            self.assertNotIn("'inversion'",legacy.queries[list(original).index(name)])
            self.assertIn("'inversion'",expanded.queries[list(measured).index(name)],name)
        self.assertEqual(set(original),{'schemas','relations','columns','constraints','indexes','views','triggers','functions','defaults','extension','database_acl'})
        self.assertIn('relation_properties',measured)

    def test_wrong_profile_inventory_is_refused_before_catalog_access(self):
        from tools.waveform_restore import content_inventory,security_inventory
        class NoCatalog:
            def execute(self,*args,**kwargs): raise AssertionError('Unvalidated inventory scope')
        for function in (content_inventory,security_inventory):
            with self.subTest(function=function),self.assertRaises(ValueError): function(NoCatalog(),profile='arbitrary')

    def test_fresh_fingerprint_requires_bound_profile_catalog_and_complete_inversion(self):
        module=self.module(); self.assertTrue(hasattr(module,'validate_fingerprint'))
        authority=self.authority(); run='b'*32; digest='c'*64
        value=dict(content={'inversion.results':{'rows':1},'inversion.result_chunks':{'rows':1}},
            security=self.fingerprint_security(),properties=['UTF8','C','C','c'],snapshot='snapshot',archive_sha256=digest,
            profile=module.binding(authority,run,digest),boundary=self.fingerprint_boundary())
        module.validate_fingerprint(value,authority,run,digest,'wp05a')
        for field in ('profile','boundary'):
            bad=deepcopy(value); del bad[field]
            with self.subTest(field=field),self.assertRaises(ValueError): module.validate_fingerprint(bad,authority,run,digest,'wp05a')
        bad=deepcopy(value); bad['boundary']['unsupported']=['policy']
        with self.assertRaises(ValueError): module.validate_fingerprint(bad,authority,run,digest,'wp05a')
        bad=deepcopy(value); bad['content']['inversion.result_chunks']['rows']=0
        with self.assertRaises(ValueError): module.validate_fingerprint(bad,authority,run,digest,'wp05a')
        bad=deepcopy(value); bad['boundary']['relations'].append(['inversion','unmeasured','r',False])
        with self.assertRaises(ValueError): module.validate_fingerprint(bad,authority,run,digest,'wp05a')
        bad=deepcopy(value); del bad['security']['columns']
        with self.assertRaises(ValueError): module.validate_fingerprint(bad,authority,run,digest,'wp05a')
        with self.assertRaises(ValueError): module.validate_fingerprint(value,authority,run,digest,'wp04')

    def test_restore_rejects_mixed_legacy_acl_before_target_identity_or_connection(self):
        from unittest.mock import patch
        from tools import waveform_restore as restore
        value=dict(content={},security={'database_acl':self.acl()},properties=['UTF8','C','C','c'],snapshot='s',archive_sha256='c'*64)
        with patch.object(restore,'digest_file',return_value='c'*64),\
            patch.object(restore,'target_identity',side_effect=AssertionError('Target work before ACL validation')),\
            patch.object(restore,'connect_source',side_effect=AssertionError('Connection before validation')):
            with self.assertRaises(ValueError):
                restore._restore_archive(self.authority(),'unused','b'*32,archive='unused',fingerprint=value,guard=lambda:None)

    def test_target_profile_proof_cannot_be_missing_changed_or_downgraded(self):
        import json,tempfile
        from pathlib import Path
        from tools.waveform_restore import digest_file
        module=self.module(); self.assertTrue(hasattr(module,'read_target_proof'))
        authority=self.authority(); run='b'*32
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory); (root/'source.dump').write_bytes(b'archive')
            value=dict(content={'inversion.results':{'rows':1},'inversion.result_chunks':{'rows':1}},
                security=self.fingerprint_security(),properties=['UTF8','C','C','c'],snapshot='s',archive_sha256=digest_file(root/'source.dump'),
                boundary=self.fingerprint_boundary())
            value['profile']=module.binding(authority,run,value['archive_sha256'])
            (root/'source-fingerprint.json').write_text(json.dumps(value),encoding='utf-8')
            with self.assertRaises(ValueError): module.read_target_proof(root,authority,run)
            module.write_target_proof(root,authority,run,value)
            self.assertEqual(module.read_target_proof(root,authority,run),json.loads(json.dumps(value)))
            value['content']['inversion.results']['rows']=2
            (root/'source-fingerprint.json').write_text(json.dumps(value),encoding='utf-8')
            with self.assertRaises(ValueError): module.read_target_proof(root,authority,run)
            with self.assertRaises(ValueError): module.read_target_proof(root,authority,'c'*32)

    def test_added_principal_elevation_membership_or_marker_refuses_source_preflight(self):
        from unittest.mock import patch
        from tools.provision_inversion_test import check_roles
        module=self.module(); authority=self.authority(); authority['parent']['database']='oilwell_waveforms_test'
        class RoleCatalog:
            def __init__(self,row,member): self.row=row; self.member=member
            def execute(self,query,params=()):
                self.value=(self.member,) if 'pg_auth_members' in query else self.row
                return self
            def fetchone(self): return self.value
        for row,member in ((None,False),((1,True,True,'oilwell-inversion-test:'+'a'*32),False),
            ((1,False,False,'oilwell-inversion-test:'+'a'*32),False),((1,False,True,'unowned'),False),
            ((1,False,True,'oilwell-inversion-test:'+'a'*32),True)):
            catalog=RoleCatalog(row,member)
            with self.subTest(row=row,member=member),patch.object(module,'catalog_boundary',return_value={}),\
                patch('backend.waveforms.migrate.check_schema'),patch('backend.registry.migrate.check_schema'),\
                patch('tools.provision_inversion_test.verify_live'),patch('backend.inversion.migrate.check_schema',side_effect=AssertionError('Unsafe roles reached schema')):
                with self.assertRaises(ValueError): module.source_preflight(catalog,authority,'wp05a')

    def test_catalog_boundary_refuses_unmeasured_classes_before_registry_acceptance(self):
        module=self.module()
        class Catalog:
            def __init__(self,label): self.label=label
            def execute(self,query,params=()): self.query=query; return self
            def fetchall(self):
                if 'FROM pg_namespace n WHERE' in self.query: return [(s,) for s in ('public','waveforms','device_registry','inversion')]
                return []
            def fetchone(self): return (self.label in self.query,)
        for catalog in ('pg_policy','pg_event_trigger','pg_largeobject_metadata','pg_foreign_server','pg_foreign_data_wrapper',
            'pg_publication','pg_subscription','pg_seclabel','pg_statistic_ext','pg_transform','pg_cast','pg_ts_config'):
            with self.subTest(catalog=catalog),self.assertRaisesRegex(ValueError,'Unsupported application'):
                module.catalog_boundary(Catalog(catalog),'wp05a')

    def test_rule_query_excludes_system_rules_but_rejects_application_rules(self):
        module=self.module()
        class Catalog:
            def __init__(self,application): self.application=application; self.rule_query=None
            def execute(self,query,params=()): self.query=query; return self
            def fetchall(self):
                if 'FROM pg_namespace n WHERE' in self.query:
                    return [(s,) for s in ('public','waveforms','device_registry','inversion')]
                if 'FROM pg_proc' in self.query:
                    return [(name,'') for name in ('close_period_only','immutable_row','review_once','device_identity_fixed','well_identity_fixed')]
                tables='schema_migrations wells devices audit_events device_state_periods well_bindings point_map_versions point_map_assignments operation_receipts registry_head'.split()
                indexes=[name+'_pkey' for name in tables]+('wells_area_code_well_code_key devices_gateway_id_device_id_key registry_audit_object_time registry_audit_time registry_binding_well_time state_no_overlap binding_no_overlap assignment_no_overlap'.split())
                return [('device_registry',name,kind,False) for kind,names in (('r',tables),('i',indexes)) for name in names]
            def fetchone(self):
                if 'FROM pg_rewrite' not in self.query: return (False,)
                self.rule_query=self.query
                # Query-routing fixture: built-in rules exist in pg_catalog.
                # Actual SQL execution is separately checked by readonly preflight.
                scoped=all(piece in self.query for piece in ('JOIN pg_class c ON c.oid=r.ev_class',
                    'JOIN pg_namespace n ON n.oid=c.relnamespace',"n.nspname NOT IN ('pg_catalog','pg_toast','information_schema')",
                    "n.nspname !~ '^pg_(toast_)?temp_[0-9]+$'","r.rulename<>'_RETURN'"))
                return (not scoped or self.application,)
        builtins=Catalog(False); module.catalog_boundary(builtins,'wp05a')
        self.assertNotIn('pg_settings',builtins.rule_query,'Do not special-case rule names')
        with self.assertRaisesRegex(ValueError,'Unsupported application'):
            module.catalog_boundary(Catalog(True),'wp05a')


if __name__=='__main__': unittest.main()
