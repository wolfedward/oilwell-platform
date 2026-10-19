from dataclasses import replace
from hashlib import sha256
from io import BytesIO
import json
import math
import unittest
from uuid import NAMESPACE_URL, uuid5

from backend.waveforms.files import scan_file
from backend.waveforms.errors import ContractError
from tools.testpack.contracts import PackConfig
from tools.testpack.cycles import generate_cycles
from tools.testpack.currents import generate_currents
from tools.testpack.waveforms import encode_cycle, waveform_negative_cases, decode_channels
from tools.testpack.storage import generator_fingerprint, pack_identity


class TestPackWaveforms(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.cycles = generate_cycles(PackConfig())
        cls.case = encode_cycle(cls.cycles[0], PackConfig())
        cls.current = generate_currents(PackConfig())

    def wave(self, case):
        return next(a.content for a in case.artifacts if a.path.endswith('.owpwave'))

    def test_four_channels_20500_bytes_five_chunks(self):
        raw = self.wave(self.case)
        result = scan_file(BytesIO(raw))
        manifest = json.loads(raw.splitlines()[0])
        self.assertEqual(manifest['streams'][0]['decoded_bytes'],20500)
        self.assertEqual(manifest['streams'][0]['chunk_count'],5)
        self.assertEqual(len(result.lines),6)
        self.assertEqual([json.loads(line)['sample_count'] for line in raw.splitlines()[1:]], [256,256,256,256,1])
        self.assertEqual(result.manifest_sha256,sha256(raw.splitlines()[0]).hexdigest())
        pack_id=pack_identity(PackConfig(),generator_fingerprint())
        self.assertEqual(manifest['capture_id'],str(uuid5(NAMESPACE_URL,pack_id+'/cycle-base/capture')))

    def test_raw_archive_nullable_evidence(self):
        manifest=scan_file(BytesIO(self.wave(self.case))).manifest.body
        self.assertEqual(manifest['source_kind'],'synthetic')
        self.assertIsNone(manifest['source_evidence'])
        self.assertIsNone(manifest['sync_evidence_id'])
        self.assertTrue(all(c['config_id'] is None and c['calibration_id'] is None and c['measurement_source']=='synthetic'
                            for c in manifest['streams'][0]['channels']))
        self.assertFalse(self.case.facts['pairing_evidence_bound'])

    def test_motor_count_and_frequency_not_resampled(self):
        self.assertEqual(len(self.current),4)
        for case in self.current:
            frame=scan_file(BytesIO(self.wave(case))).manifest
            self.assertEqual(frame.streams[0].frame_count,16384)
            self.assertEqual(frame.streams[0].rate_num,1024)
            self.assertEqual(frame.body['window_ns'],'16000000000')
        data=decode_channels(self.current[0])['current_a']['values']
        fundamental=2/len(data)*sum(v*math.sin(math.tau*50*i/1024) for i,v in enumerate(data))
        self.assertAlmostEqual(fundamental,10,delta=.0001)
        harmonic=decode_channels(self.current[1])['current_a']['values']
        a150=2/len(harmonic)*sum(v*math.sin(math.tau*150*i/1024) for i,v in enumerate(harmonic))
        self.assertAlmostEqual(a150,.5,delta=.0001)

    def test_quality_gap_remains_missing_and_nonzero_invalid_is_retained(self):
        missing=decode_channels(encode_cycle(self.cycles[5],PackConfig()))['active_power']
        self.assertEqual(missing['values'][400:420],[None]*20)
        clipped=decode_channels(encode_cycle(self.cycles[6],PackConfig()))['active_power']
        self.assertTrue(any(v is not None and abs(v)==20000 for v,q in zip(clipped['values'],clipped['quality']) if q==2))

    def test_expected_protocol_failures(self):
        negatives=waveform_negative_cases(self.case)
        self.assertEqual(len(negatives),4)
        self.assertEqual([c.expected[0]['expected'] for c in negatives],
                         ['layout_invalid','digest_mismatch','layout_invalid','json_invalid'])
        for case in negatives:
            with self.subTest(case=case.case_id), self.assertRaises(ContractError) as raised:
                scan_file(BytesIO(self.wave(case)))
            self.assertEqual(raised.exception.code,case.expected[0]['expected'])


if __name__=='__main__':
    unittest.main()
