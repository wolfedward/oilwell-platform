"""Bounded synthetic E2E fixture: no result insertion or substitute worker."""
from hashlib import sha256
from pathlib import Path
from uuid import UUID, uuid4
from types import SimpleNamespace

from backend.inversion.contracts import canonical_bytes, strict_json
from tests.inversion_governance_support import GovernanceCase, command, REVIEWER
from tests.inversion_http_support import HttpRuntimeMixin
from tests.waveform_dataset_support import DatasetCase


class EndToEndCase(HttpRuntimeMixin, GovernanceCase, DatasetCase):
    def setUp(self):
        super().setUp()
        self.start_http()
        self.reviewer=self.server.auth.login('reviewer','Review@2026')
        self.prepare_native_input()

    def prepare_native_input(self):
        from tests.inversion_vectors import acquired_case
        from tests.inversion_success_vector import synthetic_native_cycle
        self.case=acquired_case(self,native=synthetic_native_cycle(),file_first=True,
            alignment_limit=4,utc_uncertainties=(11,11))

    def check_restore_inventory(self,target):
        """Rollback-only sensitivity proof on the newly restored real sentinel."""
        from psycopg import sql
        from tools.waveform_restore import content_inventory,security_inventory,catalog_value
        evidence=[]
        with target.connect('admin') as conn:
            try:
                before_content=content_inventory(conn,profile='wp05a')
                before_security=security_inventory(conn,profile='wp05a')
                self.assertGreater(before_content['inversion.results']['rows'],0)
                self.assertGreater(before_content['inversion.result_chunks']['rows'],0)
                mutations={
                    'result_row':("ALTER TABLE inversion.results DISABLE TRIGGER USER", "UPDATE inversion.results SET body=body||'{\"restore_probe\":true}'::jsonb"),
                    'result_bytea':("ALTER TABLE inversion.result_chunks DISABLE TRIGGER USER", "UPDATE inversion.result_chunks SET payload=payload||decode('01','hex'),content_sha256=encode(sha256(payload||decode('01','hex')),'hex')"),
                    'column_acl':(sql.SQL('GRANT UPDATE(payload) ON inversion.result_chunks TO {}').format(sql.Identifier(target.authority['inversion']['roles']['reader'])),),
                    'owner':(sql.SQL('ALTER TABLE inversion.result_chunks OWNER TO {}').format(sql.Identifier(target.identity['owner'])),),
                    'function':("ALTER FUNCTION inversion.reject_mutation() COST 101",),
                    'trigger':("ALTER TABLE inversion.result_chunks DISABLE TRIGGER immutable",),
                    'native_constraint':("ALTER TABLE inversion.result_chunks ADD CONSTRAINT task14_restore_native CHECK (ordinal<1000000) NOT VALID",),
                    'added_relation':("CREATE TABLE inversion.task14_restore_probe(id integer PRIMARY KEY)",),
                    'removed_relation':("DROP TABLE inversion.result_chunks",),
                }
                for label,statements in mutations.items():
                    conn.execute('SAVEPOINT restore_sensitivity')
                    try:
                        for statement in statements: conn.execute(statement)
                        content=content_inventory(conn,profile='wp05a'); security=security_inventory(conn,profile='wp05a')
                        if label in ('result_row','result_bytea','added_relation','removed_relation'): self.assertNotEqual(content,before_content,label)
                        if label not in ('result_row','result_bytea'): self.assertNotEqual(security,before_security,label)
                        if label in ('added_relation','removed_relation'): self.assertNotEqual(set(content),set(before_content),label)
                    finally: conn.execute('ROLLBACK TO SAVEPOINT restore_sensitivity')
                    self.assertEqual(content_inventory(conn,profile='wp05a'),before_content,label)
                    self.assertEqual(security_inventory(conn,profile='wp05a'),before_security,label)
                    evidence.append(label)
            finally: conn.rollback()
        with target.connect('admin') as conn:
            self.assertEqual(catalog_value(content_inventory(conn,profile='wp05a')),catalog_value(before_content))
            self.assertEqual(catalog_value(security_inventory(conn,profile='wp05a')),catalog_value(before_security))
        return dict(rollback_verified=evidence,unchanged=True)

    def approved_parameter(self, assembly=None, body=None, parent=None):
        # Real distinct authenticated author and reviewer, with persisted events.
        status,reply=self.http('POST','/parameter-versions',self.envelope(body,version=parent))
        self.assertEqual(status,201,reply)
        identifier=reply['data']['subject_id']
        status,reply=self.http('POST','/parameters/'+identifier+'/submit',self.envelope(version=1))
        self.assertEqual(status,200,reply)
        status,reply=self.http('POST','/parameters/'+identifier+'/review',
            self.envelope({'decision':'approve'},version=2),token=self.reviewer.token)
        self.assertEqual(status,200,reply)
        return self.parameters.get(UUID(identifier))

    def completed_result(self):
        from backend.inversion.runtime import worker_services
        from backend.inversion.worker import WorkerRuntime, run_once
        status,reply=self.http('POST','/runs',self.envelope(self.case['request']))
        self.assertEqual(status,202,reply)
        run_id=reply['data']['run_id']
        worker=WorkerRuntime(worker_services,canonical_bytes({'config_path':str(self.runtime_path)}),
            Path(self.tmp.name),uuid4(),enabled=True)
        self.assertTrue(run_once(worker))
        self.assertEqual(list(Path(self.tmp.name).glob('inversion-*')),[])
        status,reply=self.http('GET','/runs/'+run_id)
        self.assertEqual(status,200,reply)
        self.assertEqual(reply['data']['state'],'completed',reply)
        self.assertIsNotNone(reply['data']['result_id'])
        self.assertEqual(tuple(self.inversion_fixture.scalar('SELECT '+column+' FROM inversion.capacity')
            for column in ('queued','running','reserved_bytes')),(0,0,0))
        return self.result_detail(reply['data']['result_id'])

    def result_detail(self,identifier):
        status,reply=self.http('GET','/results/'+identifier)
        self.assertEqual(status,200,reply)
        return reply['data']

    def check_dual_curves(self,result):
        self.assertEqual(self.scalar("SELECT count(*) FROM waveforms.provenance_metadata WHERE transport='mqtt'"),0)
        from backend.waveforms.export import DatasetExporter
        from tests.waveform_support import LIMITS
        from tests.waveform_pairing_support import CREATOR
        exporter=DatasetExporter(lambda:self.connect('operator'),limits=LIMITS,shared_accounting_bucket='GW',read_connect=lambda:self.connect('reader'))
        descriptor=exporter.files(UUID(self.case['request']['dataset_version_id']),actor=CREATOR)[0]
        original=b'\n'.join(self.case['vector']['wire_captures'][0])
        self.assertEqual(descriptor['source_files'][0]['file_sha256'],sha256(original).hexdigest())
        self.assertEqual(descriptor['source_files'][0]['bytes'],len(original))
        status,reply=self.http('GET','/results/'+result['id']+'/points?limit=1000')
        self.assertEqual(status,200,reply)
        page=reply['data']; self.assertEqual(page['content_sha256'],result['content_sha256'])
        self.assertEqual(page['total'],len(page['rows']))
        kinds={row['kind'] for row in page['rows']}
        self.assertIn('prediction',kinds); self.assertIn('comparison',kinds)
        self.assertEqual(sum(row['kind']=='prediction' for row in page['rows']),129)
        self.assertEqual(result['body']['valid_point_count'],124)
        self.assertEqual(result['body']['coverage_fraction'],'61/64')
        self.assertIsNone(result['body']['comparison']['work']['prediction_complete_cycle']['value'])
        self.assertFalse(result['body']['comparison']['independence']['field_accuracy_certified'])
        status,reply=self.http('GET','/results/'+result['id']+'/comparison')
        self.assertEqual(status,200,reply)
        self.assertEqual(reply['data']['content_sha256'],result['content_sha256'])
        self.assertFalse(result['field_validation']['certified'])
        status,reply=self.http('GET','/results/'+result['id']+'?include_input_metadata=true')
        self.assertEqual(status,200,reply)
        self.assertIsNotNone(reply['data']['input_snapshot_metadata']['observation']['relative_timing_terms'])
        from fractions import Fraction
        observation=reply['data']['input_snapshot_metadata']['observation']
        self.assertGreater(Fraction(observation['relative_alignment_ns']),0)
        self.assertGreater(Fraction(observation['uncertainty_ns']),0)

    def review_result(self,result):
        status,reply=self.http('POST','/results/'+result['id']+'/review',
            self.envelope({'decision':'approve'}),token=self.reviewer.token)
        self.assertEqual(status,200,reply)

    def export_result(self,result,intent):
        status,reply=self.http('POST','/exports',self.envelope(dict(
            result_id=result['id'],intent=intent,format='json')))
        self.assertEqual(status,201,reply)
        status,content=self.http_bytes('GET','/exports/'+reply['data']['id'])
        self.assertEqual(status,200,content)
        with self.inversion_fixture.connect('reader') as conn:
            actions={row[0] for row in conn.execute('SELECT action FROM inversion.audit WHERE subject_id=%s',(reply['data']['id'],))}
        self.assertTrue({'export.create','export.access_authorized','export.stream_finished'}<=actions)
        if intent=='evidence': self.assertIn(b'evidence_only',content)
        return content

    def revoke_parameter(self):
        parameter=self.case['parameters']
        status,reply=self.http('POST','/parameters/'+str(parameter.id)+'/revoke',
            self.envelope(version=parameter.review_version),token=self.reviewer.token)
        self.assertEqual(status,200,reply)

    def select_test_only_replacement(self):
        from backend.inversion.models import ModelService, InstalledModel, INSTALLED_MODELS
        from backend.inversion.selections import SelectionService
        assembly=self.case['request']['assembly_id']
        with self.inversion_fixture.connect('reader') as conn:
            original_id=conn.execute('SELECT id FROM inversion.selections WHERE assembly_id=%s AND version=1',(assembly,)).fetchone()[0]
        path=Path(self.tmp.name)/'governance-only-artifact.txt'
        path.write_text('Never executable; synthetic rollback governance only',encoding='utf-8')
        key='test-only-replacement'
        models=ModelService(self.inversion_fixture.connect,read_connect=lambda:self.inversion_fixture.connect('reader'),
            authorize=self.authorize,installed={**INSTALLED_MODELS,key:InstalledModel(key,2,(path,))})
        registered=models.register(key,command()); models.review(registered.subject_id,'approve',command(REVIEWER,1))
        model=models.get(registered.subject_id)
        body=strict_json(canonical_bytes(self.case['parameters'].body)); body['parent_version']=1
        body['policy']['body']['model_version']=key
        body['policy']['content_sha256']=sha256(canonical_bytes(body['policy']['body'])).hexdigest()
        body['provenance']['coverage']['policy_sha256']=body['policy']['content_sha256']
        parameter=self.approved_parameter(assembly,body,1)
        selection=SelectionService(self.inversion_fixture.connect,read_connect=lambda:self.inversion_fixture.connect('reader'),
            authorize=self.authorize,parameters=self.parameters,models=models)
        receipt=selection.select(dict(assembly_id=assembly,parameter_id=str(parameter.id),parameter_sha256=parameter.content_sha256,
            parameter_review_version=parameter.review_version,model_id=str(model.id),model_sha256=model.content_sha256,
            model_review_version=model.review_version),command(version=1))
        return SimpleNamespace(id=original_id),receipt
