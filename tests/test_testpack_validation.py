from dataclasses import replace
from hashlib import sha256
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from tools.testpack.build import build
from tools.testpack.contracts import PackConfig
from tools.testpack.storage import canonical_bytes,load_pack
from tools.testpack.validate import validate_pack,offline_passed


class TestPackValidation(unittest.TestCase):
    def setUp(self):
        self.tmp=TemporaryDirectory(prefix='软件包 校验 ')
        self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name)

    def test_offline_layers_do_not_claim_network_or_inversion(self):
        pack=build(PackConfig(),self.root/'pack')
        checks=validate_pack(pack)
        self.assertTrue(offline_passed(checks),[c for c in checks if c.status=='failed'])
        self.assertTrue(all(c.status=='passed' for c in checks if c.layer=='offline'))
        self.assertTrue(all(c.status!='passed' for c in checks if c.layer in ('software_network','http','browser')))
        self.assertEqual(next(c for c in checks if c.check_id=='inversion-comparison').status,'not_run')

    def test_manifest_tamper_refused_before_parsing(self):
        pack=build(PackConfig(),self.root/'pack')
        (pack.root/pack.manifest['artifacts'][0]['path']).write_bytes(b'changed')
        with self.assertRaises(ValueError):
            validate_pack(pack)

    def test_negative_case_success_requires_expected_code(self):
        pack=build(PackConfig(),self.root/'pack')
        manifest=pack.manifest
        case=next(c for c in manifest['cases'] if c['case_id']=='protocol-bad-digest')
        case['expected'][0]['expected']='json_invalid'
        raw=canonical_bytes(manifest)
        (pack.root/'manifest.json').write_bytes(raw)
        (pack.root/'manifest.sha256').write_bytes((sha256(raw).hexdigest()+'\n').encode())
        checks=validate_pack(load_pack(pack.root))
        self.assertEqual(next(c for c in checks if c.check_id=='protocol-bad-digest').status,'failed')
        self.assertFalse(offline_passed(checks))

    def test_extended_budget_and_case_inventory(self):
        # Keep the smoke gate explicit, not a historical status file.
        smoke=build(PackConfig(),self.root/'smoke')
        self.assertTrue(offline_passed(validate_pack(smoke)))
        pack=build(replace(PackConfig(),profile='extended'),self.root/'extended')
        counts=pack.manifest['cases']
        self.assertEqual(sum(c['facts'].get('data_kind')=='cycle' for c in counts),48)
        self.assertEqual(sum(c['category']=='shape_only' for c in counts),12)
        self.assertEqual(sum(c['facts'].get('data_kind')=='current' for c in counts),8)
        self.assertEqual(sum(c['facts'].get('daily_count',0) for c in counts),112)
        self.assertEqual(sum(c['facts'].get('frame_count',0) for c in counts if c['facts'].get('data_kind')=='history'),10752)
        self.assertLessEqual(sum(a['size_bytes'] for a in pack.manifest['artifacts']),268435456)
        self.assertTrue(offline_passed(validate_pack(pack)))


if __name__=='__main__':
    unittest.main()
