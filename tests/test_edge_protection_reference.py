import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from backend.gateway.protection_reference import EdgeProtectionReference, ReferenceStateError
from tests.test_protection_replay import settings, frame


class EdgeReferenceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / 'reference'

    def new(self, config=None):
        config = config or settings(delay=0)
        EdgeProtectionReference.initialize(self.root, config, commissioning_reference='software fixture')
        adapter = EdgeProtectionReference(self.root, config)
        self.addCleanup(adapter.close)
        return adapter

    def receive(self, adapter, evidence, **options):
        return adapter.receive(evidence, protocol_version='owp.dsp.protection/1',
                               settings_sha256=adapter.engine.settings_digest, **options)

    def test_rejected_malformed_frame_invalidates_ready_evidence_and_nonmature_pickup(self):
        adapter = self.new(settings(delay=1000))
        self.receive(adapter, frame(0, 1))
        adapter.confirm_local_startup('operator', 'checked')
        self.receive(adapter, frame(100, 11))
        with self.assertRaises(ReferenceStateError):
            self.receive(adapter, None)
        self.assertTrue(adapter.status()['shadow_inhibit'])
        self.assertEqual(adapter.status()['state']['rules']['overcurrent']['state'], 'normal')
        with self.assertRaises(ReferenceStateError):
            adapter.confirm_local_startup('operator', 'old frame')
        self.receive(adapter, frame(1100, 11))
        self.assertEqual(adapter.status()['state']['rules']['overcurrent']['state'], 'pickup')
        adapter.close()
        restarted = EdgeProtectionReference(self.root, settings(delay=1000))
        self.addCleanup(restarted.close)
        self.assertTrue(restarted.status()['shadow_inhibit'])

    def test_invalid_local_reset_attestation_invalidates_ready_evidence(self):
        adapter = self.new()
        self.receive(adapter, frame(0, 1))
        adapter.confirm_local_startup('operator', 'checked')
        with self.assertRaises(ReferenceStateError):
            self.receive(adapter, frame(100, 1), local_reset='yes')
        self.assertTrue(adapter.status()['shadow_inhibit'])
        with self.assertRaises(ReferenceStateError):
            adapter.confirm_local_startup('operator', 'old frame')

    def test_restart_keeps_confirmation_provenance_but_requires_new_confirmation(self):
        adapter = self.new()
        self.receive(adapter, frame(0, 1))
        adapter.confirm_local_startup('operator', 'physical panel check')
        adapter.close()
        restarted = EdgeProtectionReference(self.root, settings(delay=0))
        self.addCleanup(restarted.close)
        self.assertTrue(restarted.status()['shadow_inhibit'])
        self.assertEqual(restarted.local_confirmation,
                         {'actor': 'operator', 'reason': 'physical panel check'})
        self.receive(restarted, frame(100, 1))
        self.assertTrue(restarted.status()['shadow_inhibit'])
        restarted.close()
        checkpoint = json.loads((self.root / 'checkpoint.json').read_text(encoding='utf-8'))
        self.assertEqual(checkpoint['body']['local_confirmation'],
                         {'actor': 'operator', 'reason': 'physical panel check'})

    def test_protocol_or_settings_mismatch_invalidates_prior_safe_evidence(self):
        adapter = self.new()
        self.receive(adapter, frame(0, 1))
        adapter.confirm_local_startup('operator', 'checked')
        for index, kwargs in enumerate(({'protocol_version': 'owp.dsp.protection/2',
                        'settings_sha256': adapter.engine.settings_digest},
                       {'protocol_version': 'owp.dsp.protection/1',
                        'settings_sha256': '0' * 64},
                       {'protocol_version': 'owp.dsp.protection/1'},
                       {'settings_sha256': adapter.engine.settings_digest},
                       {'protocol_version': True,
                        'settings_sha256': adapter.engine.settings_digest},
                       {})):
            with self.assertRaises(ReferenceStateError):
                adapter.receive(frame(index * 200 + 200, 1), **kwargs)
            self.assertTrue(adapter.status()['shadow_inhibit'])
            with self.assertRaises(ReferenceStateError):
                adapter.confirm_local_startup('operator', 'old frame')
            self.receive(adapter, frame(index * 200 + 300, 1))
            adapter.confirm_local_startup('operator', 'fresh check')

    def test_boot_inhibit_and_explicit_local_confirmation_never_enable_output(self):
        adapter = self.new()
        self.assertTrue(adapter.status()['shadow_inhibit'])
        with self.assertRaises(ReferenceStateError):
            adapter.confirm_local_startup('operator', 'checked')
        self.receive(adapter, frame(0, 1))
        adapter.confirm_local_startup('operator', 'checked')
        self.assertFalse(adapter.status()['shadow_inhibit'])
        self.assertFalse(adapter.status()['control_enabled'])
        self.assertFalse(adapter.status()['hardware_execution'])

    def test_latch_survives_restart_and_unsafe_reset(self):
        adapter = self.new()
        self.receive(adapter, frame(0, 11))
        adapter.close()
        restarted = EdgeProtectionReference(self.root, settings(delay=0))
        self.addCleanup(restarted.close)
        self.assertTrue(restarted.status()['shadow_inhibit'])
        output = self.receive(restarted, frame(100, 11, reset_request='local'), local_reset=True)
        self.assertEqual(output['reset_decision'], 'denied')
        self.assertEqual(restarted.engine.final_state['rules']['overcurrent']['state'], 'would_trip_latched')

    def test_missing_corrupt_and_wrong_settings_fail_closed_without_repair(self):
        with self.assertRaises(ReferenceStateError):
            EdgeProtectionReference(self.root, settings())
        self.assertFalse(self.root.exists())
        adapter = self.new()
        adapter.close()
        path = self.root / 'checkpoint.json'
        original = path.read_bytes()
        wrong = settings(delay=0)
        wrong['version'] = 'different'
        with self.assertRaises(ReferenceStateError):
            EdgeProtectionReference(self.root, wrong)
        self.assertEqual(path.read_bytes(), original)
        path.write_bytes(b'corrupt')
        with self.assertRaises(ReferenceStateError):
            EdgeProtectionReference(self.root, settings(delay=0))
        self.assertEqual(path.read_bytes(), b'corrupt')

    def test_mid_write_failure_marks_uncertain_and_cannot_be_silently_restarted(self):
        adapter = self.new()
        with patch.object(adapter, '_replace_checkpoint', side_effect=OSError('disk full')):
            with self.assertRaises(ReferenceStateError):
                self.receive(adapter, frame(0, 11))
        self.assertTrue(adapter.status()['shadow_inhibit'])
        self.assertTrue((self.root / 'pending.json').exists())
        adapter.close()
        with self.assertRaises(ReferenceStateError):
            EdgeProtectionReference(self.root, settings(delay=0))

    def test_network_reset_and_two_writers_are_refused(self):
        adapter = self.new()
        with self.assertRaises(ReferenceStateError):
            EdgeProtectionReference(self.root, settings(delay=0))
        before = (self.root / 'checkpoint.json').read_bytes()
        with self.assertRaises(ReferenceStateError):
            self.receive(adapter, frame(0, 0, reset_request='local'))
        self.assertNotEqual((self.root / 'checkpoint.json').read_bytes(), before)
        self.assertTrue(adapter.status()['shadow_inhibit'])

    def test_watchdog_replay_and_hardware_trip_have_priority(self):
        adapter = self.new()
        self.receive(adapter, frame(0, 1))
        adapter.confirm_local_startup('operator', 'checked')
        adapter.mark_unavailable('watchdog_timeout')
        self.assertTrue(adapter.status()['shadow_inhibit'])
        row = self.receive(adapter, frame(100, 0, quality='invalid', hardware_trip=True))
        self.assertTrue(row['hardware_inhibit'])
        row = self.receive(adapter, frame(50, 0, seq=0))
        self.assertEqual(row['qualification'], 'invalid')
        self.assertTrue(adapter.status()['shadow_inhibit'])

    def test_existing_root_not_overwritten_and_checkpoint_size_bounded(self):
        adapter = self.new()
        with self.assertRaises(ReferenceStateError):
            EdgeProtectionReference.initialize(self.root, settings(), commissioning_reference='again')
        for index in range(10):
            self.receive(adapter, frame(index * 100, 1))
        self.assertLess((self.root / 'checkpoint.json').stat().st_size, 32768)
        self.assertEqual(adapter.engine.events, [])

    def test_watchdog_invalidates_prior_safe_evidence_and_pickup_elapsed(self):
        adapter = self.new(settings(delay=1000))
        self.receive(adapter, frame(0, 1))
        adapter.mark_unavailable('watchdog_timeout')
        with self.assertRaises(ReferenceStateError):
            adapter.confirm_local_startup('operator', 'old data is not evidence')
        self.receive(adapter, frame(100, 11))
        self.receive(adapter, frame(900, 11))
        adapter.mark_unavailable('watchdog_timeout')
        result = self.receive(adapter, frame(1100, 11))
        self.assertEqual(result['rules']['overcurrent']['state'], 'pickup')


if __name__ == '__main__':
    unittest.main()
