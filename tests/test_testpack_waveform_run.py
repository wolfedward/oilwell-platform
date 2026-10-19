from copy import deepcopy
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
import unittest

from tools.testpack.waveform_run import channel_bodies, bind_channels, inversion_gate, save_bound_file, guard_selection, waveform_outcome
from tools.testpack.evidence_report import evidence_html,render_evidence
from tools.testpack.contracts import Channel
from backend.waveforms.evidence_contracts import validate_body


class TestPackWaveformRun(unittest.TestCase):
    def test_sampling_calibration_before_bound_manifest(self):
        channel=Channel('active_power','W','0.01','0',(1,2),(0,0))
        events=[]
        class Client:
            def evidence(self,kind,body):
                events.append(kind)
                validate_body(kind,body)
                if kind=='calibration':
                    self_test.assertEqual(body['sampling_config_version_id'],'11111111-1111-4111-8111-111111111111')
                return {'id':'11111111-1111-4111-8111-111111111111','content_sha256':'a'*64,'version':2}
        self_test=self
        binding=bind_channels(Client(),(channel,), '2026-09-30T16:00:00.000000Z')
        self.assertEqual(events,['sampling_config','calibration'])
        self.assertEqual(binding.calibration['active_power']['review_version'],2)
        from tools.testpack.waveforms import _encode
        from tools.testpack.contracts import PackConfig
        raw=_encode('tiny-contract',PackConfig(),(channel,),128,7812500,binding=binding)
        manifest=__import__('json').loads(raw.splitlines()[0])
        self.assertEqual(manifest['streams'][0]['channels'][0]['config_id'],binding.reported_configs['active_power'])

    def test_original_pack_never_rewritten(self):
        with TemporaryDirectory() as directory:
            root=Path(directory);original=root/'original.owpwave';original.write_bytes(b'original')
            run=root/'run';run.mkdir()
            saved=save_bound_file(run,'cycle-base',b'bound')
            self.assertEqual(original.read_bytes(),b'original')
            self.assertNotEqual(saved['sha256'],__import__('hashlib').sha256(original.read_bytes()).hexdigest())
            with self.assertRaises(FileExistsError):save_bound_file(run,'cycle-base',b'changed')
            for case_id in ('../outside','C:/outside','..'):
                with self.assertRaises(ValueError):save_bound_file(run,case_id,b'x')

    def test_failed_budget_never_calls_approval_or_solver(self):
        with patch('tools.testpack.waveform_run.software_budget',return_value={'passed':False,'reasons':['budget_failed']}):
            result=inversion_gate(object())
        self.assertEqual(result['status'],'blocked')
        self.assertEqual(result['parameter_submitted'],False)
        self.assertEqual(result['run_id'],None)

    def test_source_and_calibration_cannot_be_promoted(self):
        original={'purpose':'software_pair_test','sync':{'id':'sync','version':'2'},'profile':{'id':'profile'}}
        before=deepcopy(original)
        field=guard_selection(original,profile={'id':'field'},purpose='field_pair_input')
        self.assertEqual(original,before)
        self.assertEqual(field['purpose'],'field_pair_input')
        self.assertEqual(field['sync'],original['sync'])

    def test_withdrawn_evidence_and_version_rejected(self):
        original={'sync':{'id':'sync','version':'2','content_sha256':'a'*64}}
        changed=guard_selection(original,sync_version=3)
        self.assertEqual(original['sync']['version'],'2')
        self.assertEqual(changed['sync']['version'],'3')

    def test_expected_rejection_requires_exact_observed_quality_reason(self):
        self.assertEqual(waveform_outcome('cycle-clipped',{'status':'ineligible','reasons':['unregistered','bad_or_clipped_samples']}),'passed')
        self.assertEqual(waveform_outcome('cycle-clipped',{'status':'ineligible','reasons':['config_mismatch']}),'failed')
        self.assertEqual(waveform_outcome('cycle-base',{'status':'ineligible','reasons':['config_mismatch']}),'failed')
        self.assertEqual(waveform_outcome('cycle-missing',{'status':'eligible','reasons':[]}),'failed')

    def test_evidence_page_escapes_input_and_keeps_source(self):
        page=evidence_html({'case_id':'<script>alert(1)</script>','source_kind':'synthetic','speed_budget':'failed'})
        self.assertNotIn('<script>alert(1)</script>',page)
        self.assertIn('&lt;script&gt;',page)
        self.assertIn('软件模型',page)
        self.assertIn('非现场测量',page)

    def test_actual_report_render_is_a_real_png(self):
        with TemporaryDirectory() as directory:
            target=Path(directory)/'evidence.png'
            render_evidence({'source_kind':'synthetic','budget':'failed'},target)
            self.assertTrue(target.read_bytes().startswith(b'\x89PNG\r\n\x1a\n'))
            self.assertGreater(target.stat().st_size,10000)
            self.assertTrue(target.with_suffix('.hashes.json').is_file())


if __name__=='__main__':unittest.main()
