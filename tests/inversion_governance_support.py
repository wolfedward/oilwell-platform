"""Combined fixture; separate role maps, one session reset lock, real evidence writes."""
from copy import deepcopy
from dataclasses import replace
from hashlib import sha256
from uuid import uuid4
from backend.inversion.contracts import InversionCommand, VersionPrecondition, canonical_bytes
from backend.waveforms.commands import Actor, Command, evidence_request_hash
from backend.waveforms.evidence import EvidenceService
from tests.waveform_support import WaveformDatabaseCase, LIMITS
from tests.waveform_registry_support import WaveformRegistryCase, REGISTRY_DATA
from tests.inversion_support import InversionDatabaseCase
from tests.inversion_vectors import parameter_body
from tests.test_waveform_evidence import sampling_body

AUTHOR = Actor('local', 'author', 'Author', 'algorithm_engineer')
REVIEWER = Actor('local', 'reviewer', 'Reviewer', 'reviewer')
START = '2026-01-01T00:00:00.000000Z'
END = '2027-01-01T00:00:00.000000Z'
MID = '2026-07-01T00:00:00.000000Z'


def command(actor=AUTHOR, version=None, key=None):
    return InversionCommand(actor, key or uuid4(), VersionPrecondition('absent') if version is None
                            else VersionPrecondition('match', version), 'Explicit synthetic governance test')


