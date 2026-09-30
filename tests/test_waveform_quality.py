"""Synthetic program branches do not create field-approved measurements."""
import importlib
import unittest


class QualityTests(unittest.TestCase):
    def test_unavailable_channel_does_not_soften_known_hard_failures(self):
        from dataclasses import replace
        from tests.waveform_pairing_support import pure_case,evidence,profile_body
        for kind,reason in (('rms','unsupported_quantity'),('quantity','unsupported_quantity'),
                            ('role','reference_role_mismatch'),('clipped','bad_or_clipped_samples')):
            context,_=pure_case(signal='rms' if kind=='rms' else 'instantaneous',
                                bad_index=1 if kind=='clipped' else None,quality=2)
            body=profile_body(); missing='power' if kind=='clipped' else 'load'
            streams=tuple(d for d in context.streams if d.role!=missing)
            roles=tuple(d.role for d in context.streams)
            if kind=='rms': body['required_quantities'][0]['signal_kind']='rms'
            if kind=='quantity': body['required_quantities'][0]['quantity']='current_a'
            if kind=='role':
                streams=tuple(replace(d,role='position') if d.role=='displacement' else d for d in streams)
                roles=tuple('position' if r=='displacement' else r for r in roles)
                for section in ('required_quantities','min_rates','ranges'):
                    for entry in body[section]:
                        if entry['role']=='displacement': entry['role']='position'
            context=replace(context,streams=streams,completion={context.key:None},
                cycle=context.cycle|{'selected_roles':roles,'unresolved_roles':(missing,)})
            result=self.q.evaluate(context,profile=self.q.ProfileVersion(evidence('profile',body)))
            with self.subTest(kind=kind):
                self.assertEqual(result.status,'ineligible')
                self.assertIn(reason,result.reasons)

    def test_unavailable_selected_channels_remain_explicit_pending_dependencies(self):
        from dataclasses import replace
        from tests.waveform_pairing_support import pure_case
        context,profile=pure_case()
        roles=tuple(d.role for d in context.streams)
        context=replace(context,streams=(),completion={context.key:None},sync_evidence=None,
            cycle=context.cycle|{'status':'pending','selected_roles':roles,'unresolved_roles':roles})
        result=self.q.evaluate(context,profile=profile)
        self.assertEqual(result.status,'pending')
        self.assertIn('selected_channel_unavailable',result.reasons)
        self.assertIn('incomplete',result.reasons)

    def test_three_phase_instantaneous_fixed_gate_and_documented_wiring(self):
        from tests.waveform_pairing_support import three_phase_case
        context,profile=three_phase_case()
        self.assertEqual(self.q.evaluate(context,profile=profile).status,'eligible')
        self.assertEqual(self.q.cycle_summary(context)['stroke'],'4')
        for change,reason in (({'signal':'rms'},'unsupported_quantity'),({'missing_phase':True},'unsupported_quantity'),
                              ({'wiring':False},'evidence_unavailable')):
            with self.subTest(change=change):
                context,profile=three_phase_case(**change)
                result=self.q.evaluate(context,profile=profile)
                self.assertNotEqual(result.status,'eligible')
                self.assertIn(reason,result.reasons)

    def test_fixed_displacement_role_cannot_follow_profile_alias(self):
        from dataclasses import replace
        from tests.waveform_pairing_support import pure_case,evidence,profile_body
        context,profile=pure_case()
        self.assertEqual(self.q.evaluate(context,profile=profile).status,'eligible')
        for wrong_quantity in (False,True):
            body=profile_body()
            renames={'displacement':'position'} | ({'load':'displacement'} if wrong_quantity else {})
            for section in ('required_quantities','min_rates','ranges'):
                for entry in body[section]: entry['role']=renames.get(entry['role'],entry['role'])
            changed=replace(context,streams=tuple(replace(d,role=renames.get(d.role,d.role)) for d in context.streams))
            result=self.q.evaluate(changed,profile=self.q.ProfileVersion(evidence('profile',body)))
            with self.subTest(wrong_quantity=wrong_quantity):
                self.assertEqual(result.status,'ineligible')
                self.assertIn('reference_role_mismatch',result.reasons)
            archive=replace(changed,cycle=changed.cycle|{'purpose':'raw_archive'})
            self.assertEqual(self.q.evaluate(archive,profile=self.q.ProfileVersion(evidence('profile',profile_body('raw_archive')))).status,'eligible')

    def test_profile_cannot_authorize_rms_as_cycle_power(self):
        from tests.waveform_pairing_support import pure_case,evidence,profile_body
        context,_=pure_case(signal='rms')
        body=profile_body(); body['required_quantities'][0]['signal_kind']='rms'
        profile=self.q.ProfileVersion(evidence('profile',body))
        result=self.q.evaluate(context,profile=profile)
        self.assertEqual(result.status,'ineligible')
        self.assertIn('unsupported_quantity',result.reasons)

    def test_calibration_source_kind_cannot_relabel_synthetic_channel(self):
        from dataclasses import replace
        from tests.waveform_pairing_support import pure_case,evidence
        from backend.waveforms.evidence_contracts import thaw
        context,profile=pure_case(); key=next(iter(context.calibrations)); old=context.calibrations[key]
        body=thaw(old.body); body['source_kind']='field_measurement'
        context=replace(context,calibrations=dict(context.calibrations)|{key:evidence('calibration',body,old.id)})
        self.assertNotEqual(self.q.evaluate(context,profile=profile).status,'eligible')

    def test_cycle_metrics_and_flat_reference_refusal(self):
        from dataclasses import replace
        from fractions import Fraction
        import struct
        from tests.waveform_pairing_support import pure_case
        context,profile=pure_case()
        self.assertTrue(hasattr(self.q,'cycle_summary'),'Task11 exact cycle metrics absent')
        summary=self.q.cycle_summary(context)
        self.assertEqual((summary['stroke'],summary['cycles_per_minute'],summary['valid_sample_count']),('4','30',3))
        data=context.streams[-1]; raw=bytearray(data.data)
        for index in range(3): struct.pack_into('<i',raw,index*15+10,0)
        changed=replace(data,data=bytes(raw))
        context=replace(context,streams=(*context.streams[:-1],changed))
        self.assertIn('cycle_unresolved',self.q.evaluate(context,profile=profile).reasons)

    def test_profile_cannot_change_requested_purpose(self):
        from dataclasses import replace
        from tests.waveform_pairing_support import pure_case,evidence,profile_body
        context,_=pure_case()
        archive=self.q.ProfileVersion(evidence('profile',profile_body('raw_archive')))
        result=self.q.evaluate(context,profile=archive)
        self.assertEqual(result.purpose,'software_pair_test')
        self.assertEqual(result.status,'ineligible')
        self.assertIn('unsupported_quantity',result.reasons)

    def setUp(self):
        try: self.q=importlib.import_module('backend.waveforms.quality')
        except ModuleNotFoundError: self.fail('Task11 quality implementation is absent')

    def test_missing_blocks_field_pair(self):
        from dataclasses import replace
        from tests.waveform_pairing_support import pure_case,evidence,profile_body
        context,profile=pure_case(bad_index=1,quality=1)
        context=replace(context,cycle=context.cycle|{'purpose':'field_pair_input'})
        profile=self.q.ProfileVersion(evidence('profile',profile_body('field_pair_input')))
        result=self.q.evaluate(context,profile=profile)
        self.assertIn('missing_samples',result.reasons)
        self.assertEqual(result.status,'ineligible')

    def test_known_expiry_is_hard_failure(self):
        from dataclasses import replace
        from tests.waveform_pairing_support import pure_case,evidence
        from backend.waveforms.evidence_contracts import thaw
        from backend.waveforms.quality import channel_identity
        context,profile=pure_case(); data=context.streams[0]; key=channel_identity(data)
        cal=context.calibrations[key]; body=thaw(cal.body); body['valid_to']='1970-01-01T00:00:00.000001Z'
        cals=dict(context.calibrations); cals[key]=evidence('calibration',body,cal.id)
        context=replace(context,calibrations=cals,cycle=context.cycle|{'absolute_support':{key:(1000,2000)}})
        result=self.q.evaluate(context,profile=profile)
        self.assertIn('calibration_expired_or_revoked',result.reasons)
        self.assertEqual(result.status,'ineligible')

    def test_raw_archive_retains_visible_issues_without_pair_eligibility(self):
        from dataclasses import replace
        from tests.waveform_pairing_support import pure_case,evidence,profile_body
        context,_=pure_case(bad_index=1,quality=1)
        context=replace(context,cycle=context.cycle|{'purpose':'raw_archive'},restrictions=('late conflict',),registry_snapshot=None)
        profile=self.q.ProfileVersion(evidence('profile',profile_body('raw_archive')))
        result=self.q.evaluate(context,profile=profile)
        self.assertEqual(result.status,'eligible')
        self.assertEqual(result.purpose,'raw_archive')
        self.assertTrue({'missing_samples','capture_conflict','evidence_unavailable'}<=set(result.reasons))

    def test_rms_not_instantaneous(self):
        from tests.waveform_pairing_support import pure_case
        context,profile=pure_case(signal='rms')
        self.assertIn('unsupported_quantity',self.q.evaluate(context,profile=profile).reasons)

    def test_same_cycle_label_not_sufficient(self):
        from dataclasses import replace
        from tests.waveform_pairing_support import pure_case
        context,profile=pure_case()
        context=replace(context,sync_evidence=None)
        self.assertIn('unknown_clock',self.q.evaluate(context,profile=profile).reasons)

    def test_all_samples_beyond_preview_limit_count(self):
        from tests.waveform_pairing_support import pure_case
        context,profile=pure_case(count=20003)
        self.assertEqual(self.q.evaluate(context,profile=profile).status,'eligible')
        context,profile=pure_case(count=20003,bad_index=20001,quality=4)
        self.assertIn('bad_or_clipped_samples',self.q.evaluate(context,profile=profile).reasons)
