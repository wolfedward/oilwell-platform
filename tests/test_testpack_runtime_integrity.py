from hashlib import sha256
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from tools.testpack.contracts import PackConfig
from tools.testpack.storage import write_pack,strict_json
from tests.test_testpack_contracts import example,GENERATOR


class RuntimeIntegrity(unittest.TestCase):
    def test_runner_pack_must_match_owned_lab_before_writes(self):
        from tools.testpack.lab import checked_pack
        with TemporaryDirectory() as folder:
            pack=write_pack(Path(folder)/'pack',PackConfig(),(example(),),GENERATOR)
            lab=SimpleNamespace(monitoring_root=Path(folder)/'monitor')
            good={'pack_id':pack.manifest['pack_id'],'pack_sha256':sha256((pack.root/'manifest.json').read_bytes()).hexdigest()}
            with patch('tools.testpack.lab.checked_handle'),patch('tools.testpack.lab.deployment_fixture',return_value=good):
                self.assertEqual(checked_pack(lab,pack),pack)
            with patch('tools.testpack.lab.checked_handle'),patch('tools.testpack.lab.deployment_fixture',return_value=good|{'pack_sha256':'f'*64}):
                with self.assertRaises(ValueError):checked_pack(lab,pack)

    def test_current_amplitude_metadata_matches_each_phase(self):
        from tools.testpack.currents import generate_currents
        from tools.testpack.waveforms import decode_channels
        cases={c.case_id:c for c in generate_currents(PackConfig(profile='extended'))}
        for name,peaks in [('amplitude',[12,12,12]),('unbalance',[10,8,10])]:
            case=cases['current-'+name]
            meta=strict_json(next(a.content for a in case.artifacts if a.path.endswith('.json')))
            model=meta['diagnostics']
            self.assertEqual(model['fundamental_peak_by_phase_A'],peaks)
            self.assertEqual(model['fundamental_peak_A'],peaks[0] if len(set(peaks))==1 else None)
