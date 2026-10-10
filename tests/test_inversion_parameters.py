"""Governance integration against isolated real evidence metadata and PostgreSQL."""
import importlib.util
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from uuid import uuid4
from tests.inversion_governance_support import GovernanceCase, command, AUTHOR, REVIEWER, START, END, MID
from backend.inversion.transactions import InversionError


class PhysicalSupportTests(unittest.TestCase):
    def test_closed_observation_support_uses_exact_fractional_nanoseconds(self):
        from fractions import Fraction
        from backend.inversion.parameters import covers_observation, contains_interval
        start=datetime(1970,1,1,tzinfo=timezone.utc)
        end=start+timedelta(microseconds=1)
        self.assertTrue(contains_interval(start,end,start,end))
        self.assertTrue(covers_observation(start,end,0,0,0))
        self.assertTrue(covers_observation(start,end,0,Fraction(9999,10),0))
        self.assertFalse(covers_observation(start,end,0,1000,0))
        self.assertFalse(covers_observation(start,end,1,Fraction(9999,10),Fraction(1,10)))
        self.assertFalse(covers_observation(start,end,0,1,Fraction(1,10)))
        for args in ((1,0,0),(0,1,-1),(0.0,1,0),(0,1,True)):
            with self.subTest(args=args), self.assertRaises(ValueError): covers_observation(start,end,*args)


