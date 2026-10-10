"""Only immutable server selections can identify inversion input."""
import unittest
from uuid import uuid4
from backend.inversion.contracts import canonical_bytes
from backend.inversion.errors import InversionContractError
from backend.inversion.input_contracts import parse_run_request
from tests.inversion_governance_support import GovernanceCase,AUTHOR
from tests.waveform_dataset_support import DatasetCase
from tests.inversion_vectors import acquired_case


def request_body():
    value={key:str(uuid4()) for key in ('dataset_version_id','pair_id','assembly_id','parameter_id','model_id')}
    value.update({key:'a'*64 for key in ('dataset_sha256','pair_sha256','parameter_sha256','model_sha256')})
    value.update(member_ordinal=0,pair_review_version=2,parameter_review_version=3,
                 model_review_version=2,selection_version=1,operating_condition_ids=['condition-1'])
    return value


class InputContractTests(unittest.TestCase):
    def test_archive_sync_pin_is_optional_without_rewriting_legacy_body(self):
        from copy import deepcopy
        from backend.inversion.parameter_contracts import parse_parameter_set
        from tests.inversion_vectors import parameter_body,parameter_wire
        body=parameter_body(); original=deepcopy(body)
        self.assertIsNone(parse_parameter_set(parameter_wire(body)).phase.channel_pin.sync_pin)
        self.assertEqual(body,original)
        pin=dict(id=str(uuid4()),content_sha256='a'*64,review_version=2)
        body['phase']['channel_pin']['sync_pin']=pin
        body['provenance']['channel_bindings'][1]['channel_pin']['sync_pin']=pin
        self.assertEqual(str(parse_parameter_set(parameter_wire(body)).phase.channel_pin.sync_pin.id),pin['id'])
        body['phase']['channel_pin']['sync_pin']=None
        body['provenance']['channel_bindings'][1]['channel_pin']['sync_pin']=None
        self.assertIsNone(parse_parameter_set(parameter_wire(body)).phase.channel_pin.sync_pin)

    def test_conditions_are_immutable_ids_and_request_has_no_client_arrays(self):
        body=request_body(); parsed=parse_run_request(canonical_bytes(body))
        self.assertEqual(parsed.operating_condition_ids,('condition-1',))
        for changes in ({'temperature_K':'300'},{'samples':[1,2,3]},
                        {'operating_condition_ids':[{'id':'condition-1','temperature_K':'300'}]},
                        {'operating_condition_ids':['condition-1','condition-1']}, {'member_ordinal':1000}):
            with self.subTest(changes=changes), self.assertRaises(InversionContractError):
                parse_run_request(canonical_bytes(body|changes))


