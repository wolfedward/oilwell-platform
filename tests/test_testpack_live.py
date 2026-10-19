"""Explicit owned-lab integration only. A skipped run is not delivery evidence."""
import os
from pathlib import Path
from datetime import datetime, timezone
import unittest
from uuid import uuid4


class TestPackLive(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.delivery_checks=None
        target=os.environ.get('OILWELL_TESTPACK_LIVE_DELIVERY')
        if target:
            from tools.testpack.lab import load_lab
            from tools.testpack.storage import load_pack
            from tools.testpack.delivery import run_delivery
            lab=load_lab(Path(os.environ['OILWELL_TESTPACK_LIVE_ROOT']))
            pack=load_pack(Path(os.environ['OILWELL_TESTPACK_LIVE_PACK']))
            cls.delivery_checks,cls.public_report=run_delivery(lab,pack,Path(target))

    def test_actual_browser_and_report(self):
        if self.delivery_checks is None:self.skipTest('full owned delivery not requested; not delivery evidence')
        checks={c.check_id:c for c in self.delivery_checks}
        self.assertEqual(checks['viewer-interaction'].status,'passed',checks['viewer-interaction'].actual)
        self.assertEqual(checks['inversion-comparison'].status,'blocked')
        self.assertTrue(self.public_report.is_file())

    def test_actual_study_workflow(self):
        if self.delivery_checks is not None:
            from tools.testpack.study_run import STEPS
            checks={c.check_id:c for c in self.delivery_checks}
            self.assertTrue(all(checks['study-'+s].status=='passed' for s in STEPS))
            return
        root=os.environ.get('OILWELL_TESTPACK_LIVE_ROOT');pack=os.environ.get('OILWELL_TESTPACK_LIVE_PACK')
        if not root or not pack:self.skipTest('explicit owned lab required; not delivery evidence')
        from tools.testpack.lab import load_lab
        from tools.testpack.storage import load_pack
        from tools.testpack.study_run import run_studies
        lab=load_lab(Path(root))
        run=lab.root/('run-studies-'+datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S')+'-'+uuid4().hex[:8])
        checks=run_studies(lab,load_pack(Path(pack)),run)
        self.assertTrue(all(c.status=='passed' for c in checks),(str(run),[(c.check_id,c.status,c.actual) for c in checks if c.status!='passed']))

    def test_actual_waveform_workflow(self):
        if self.delivery_checks is not None:
            checks={c.check_id:c for c in self.delivery_checks}
            self.assertEqual(checks['waveform-evidence-http'].status,'passed')
            self.assertEqual(checks['original_pack_unchanged'].status,'passed')
            return
        root=os.environ.get('OILWELL_TESTPACK_LIVE_ROOT');pack=os.environ.get('OILWELL_TESTPACK_LIVE_PACK')
        if not root or not pack:self.skipTest('explicit owned lab required; not delivery evidence')
        from tools.testpack.lab import load_lab
        from tools.testpack.storage import load_pack
        from tools.testpack.waveform_run import run_waveforms
        lab=load_lab(Path(root))
        run=lab.root/('run-waves-'+datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S')+'-'+uuid4().hex[:8])
        checks=run_waveforms(lab,load_pack(Path(pack)),run)
        failures=[(c.check_id,c.actual.get('stage'),c.actual.get('error_code'),c.actual.get('error_type')) for c in checks if c.status=='failed']
        self.assertFalse(failures,(str(run),failures))
        for check in checks:
            if check.check_id in {c+'-waveform' for c in ('cycle-base','cycle-balance','cycle-friction','cycle-speed')}:
                self.assertEqual(check.status,'passed',(check.check_id,check.actual.get('pair')))
                self.assertEqual(check.actual['inversion']['status'],'blocked')
                self.assertFalse(check.actual['inversion']['parameter_submitted'])
        from tools.testpack.waveform_run import run_waveform_guards
        guard=run_waveform_guards(lab,load_pack(Path(pack)),lab.root/(run.name+'-guards'),run)
        self.assertEqual(guard.status,'passed',guard.actual)

    def test_actual_telemetry_pipeline(self):
        if self.delivery_checks is not None:
            from tools.testpack.telemetry_run import STEPS
            checks={c.check_id:c for c in self.delivery_checks}
            self.assertTrue(all(checks['telemetry-'+s].status=='passed' for s in STEPS))
            return
        root=os.environ.get('OILWELL_TESTPACK_LIVE_ROOT')
        pack=os.environ.get('OILWELL_TESTPACK_LIVE_PACK')
        if not root or not pack:
            self.skipTest('explicit owned lab and pack paths required; not a delivery pass')
        from tools.testpack.lab import load_lab
        from tools.testpack.storage import load_pack
        from tools.testpack.telemetry_run import run_telemetry
        lab=load_lab(Path(root))
        run=lab.root/('run-telemetry-'+datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S')+'-'+uuid4().hex[:8])
        checks=run_telemetry(lab,load_pack(Path(pack)),run)
        self.assertTrue(checks)
        self.assertTrue(all(c.status=='passed' for c in checks),[(c.check_id,c.status,c.actual) for c in checks if c.status!='passed'])


if __name__=='__main__':unittest.main()