class ParameterGovernanceTests(GovernanceCase):
    def test_broader_condition_record_retains_own_interval(self):
        from backend.inversion.parameters import covers_observation, _ns
        from backend.inversion.contracts import utc
        assembly,_=self.create_assembly(valid_from=MID)
        body=self.body(assembly.subject_id); body['valid_from']=MID
        approved=self.approved_parameter(assembly.subject_id,body)
        condition=approved.body['provenance']['operating_conditions'][0]
        self.assertEqual(condition['valid_from'],START)
        self.assertEqual(approved.body['valid_from'],MID)
        self.assertFalse(covers_observation(utc(condition['valid_from']),utc(condition['valid_to']),
            _ns(utc(END)),_ns(utc(END)),0))

    def test_independent_lab_organization_is_preserved_without_replacing_identity(self):
        assembly,initial=self.create_assembly(source_organization='Installation organization')
        body=self.body(assembly.subject_id)
        body['provenance']['source_organization']='Independent engineering laboratory'
        record=self.approved_parameter(assembly.subject_id,body)
        self.assertEqual(record.body['provenance']['source_organization'],'Independent engineering laboratory')
        self.parameters.create_assembly(initial|dict(parent_version=1,valid_from=MID,
            source_organization='Successor installation organization'),command(version=1))
        self.assertEqual(self.inv_scalar("SELECT body->>'source_organization' FROM inversion.assembly_versions WHERE assembly_id=%s AND version=1",(assembly.subject_id,)),'Installation organization')
        mismatch=self.body(assembly.subject_id,mechanical=2,parent=1)
        mismatch['source_kind']='bench_measurement'; mismatch['valid_from']=MID
        draft=self.parameters.create_version(mismatch,command(version=1))
        with self.assertRaises(InversionError): self.parameters.submit(draft.subject_id,command(version=1))

    def test_metadata_installer_refuses_receiver_as_inversion_runtime(self):
        from backend.inversion.metadata import grant_metadata
        roles=self.inversion_fixture.sidecar['roles'] | {'operator':self.manifest['roles']['receiver']}
        with self.connect('migration_owner') as conn:
            try:
                with self.assertRaises(InversionError): grant_metadata(conn,roles=roles)
            finally: conn.rollback()

    def test_separate_submitter_cannot_review_and_revocation_is_terminal(self):
        assembly,_=self.create_assembly()
        created=self.parameters.create_version(self.body(assembly.subject_id),command())
        self.parameters.submit(created.subject_id,command(REVIEWER,1))
        with self.assertRaises(InversionError): self.parameters.review(created.subject_id,'approve',command(REVIEWER,2))
        independent=replace(REVIEWER,user_id='third')
        self.parameters.review(created.subject_id,'approve',command(independent,2))
        self.parameters.revoke(created.subject_id,command(independent,3))
        self.assertEqual(self.parameters.get(created.subject_id).review_state,'revoked')
        with self.assertRaises(InversionError): self.parameters.check_current(created.subject_id)
        with self.assertRaises(InversionError): self.parameters.review(created.subject_id,'approve',command(independent,4))

    def test_real_well_assignment_uses_registry_status_and_preserves_source(self):
        well=self.registry_command('well.create',area_code='A',well_code='P1',name='Governance fixture well')['entity']
        assembly,_=self.create_assembly(source_kind='bench_measurement')
        body=dict(assembly_id=str(assembly.subject_id),well_id=well['id'],valid_from=START,valid_to=MID,close_assignment_id=None)
        first=self.parameters.assign(body,command())
        self.assertEqual(self.inv_scalar('SELECT well_id FROM inversion.assignments WHERE id=%s',(first.subject_id,)),well['id'])
        with self.registry_connect() as conn: conn.execute("UPDATE device_registry.wells SET status='archived' WHERE id=%s",(well['id'],))
        with self.assertRaises(InversionError): self.parameters.assign(body|dict(valid_from=MID,valid_to=END),command())
        self.assertEqual(self.inv_scalar("SELECT body->>'source_kind' FROM inversion.assemblies WHERE id=%s",(assembly.subject_id,)),'bench_measurement')

    def test_approval_receipt_failure_rolls_back_review_and_audit(self):
        assembly,_=self.create_assembly(); draft=self.parameters.create_version(self.body(assembly.subject_id),command())
        self.parameters.submit(draft.subject_id,command(version=1))
        before=self.inv_scalar('SELECT count(*) FROM inversion.audit')
        case=self
        class Cursor:
            def __init__(self, inner): self.inner=inner
            def __enter__(self): self.inner.__enter__(); return self
            def __exit__(self,*args): return self.inner.__exit__(*args)
            def __getattr__(self,key): return getattr(self.inner,key)
            def execute(self, query, args=None):
                if query.startswith('INSERT INTO inversion.receipts'): raise RuntimeError('Injected lost transaction')
                return self.inner.execute(query,args)
        class Connection:
            def __init__(self): self.inner=case.inversion_fixture.connect()
            def __enter__(self): self.inner.__enter__(); return self
            def __exit__(self,*args): return self.inner.__exit__(*args)
            def __getattr__(self,key): return getattr(self.inner,key)
            def cursor(self): return Cursor(self.inner.cursor())
        from backend.inversion.parameters import ParameterService
        failing=ParameterService(Connection,read_connect=lambda:self.inversion_fixture.connect('reader'),authorize=self.authorize,accounting_bucket='GW')
        with self.assertRaises(RuntimeError): failing.review(draft.subject_id,'approve',command(REVIEWER,2))
        self.assertEqual(self.parameters.get(draft.subject_id).review_state,'submitted')
        self.assertEqual(self.inv_scalar('SELECT count(*) FROM inversion.audit'),before)
        self.assertEqual(self.inv_scalar('SELECT count(*) FROM inversion.reviews WHERE subject_id=%s',(draft.subject_id,)),1)

    def test_half_open_assignment_and_parameter_validity(self):
        assembly,_=self.create_assembly()
        a=str(assembly.subject_id)
        first=self.parameters.assign(dict(assembly_id=a,well_id='test:A',valid_from=START,valid_to=MID,close_assignment_id=None),command())
        self.parameters.assign(dict(assembly_id=a,well_id='test:B',valid_from=MID,valid_to=END,close_assignment_id=None),command())
        with self.assertRaises(InversionError):
            self.parameters.assign(dict(assembly_id=a,well_id='test:C',valid_from=START,valid_to=END,close_assignment_id=None),command())
        from backend.inversion.parameters import contains_interval
        start=datetime(2026,1,1,tzinfo=timezone.utc); end=datetime(2026,7,1,tzinfo=timezone.utc)
        self.assertTrue(contains_interval(start,end,start,end))
        self.assertFalse(contains_interval(start,end,end,end+timedelta(seconds=1)))
        self.assertEqual(self.inv_scalar('SELECT well_id FROM inversion.assignments WHERE id=%s',(first.subject_id,)),'test:A')

    def test_creator_submitter_not_reviewer(self):
        assembly,_=self.create_assembly(); body=self.body(assembly.subject_id)
        draft=self.parameters.create_version(body,command())
        submit=command(version=1)
        self.parameters.submit(draft.subject_id,submit)
        self.assertTrue(self.parameters.submit(draft.subject_id,submit).replayed)
        renamed=replace(AUTHOR,username='Renamed',role_code='administrator')
        with self.assertRaises(InversionError) as caught:
            self.parameters.review(draft.subject_id,'approve',command(renamed,2))
        self.assertEqual(caught.exception.code,'inversion.self_review')
        approve=command(REVIEWER,2)
        self.parameters.review(draft.subject_id,'approve',approve)
        self.assertTrue(self.parameters.review(draft.subject_id,'approve',approve).replayed)
        self.assertEqual(self.parameters.get(draft.subject_id).review_state,'approved')
        self.assertEqual(self.inv_scalar('SELECT count(*) FROM inversion.reviews WHERE subject_id=%s',(draft.subject_id,)),2)
        self.allowed=False
        with self.assertRaises(InversionError) as caught: self.parameters.review(draft.subject_id,'approve',approve)
        self.assertEqual(caught.exception.status,403)

    def test_synthetic_cannot_bind_production(self):
        assembly,_=self.create_assembly()
        with self.assertRaises(InversionError) as caught:
            self.parameters.assign(dict(assembly_id=str(assembly.subject_id),well_id=str(uuid4()),valid_from=START,valid_to=None,close_assignment_id=None),command())
        self.assertEqual(caught.exception.code,'inversion.source_ineligible')

    def test_parameter_attachment_and_loss_provenance_required(self):
        assembly,_=self.create_assembly(); body=self.body(assembly.subject_id)
        body['drive']['loss_grid']['evidence_refs'][0]['purpose']='geometry'
        draft=self.parameters.create_version(body,command())
        with self.assertRaises(InversionError): self.parameters.submit(draft.subject_id,command(version=1))
        self.assertEqual(self.parameters.get(draft.subject_id).review_state,'draft')
        self.assertEqual(self.inv_scalar('SELECT count(*) FROM inversion.reviews'),0)

    def test_missing_coverage_and_stale_evidence_prevent_submission_or_approval(self):
        assembly,_=self.create_assembly(); body=self.body(assembly.subject_id)
        del body['provenance']['coverage']['max_time_alignment_error_ns']
        draft=self.parameters.create_version(body,command())
        with self.assertRaises(ValueError): self.parameters.submit(draft.subject_id,command(version=1))
        good=self.body(assembly.subject_id,parent=1)
        created=self.parameters.create_version(good,command(version=1))
        self.parameters.submit(created.subject_id,command(version=1))
        pin=good['provenance']['channel_bindings'][0]['channel_pin']['calibration_pin']
        from uuid import UUID
        self.evidence.revoke(UUID(pin['id']),self.wave_command('revoke',UUID(pin['id']),version=2))
        with self.assertRaises(InversionError): self.parameters.review(created.subject_id,'approve',command(REVIEWER,2))

    def test_later_install_does_not_rewrite_history(self):
        assembly,original=self.create_assembly()
        old=self.approved_parameter(assembly.subject_id)
        new=original | dict(parent_version=1,valid_from=MID,asset_number='TEST-002')
        self.parameters.create_assembly(new,command(version=1))
        after=self.parameters.get(old.id)
        self.assertEqual(after.content_sha256,old.content_sha256)
        self.assertEqual(after.valid_to,old.valid_to)
        self.assertEqual(self.inv_scalar('SELECT body->>\'valid_to\' FROM inversion.assembly_versions WHERE assembly_id=%s AND version=1',(old.assembly_id,)),None)
        self.assertEqual(self.inv_scalar('SELECT count(*) FROM inversion.mechanical_events'),1)
        with self.assertRaises(InversionError): self.parameters.check_current(old.id)
        from backend.inversion.parameters import _ns
        from fractions import Fraction
        boundary=_ns(datetime(2026,7,1,tzinfo=timezone.utc))
        self.assertEqual(self.parameters.check_current(old.id,start=boundary-1,end=boundary-Fraction(1,10)).id,old.id)
        with self.assertRaises(InversionError): self.parameters.check_current(old.id,start=boundary,end=boundary)
        with self.assertRaises(InversionError): self.parameters.check_current(old.id,start=boundary-2,end=boundary-1,uncertainty_ns=Fraction(1,10))

    def test_parameter_lineage_and_mechanical_lineage_are_independent(self):
        assembly,original=self.create_assembly()
        first=self.parameters.create_version(self.body(assembly.subject_id),command())
        second=self.parameters.create_version(self.body(assembly.subject_id,parent=1),command(version=1))
        self.assertEqual(second.version,2)
        with self.assertRaises(InversionError):
            self.parameters.create_version(self.body(assembly.subject_id,parent=1),command(version=1))
        other,original=self.create_assembly()
        self.parameters.create_assembly(original | dict(parent_version=1,valid_from=MID),command(version=1))
        body=self.body(other.subject_id,mechanical=2); body['valid_from']=MID
        first_under_two=self.parameters.create_version(body,command())
        self.assertEqual(first_under_two.version,1)

    def test_open_assignment_close_is_append_only_and_atomic(self):
        assembly,_=self.create_assembly(); a=str(assembly.subject_id)
        old=self.parameters.assign(dict(assembly_id=a,well_id='test:A',valid_from=START,valid_to=None,close_assignment_id=None),command())
        self.parameters.assign(dict(assembly_id=a,well_id='test:B',valid_from=MID,valid_to=None,close_assignment_id=str(old.subject_id)),command(version=1))
        self.assertIsNone(self.inv_scalar('SELECT valid_to FROM inversion.assignments WHERE id=%s',(old.subject_id,)))
        self.assertEqual(self.inv_scalar('SELECT version FROM inversion.assignment_state WHERE assignment_id=%s',(old.subject_id,)),2)

    def test_metadata_only_privileges_and_probe_drift(self):
        import psycopg
        from backend.inversion.metadata import probe_metadata
        for role in ('reader','operator'):
            with self.inversion_fixture.connect(role) as conn:
                with self.assertRaises(psycopg.errors.InsufficientPrivilege): conn.execute('SELECT payload FROM waveforms.attachments')
        with self.connect('migration_owner') as conn:
            from psycopg import sql
            conn.execute(sql.SQL('GRANT SELECT(payload) ON waveforms.attachments TO {}').format(sql.Identifier(self.inversion_fixture.sidecar['roles']['operator'])))
        try:
            with self.assertRaises(InversionError): probe_metadata(self.inversion_fixture.connect)
        finally:
            with self.connect('migration_owner') as conn:
                conn.execute(sql.SQL('REVOKE SELECT(payload) ON waveforms.attachments FROM {}').format(sql.Identifier(self.inversion_fixture.sidecar['roles']['operator'])))


class ParameterFoundationTests(unittest.TestCase):
    def test_parameter_service_is_available(self):
        self.assertIsNotNone(importlib.util.find_spec('backend.inversion.parameters'))


if __name__ == '__main__': unittest.main()