class AcquisitionTests(GovernanceCase,DatasetCase):
    def acquirer(self,case):
        from backend.inversion.input_acquisition import InputAcquisition
        return InputAcquisition(self.inversion_fixture.connect,
            read_connect=lambda:self.inversion_fixture.connect('reader'),exporter=case['exporter'],
            authorize=self.authorize,accounting_bucket='GW')

    def test_canonical_hash_all_chunks_and_single_calibration(self):
        from hashlib import sha256
        from fractions import Fraction
        case=acquired_case(self)
        from unittest.mock import patch
        reads=[]; raw=case['exporter']._raw
        def audited(fact,actor,**kwargs):
            reads.append(fact); return raw(fact,actor,**kwargs)
        with patch.object(case['exporter'],'_raw',side_effect=audited):
            prepared=self.acquirer(case).prepare_input(parse_run_request(canonical_bytes(case['request'])),AUTHOR,check=lambda:None)
        self.assertEqual(len(reads),11)  # Manifest + five electrical + five sensor chunks.
        self.assertEqual(len(set(reads)),11)
        self.assertEqual(prepared.prediction_inputs.power.samples[0].value_si,Fraction(-5))
        self.assertEqual(prepared.prediction_inputs.power.samples[0].time_ns,Fraction(17))
        self.assertEqual(prepared.reference_inputs.load.samples[0].value_si,Fraction(10000))
        self.assertEqual(len(prepared.prediction_inputs.crank_angle.samples),129)
        self.assertEqual(prepared.snapshot.content_sha256,sha256(prepared.snapshot.canonical).hexdigest())
        self.assertFalse(prepared.snapshot.body['applicability']['independent_comparison'])
        self.assertFalse(prepared.snapshot.body['applicability']['field_accuracy_certified'])
        with self.assertRaises(TypeError): prepared.snapshot.body['selection']['channels'][0]['role']='load'

        # Corruption in the last original chunk cannot be hidden by selected values.
        def corrupt(fact,actor,**kwargs):
            value=raw(fact,actor,**kwargs)
            return value+b' ' if fact==reads[-1] else value
        from backend.waveforms.errors import WaveformError
        with patch.object(case['exporter'],'_raw',side_effect=corrupt),self.assertRaises(WaveformError):
            self.acquirer(case).prepare_input(parse_run_request(canonical_bytes(case['request'])),AUTHOR,check=lambda:None)

    def test_unchanged_installation_accepts_new_capture_without_reading_archive(self):
        from unittest.mock import patch
        case=acquired_case(self,later=True); exporter=case['exporter']; read_ids=[]; original=exporter._raw
        def read(fact,actor,**kwargs):
            read_ids.append(fact); return original(fact,actor,**kwargs)
        with patch.object(exporter,'_raw',side_effect=read):
            prepared=self.acquirer(case).prepare_input(parse_run_request(canonical_bytes(case['request'])),AUTHOR,check=lambda:None)
        self.assertTrue(prepared.snapshot.body['applicability']['independent_comparison'])
        selected=case['vector']['frames'][0].key
        with self.connect('reader') as conn:
            found=conn.execute('SELECT c.capture_id FROM waveforms.fact_metadata f JOIN waveforms.outcome_metadata o USING(fact_id) JOIN waveforms.capture_metadata c USING(gateway_id,device_id,capture_id) WHERE f.fact_id=ANY(%s)',(read_ids,)).fetchall()
        self.assertEqual({r[0] for r in found},{selected.capture_id})

    def test_frozen_selection_cannot_gain_angle(self):
        from backend.inversion.transactions import InversionError
        case=acquired_case(self,angle=False)
        with self.assertRaises(InversionError) as error:
            self.acquirer(case).prepare_input(parse_run_request(canonical_bytes(case['request'])),AUTHOR,check=lambda:None)
        self.assertEqual(error.exception.code,'inversion.required_role_missing')

    def test_legacy_archive_without_sync_remains_computable_but_independence_unproven(self):
        case=acquired_case(self,later=True,archive_sync=False)
        prepared=self.acquirer(case).prepare_input(parse_run_request(canonical_bytes(case['request'])),AUTHOR,check=lambda:None)
        self.assertFalse(prepared.snapshot.body['applicability']['independent_comparison'])
        self.assertIn('archived_sync_budget_unproven',prepared.snapshot.body['applicability']['independence_reasons'])
        self.assertEqual(len(prepared.prediction_inputs.crank_angle.samples),129)

    def test_current_check_uses_same_transaction(self):
        import psycopg
        from dataclasses import asdict
        from psycopg.types.json import Jsonb
        from backend.inversion.history import recheck_with_cursor
        from backend.inversion.transactions import lock,LockPlan
        case=acquired_case(self,later=True)
        prepared=self.acquirer(case).prepare_input(parse_run_request(canonical_bytes(case['request'])),AUTHOR,check=lambda:None)
        with self.inversion_fixture.connect() as conn:
            cur=conn.cursor(); lock(cur,LockPlan(waveform=True,registry=True))
            self.assertEqual(recheck_with_cursor(cur,prepared.snapshot),())
            selected_ids={item['descriptor']['capture_pk'] for item in prepared.snapshot.body['captures']}
            archived_ids={item['capture_pk'] for item in prepared.snapshot.body['applicability']['archived_channels']}-selected_ids
            self.assertEqual((len(selected_ids),len(archived_ids)),(1,1))
            for source,capture_id in [('selected',id) for id in selected_ids]+[('archived_only',id) for id in archived_ids]:
                with self.subTest(source=source),self.assertRaises(psycopg.errors.LockNotAvailable):
                    with self.connect('migration_owner') as competing:
                        competing.execute('SELECT capture_pk FROM waveforms.captures WHERE capture_pk=%s FOR UPDATE NOWAIT',(capture_id,))
            cur.execute('INSERT INTO inversion.reviews(id,subject_id,version,action,actor,reason,body) VALUES(%s,%s,4,\'revoke\',%s,\'Uncommitted same-cursor test\',%s)',
                (uuid4(),case['parameters'].id,Jsonb(asdict(AUTHOR)),Jsonb({})))
            self.assertIn('parameter_pin_changed',recheck_with_cursor(cur,prepared.snapshot))
            conn.rollback()
        with self.inversion_fixture.connect() as conn:
            cur=conn.cursor(); lock(cur,LockPlan(waveform=True,registry=True))
            self.assertEqual(recheck_with_cursor(cur,prepared.snapshot),())

    def test_unavailable_archived_pins_refuse_without_extra_originals(self):
        from dataclasses import replace
        from backend.inversion.history import archived_channels
        from backend.inversion.parameter_contracts import parse_parameter_set
        from backend.inversion.transactions import InversionError
        case=acquired_case(self,later=True)
        parameters=parse_parameter_set(canonical_bytes(case['parameters'].body))
        selected={item.role:item.channel_pin for item in parameters.provenance.channel_bindings}
        for change in ({'capture_id':uuid4()},{'manifest_sha256':'f'*64},{'channel_id':'absent'},{'last_index':1000000}):
            bindings=list(parameters.provenance.channel_bindings)
            bindings[0]=replace(bindings[0],channel_pin=replace(bindings[0].channel_pin,**change))
            bad=replace(parameters,provenance=replace(parameters.provenance,channel_bindings=tuple(bindings)))
            with self.subTest(change=change),self.inversion_fixture.connect('reader') as conn,self.assertRaises(InversionError):
                archived_channels(conn.cursor(),bad,selected,bucket='GW')
        # Metadata-seam negative, not a claim of a second genuine raw lifecycle:
        # a recorder key must not silently replace a different producer device.
        from copy import deepcopy
        from unittest.mock import patch
        from backend.waveforms.capacity import row
        def other_producer(cur):
            value=row(cur)
            if value is not None:
                value=deepcopy(value); value['specification']['source']['device_id']='different-producer'
            return value
        with self.inversion_fixture.connect('reader') as conn,patch('backend.inversion.history.row',side_effect=other_producer):
            with self.assertRaises(InversionError) as denied:
                archived_channels(conn.cursor(),parameters,selected,bucket='GW')
        self.assertEqual(denied.exception.code,'inversion.archive_channel_changed')

    def test_archive_sync_pin_missing_revoked_endpoint_range_and_drift(self):
        from dataclasses import replace
        from backend.inversion.history import archived_channels,calibration_independence
        from backend.inversion.parameter_contracts import parse_parameter_set
        from backend.inversion.series import EvidencePin
        from backend.inversion.transactions import InversionError
        from backend.waveforms.evidence_contracts import thaw
        from fractions import Fraction
        case=acquired_case(self,later=True); parameters=parse_parameter_set(canonical_bytes(case['parameters'].body))
        selected={item.role:item.channel_pin for item in parameters.provenance.channel_bindings}
        def with_sync(pin):
            return replace(parameters,provenance=replace(parameters.provenance,channel_bindings=tuple(
                replace(item,channel_pin=replace(item.channel_pin,sync_pin=pin)) for item in parameters.provenance.channel_bindings)))
        def archive(value):
            with self.inversion_fixture.connect('reader') as conn: return archived_channels(conn.cursor(),value,selected,bucket='GW')
        missing=archive(with_sync(None))
        self.assertTrue(all(a['timing']['total_bound_ns'] is None for a in missing))
        sync=case['archive']['sync']; proven=archive(parameters)
        self.assertTrue(all(a['timing']['status']=='verified_mapping_bound' for a in proven))
        for change in ('endpoint','range','drift'):
            body=thaw(sync.body)
            if change=='range': body['endpoints'][1]['valid_end_ns']='1'
            elif change=='drift': body['endpoints'][1]['frequency_error_ppm']='100000000'
            version=case['vector']['sync'] if change=='endpoint' else self.approved_evidence('sync',body)
            modified=with_sync(EvidencePin(version.id,version.content_sha256,version.version))
            if change!='drift':
                with self.subTest(change=change),self.assertRaises(InversionError): archive(modified)
            else:
                rows=archive(modified); start=Fraction(rows[0]['absolute_start_ns'])+60000000000
                self.assertIn('calibration_time_overlap',calibration_independence(modified,rows,'other-cycle',start,start+2000000000,Fraction(0),[]))
                common=[a for a in rows if a['role'] in ('load','displacement')]
                self.assertEqual(len({a['specification']['stream_id'] for a in common}),1)
                self.assertTrue(all(Fraction(a['timing']['endpoint_bound_ns'])>0 for a in common))
                for shared in (common[:1],common):
                    self.assertIn('calibration_time_overlap',calibration_independence(modified,shared,'other-cycle',start,start+2000000000,Fraction(0),[]))
        command=case['archive']['evidence_command']
        service=case['archive']['evidence_service']
        service.revoke(sync.id,command=command('revoke',sync.id,sync.version))
        with self.assertRaises(InversionError): archive(parameters)
        with self.assertRaises(InversionError): self.parameters.check_current(case['parameters'].id)

    def test_operator_metadata_projection_cannot_read_originals(self):
        import psycopg
        from backend.inversion.metadata import probe_metadata
        probe_metadata(self.inversion_fixture.connect)
        for query in ('SELECT raw_payload FROM waveforms.facts', 'SELECT canonical_bytes FROM device_registry.point_map_versions',
                      "SELECT waveforms.audited_raw(gen_random_uuid(),'{}'::jsonb)"):
            with self.subTest(query=query),self.assertRaises(psycopg.errors.InsufficientPrivilege):
                with self.inversion_fixture.connect() as conn: conn.execute(query)