class GovernanceCase(WaveformRegistryCase):
    def setUp(self):
        self._validate_waveform_fixture()
        self.inversion_fixture = InversionDatabaseCase()
        self.addCleanup(self.inversion_fixture.doCleanups)
        self.inversion_fixture._validate_inversion_fixture()
        from tools.provision_waveform_registry_test import owner_settings, sidecar_identity
        self.registry_settings=owner_settings()
        expected=sidecar_identity(self.manifest)
        self.assertEqual(self.registry_settings['user'],expected['role'])
        with self.registry_connect() as conn:
            self.assertEqual(conn.execute("SELECT pg_get_userbyid(nspowner) FROM pg_namespace WHERE nspname='device_registry'").fetchone(),(expected['role'],))
        self._lock_waveform_fixture()
        self._migrate_and_grant_waveform_under_lock()
        self.inversion_fixture._migrate_and_grant_inversion_under_lock()
        from backend.inversion.metadata import grant_metadata, grant_registry_metadata, probe_metadata
        with self.connect('migration_owner') as conn:
            grant_metadata(conn, roles=self.inversion_fixture.sidecar['roles'])
        with self.registry_connect() as conn:
            grant_registry_metadata(conn, roles=self.inversion_fixture.sidecar['roles'])
        self.inversion_fixture._reset_inversion_under_lock()
        self._reset_waveform_under_lock()
        with self.registry_connect() as conn:
            conn.execute('TRUNCATE '+','.join('device_registry.'+n for n in REGISTRY_DATA))
            conn.execute('UPDATE device_registry.registry_head SET revision=1 WHERE singleton=1')
        probe_metadata(lambda: self.inversion_fixture.connect('reader'))
        probe_metadata(self.inversion_fixture.connect)
        self.allowed = True
        from backend.inversion.parameters import ParameterService
        self.parameters = ParameterService(self.inversion_fixture.connect,
            read_connect=lambda: self.inversion_fixture.connect('reader'),
            authorize=self.authorize, accounting_bucket='GW')
        self.evidence = EvidenceService(lambda: self.connect('operator'), limits=LIMITS,
            shared_accounting_bucket='GW', read_connect=lambda: self.connect('reader'))
        payload=b'%PDF-1.7 Synthetic engineering report with explicitly known bounds'
        attachment=dict(media_type='application/pdf',payload_bytes=len(payload),payload_sha256=sha256(payload).hexdigest())
        self.attachment = self.evidence.upload_attachment(payload,media_type='application/pdf',
            command=self.wave_command('upload',attachment=attachment))

    def authorize(self, actor, capability):
        from backend.inversion.transactions import InversionError
        if not self.allowed: raise InversionError('forbidden', status=403)

    def wave_command(self, action, target=None, version=0, actor=AUTHOR, **body):
        value=Command(actor,uuid4(),version,'Synthetic evidence governance','0'*64)
        return replace(value,request_hash=evidence_request_hash(action,target,value,**body))

    def pin(self, purpose):
        return dict(id=str(self.attachment.id),content_sha256=self.attachment.sha256,
                    purpose=purpose,source_description='Synthetic report, section '+purpose)

    def assembly_body(self, **changes):
        body=dict(assembly_id=str(uuid4()),manufacturer='Software laboratory',model='Known four-bar',
            asset_number='TEST-001',mechanism='conventional_crank_balanced',source_kind='synthetic',
            source_organization='Software test',valid_from=START,valid_to=None,parent_version=None,
            installation_evidence=[self.pin('installation')])
        return body | changes

    def create_assembly(self, **changes):
        body=self.assembly_body(**changes)
        receipt=self.parameters.create_assembly(body,command())
        return receipt,body

    def body(self, assembly, mechanical=1, parent=None):
        body=parameter_body(); body.update(assembly_id=str(assembly),assembly_version=mechanical,parent_version=parent)
        for group in ('geometry','phase','bodies','drive','domain','uncertainty'):
            body[group]['evidence_refs']=[self.pin(group)]
        body['drive']['loss_grid']['evidence_refs']=[self.pin('loss_grid')]
        body['provenance']['attachment_pins']=[self.pin('provenance')]
        for item in body['provenance']['operating_conditions']: item['attachment_pins']=[self.pin('operating_conditions')]
        for role in body['provenance']['channel_bindings']:
            role['installation_evidence_refs']=[self.pin('installation')]
            sampling=sampling_body()
            sampling.update(valid_from=START,attachment_ids=[str(self.attachment.id)])
            sampling['endpoint'].update(gateway_id='gateway-1',device_id='device-1',stream_id='stream-1',channel_id=role['role'])
            units={'active_power':('total_active_power','W'),'crank_angle':('crank_angle','rad'),
                   'load':('load','N'),'displacement':('displacement','m')}
            sampling['quantity'],sampling['unit']=units[role['role']]
            evidence=self.approved_evidence('sampling_config',sampling)
            calibration=dict(source_kind='synthetic',valid_from=START,valid_to=None,
                attachment_ids=[str(self.attachment.id)],notes='Synthetic known calibration',
                endpoint=sampling['endpoint'],sampling_config_version_id=str(evidence.id),
                sampling_config_sha256=evidence.content_sha256,scale='1',offset='0',unit=sampling['unit'],
                calibrated_range=dict(min='-100000',max='100000',unit=sampling['unit']),zero='0',direction='positive',
                measurement_uncertainty='0',time_correction_ns='0',residual_ns='0',method='Known synthetic',
                basis='Known controlled values',calibrated_at=START)
            cal=self.approved_evidence('calibration',calibration)
            role['channel_pin']['channel_id']=role['role']
            role['channel_pin']['sampling_pin']=dict(id=str(evidence.id),content_sha256=evidence.content_sha256,review_version=evidence.version)
            role['channel_pin']['calibration_pin']=dict(id=str(cal.id),content_sha256=cal.content_sha256,review_version=cal.version)
        body['phase']['channel_pin']=deepcopy(body['provenance']['channel_bindings'][1]['channel_pin'])
        # The fixture uses explicit in-domain synthetic operating conditions.
        for key in ('temperature_K','supply_voltage_V','supply_frequency_Hz'):
            body['domain'][key]=dict(min='0',max='1000')
        coverage=body['provenance']['coverage']
        body['uncertainty']['numerical_factor_budget_m']='0.000001'
        body['uncertainty']['numerical_budget_Nm']='0.0001'
        for entry in coverage['scopes']: entry['attachment_pin']=self.pin(entry['scope'])
        for key in ('domain','uncertainty'):
            coverage[key+'_sha256']=sha256(canonical_bytes(body[key])).hexdigest()
        return body

    def approved_evidence(self, kind, body):
        evidence=self.evidence.create(kind,body,self.wave_command('create',kind=kind,body=body))
        return self.evidence.review(evidence.id,decision='approve',command=self.wave_command('review',
            evidence.id,version=1,actor=REVIEWER,decision='approve'))

    def approved_parameter(self, assembly=None, body=None, parent=None):
        if assembly is None: assembly=self.create_assembly()[0].subject_id
        body=self.body(assembly,parent=parent) if body is None else body
        created=self.parameters.create_version(body,command(version=parent))
        self.parameters.submit(created.subject_id,command(version=1))
        self.parameters.review(created.subject_id,'approve',command(REVIEWER,2))
        return self.parameters.get(created.subject_id)

    def inv_scalar(self, query, args=()):
        return self.inversion_fixture.scalar(query,args)
