"""Exact corrected physical windows against half-open registry histories."""
from datetime import datetime, timezone
from fractions import Fraction
import unittest
from uuid import uuid4
from backend.waveforms.contracts import CaptureKey
from backend.waveforms.errors import ContractError
from tests.waveform_registry_support import WaveformRegistryCase


class RegistryWindowTests(unittest.TestCase):
    def test_ns_at_binding_end_is_not_inside(self):
        from backend.waveforms.registry_context import contains_window
        self.assertFalse(contains_window(0, 1000, 1000, 1000))
        self.assertTrue(contains_window(0, 1000, 0, Fraction(1999, 2)))
        self.assertTrue(contains_window(0, 1000, Fraction(1999, 2), Fraction(1999, 2)))

    def test_unknown_utc_does_not_use_received_time(self):
        from backend.waveforms.registry_context import RegistryContext
        def forbidden(): self.fail('unknown UTC must not guess or connect')
        result = RegistryContext(forbidden).resolve((CaptureKey('G', 'D', uuid4()),),
            utc_start=None, start_ns=0, end_ns=1, uncertainty_ns=None, revision=None)
        self.assertEqual(result.status, 'pending')
        self.assertIsNone(result.utc_start)

    def test_window_expands_exactly_once_and_serializes_fraction(self):
        from backend.waveforms.registry_context import physical_window, rational_text
        self.assertEqual(physical_window(-100000000, 600000000, 50000000),
                         (Fraction(-150000000), Fraction(650000000)))
        self.assertEqual(rational_text(Fraction(1999,2)), '1999/2')
        self.assertEqual(rational_text(Fraction(-300000000)), '-300000000')
        for args in ((True, 1, 0), (0.0, 1, 0), (0, 1, -1), (2, 1, 0)):
            with self.assertRaises(ContractError): physical_window(*args)


class RegistryHistoryTests(WaveformRegistryCase):
    def test_all_sources_share_one_repeatable_read_revision_during_change(self):
        from datetime import timedelta
        from backend.waveforms.registry_context import RegistryContext
        first=self.history_source(); second=self.history_source(device='D2',well=first['well'])
        keys=(CaptureKey('GW','D',uuid4()),CaptureKey('GW','D2',uuid4()))
        anchor=datetime.now(timezone.utc)+timedelta(seconds=1)
        opened=[]; changed=[]; case=self
        class Cursor:
            def __init__(self,inner): self.inner=inner
            def __getattr__(self,key): return getattr(self.inner,key)
            def execute(self,statement,params=None):
                answer=self.inner.execute(statement,params)
                if statement.startswith('SELECT id,gateway_id') and not changed:
                    changed.append(True)
                    case.registry_command('device.state',second['device']['id'],expected_version=second['device']['version'],status='suspended')
                return answer
        class Connection:
            def __init__(self,inner): self.inner=inner
            def __getattr__(self,key): return getattr(self.inner,key)
            def __enter__(self): self.inner.__enter__(); return self
            def __exit__(self,*args): return self.inner.__exit__(*args)
            def cursor(self,**kw):
                self_setting=self.inner.execute("SELECT current_setting('transaction_isolation'),current_setting('transaction_read_only')").fetchone()
                case.assertEqual(self_setting,('repeatable read','on'))
                return Cursor(self.inner.cursor(**kw))
        def connect(): opened.append(True); return Connection(self.connect('reader'))
        result=RegistryContext(connect).resolve(keys,utc_start=anchor,start_ns=0,end_ns=1,uncertainty_ns=0,revision=None)
        self.assertEqual(result.status,'resolved')
        self.assertEqual(len(opened),1)
        fresh=self.resolve(keys,anchor)
        self.assertEqual(fresh.status,'ineligible')
        self.assertEqual(fresh.revision,result.revision+1)

    def resolver(self):
        from backend.waveforms.registry_context import RegistryContext
        return RegistryContext(lambda:self.connect('reader'))

    def resolve(self,keys,anchor,start=0,end=1,uncertainty=0,revision=None):
        return self.resolver().resolve(keys,utc_start=anchor,start_ns=start,end_ns=end,
                                       uncertainty_ns=uncertainty,revision=revision)

    def test_fraction_just_before_migration_boundary_and_exact_end(self):
        from datetime import timedelta
        source=self.history_source()
        second=self.registry_command('well.create',area_code='A',well_code='SECOND',name='Second synthetic well')['entity']
        changed=self.registry_command('device.bind',source['device']['id'],expected_version=source['device']['version'],well_id=second['id'])
        end=datetime.fromisoformat(changed['committed_at'])
        key=(CaptureKey('GW','D',uuid4()),)
        inside=self.resolve(key,end-timedelta(microseconds=1),end=Fraction(1999,2))
        self.assertEqual(inside.status,'resolved')
        self.assertEqual(inside.well['id'],source['well']['id'])
        crossing=self.resolve(key,end-timedelta(microseconds=1),end=1000)
        self.assertEqual(crossing.status,'ineligible')
        self.assertIn('binding_boundary',crossing.resolution_reasons)
        self.assertEqual(inside.upper_ns,'1999/2')
        after=self.resolve(key,end,end=0)
        self.assertEqual(after.status,'ineligible')
        self.assertIn('inactive_source',after.resolution_reasons)
        self.assertEqual(after.sources[0]['well']['id'],second['id'])

    def test_all_sources_same_well_and_unknown_utc(self):
        first=self.history_source(); second=self.history_source(device='D2',well=first['well'])
        keys=(CaptureKey('GW','D',uuid4()),CaptureKey('GW','D2',uuid4()))
        anchor=datetime.fromisoformat(second['active_at'])
        result=self.resolve(keys,anchor)
        self.assertEqual(result.status,'resolved')
        self.assertEqual(len(result.sources),2)
        self.assertEqual(self.resolve(keys,None,uncertainty=None).status,'pending')
        self.assertEqual(self.resolve(keys,anchor,revision=result.revision-1).status,'pending')
        third=self.history_source(device='D3')
        self.assertEqual(self.resolve((keys[0],CaptureKey('GW','D3',uuid4())),datetime.fromisoformat(third['active_at'])).status,'ineligible')

    def test_corrected_support_before_registration_fails_and_archive_preserves_history(self):
        from datetime import timedelta
        source=self.history_source(); key=(CaptureKey('GW','D',uuid4()),)
        anchor=datetime.fromisoformat(source['active_at'])
        corrected=self.resolve(key,anchor,start=-100000000,end=600000000,uncertainty=50000000)
        self.assertNotEqual(corrected.status,'resolved')
        unbound=self.registry_command('device.unbind',source['device']['id'],expected_version=source['device']['version'])
        self.registry_command('well.change',source['well']['id'],expected_version=source['well']['version'],status='archived')
        history=self.resolve(key,datetime.fromisoformat(unbound['committed_at'])-timedelta(microseconds=1),end=0)
        self.assertEqual(history.status,'resolved')
        self.assertEqual(history.well['id'],source['well']['id'])
