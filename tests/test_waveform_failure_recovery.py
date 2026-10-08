"""Fast real process fault checks; the explicit verifier owns the long soak."""
from hashlib import sha256
import importlib.util
from tests.waveform_support import WaveformDatabaseCase
from tests.waveform_stack import Stack
from tests.test_waveform_repository import capture


class WaveformFailureRecoveryTests(WaveformDatabaseCase):
    def test_dual_lease_workers_enforce_and_retain_resource_guards(self):
        from pathlib import Path
        from uuid import uuid4
        from tests.waveform_resource_scenarios import measure_lease_workers
        from tools.verify_waveforms import source_inventory,inventory_sha256
        root=Path('logs/waveforms-wp04')/('r86-dual-'+uuid4().hex); root.mkdir(parents=True)
        print('R86_DUAL_RESOURCE_EVIDENCE',root.resolve(),flush=True)
        value=measure_lease_workers(self,root,run_id=uuid4().hex,source_sha256=inventory_sha256(source_inventory()))
        self.assertEqual(value['status'],'passed'); self.assertEqual(value['peak_connections'],4)
        self.assertEqual(value['completed'],2); self.assertTrue(value['sampler_joined'])
        self.assertTrue(value['owned_cleanup']['complete']); self.assertTrue(value['continuous_samples'])
        self.assertTrue(all(len(sample['processes'])==2 for sample in value['continuous_samples']))
        self.assertEqual(value['stopped']['connections'],0); self.assertEqual(value['stopped']['remaining_threads'],[])

    def test_actual_lease_evidence_preserves_stale_token_cost_and_two_workers(self):
        from pathlib import Path
        from uuid import uuid4
        from tests import waveform_resource_scenarios as resources
        from tools.verify_waveforms import source_inventory,inventory_sha256
        root=Path('.superpowers/sdd/2026-09-29-synchronized-waveform-paired-dataset/task-17-r86-focused')/uuid4().hex
        root.mkdir(parents=True); run_id=uuid4().hex
        print('R86_LEASE_EVIDENCE',root.resolve(),flush=True)
        result=resources.measure_leases(self,root,run_id=run_id,source_sha256=inventory_sha256(source_inventory()))
        self.assertEqual(result['status'],'passed'); self.assertEqual(result['run_id'],run_id)
        self.assertTrue(all(result['assertions'].values()))
        self.assertEqual({p['cardinality'] for p in result['plans']},{1,64,66})
        self.assertEqual(result['dual_workers']['peak_connections'],4)
        self.assertEqual(result['dual_workers']['completed'],2)
        self.assertTrue(result['cleanup']['complete'])
        self.assertTrue(all(c.closed for c in self.connections if c is not self.guard))

    def fault_case(self,fault,committed):
        self.assertIsNotNone(importlib.util.find_spec('tests.waveform_measurements'))
        with Stack(self) as stack:
            raw=capture()[0]
            stack.launch('receive',fault=fault)
            stack.await_event('receive','subscribed',gateway='GW')
            stack.publish('GW',raw)
            stack.await_event('receive','transaction_paused',phase=fault)
            self.assertEqual(self.scalar('SELECT count(*) FROM waveforms.facts',role='receiver'),int(committed))
            killed=stack.kill_owned('receive',reason=fault)
            self.assertNotEqual(killed['exit_code'],0)
            stack.launch('receive'); stack.await_event('receive','subscribed',gateway='GW')
            stack.reset_peer('GW'); stack.publish('GW',raw)
            stack.await_count('facts',1)
            self.assertEqual(self.scalar('SELECT raw_payload FROM waveforms.facts',role='receiver'),raw)
            self.assertEqual(self.scalar('SELECT payload_sha256 FROM waveforms.facts',role='receiver'),sha256(raw).hexdigest())
            stack.stop('receive'); self.assertEqual(stack.processes['receive'].returncode,0)

    def test_precommit_kill_rolls_back_then_replay_recovers(self): self.fault_case('pause_before_commit',False)
    def test_committed_result_loss_replay_preserves_one_fact(self): self.fault_case('pause_after_commit',True)

    def test_serial_fault_orchestrator_observes_owned_broker_restart_and_recovery(self):
        from tests import waveform_experiments as experiments
        self.assertTrue(hasattr(experiments,'fault_cycle'))
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as directory:
            result=experiments.fault_cycle(self,Path(directory),5)
            self.assertTrue(result['observed']); self.assertTrue(result['recovered'])
            self.assertEqual(result['kind'],'link_loss'); self.assertEqual(result['facts'],2)

    def test_representative_owned_assembly_sender_receipt_and_clock_fault_hooks(self):
        from tests.waveform_experiments import fault_cycle
        import tempfile
        from pathlib import Path
        for index in (2,4,6,9):
            with self.subTest(index=index),tempfile.TemporaryDirectory() as directory:
                result=fault_cycle(self,Path(directory),index)
                self.assertTrue(result['observed']); self.assertTrue(result['recovered'])
                self.assertFalse(result['cleanup']['residue'])

    def test_sender_measurement_phases_preserve_owned_kill_and_reconnect_faults(self):
        from tests.waveform_experiments import fault_cycle
        import tempfile
        from pathlib import Path
        for index in (4,11):
            with self.subTest(index=index),tempfile.TemporaryDirectory() as directory:
                result=fault_cycle(self,Path(directory),index)
                self.assertTrue(result['observed']); self.assertTrue(result['recovered'])
                self.assertFalse(result['cleanup']['residue'])
