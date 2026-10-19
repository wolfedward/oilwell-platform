"""Explicit owned-lab integration only. A skipped run is not delivery evidence."""
import os
from pathlib import Path
from datetime import datetime, timezone
import unittest
from uuid import uuid4


class TestPackLive(unittest.TestCase):
    def test_actual_telemetry_pipeline(self):
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
