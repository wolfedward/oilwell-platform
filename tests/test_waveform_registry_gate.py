"""Coordinated history locking on the owned waveform sidecar only."""
from concurrent.futures import ThreadPoolExecutor
from hashlib import sha256
import threading
import unittest

from tests.waveform_registry_support import WaveformRegistryCase


class RegistryHistoryGateTests(WaveformRegistryCase):
    def test_consumer_passes_corrected_exact_support_and_uncertainty_once(self):
        from dataclasses import replace
        from fractions import Fraction
        from backend.waveforms.contracts import CaptureKey,ManifestFrame
        from backend.waveforms.quality_context import ContextBuilder
        from tests.test_waveform_timing import stream
        from backend.waveforms.timing import ChannelData
        from tests.waveform_pairing_support import pure_case
        from tests.waveform_support import LIMITS
        from uuid import UUID
        import struct
        from backend.waveforms.registry_context import utc_text
        from datetime import datetime
        source=self.history_source(); anchor=utc_text(datetime.fromisoformat(source['active_at'])); key=CaptureKey('GW','D',UUID(int=1))
        s=stream(count=8,num=10,offset=100000000)
        data=ChannelData(key,s,s.channels[0],struct.pack('<iB',1,0)*8,0,7,-200000000)
        context,_=pure_case(); context=replace(context,key=key,streams=(data,))
        frame=ManifestFrame(key,'a'*64,dict(utc_start=anchor,utc_uncertainty_ns='50000000'),(s,))
        builder=ContextBuilder(lambda:self.connect('operator'),limits=LIMITS,shared_accounting_bucket='GW',read_connect=lambda:self.connect('reader'))
        snapshot=builder.registry(context,dict(manifests={key:frame}))
        self.assertEqual((snapshot.start_ns,snapshot.end_ns,snapshot.lower_ns,snapshot.upper_ns),('-100000000','600000000','-150000000','650000000'))
        self.assertNotEqual(snapshot.status,'resolved')
        from backend.waveforms.registry_context import contains_window
        self.assertTrue(contains_window(0,1000,Fraction(0),Fraction(1999,2)))
        self.assertFalse(contains_window(0,1000,Fraction(0),Fraction(1000)))

    def test_existing_registry_commit_replay_conflict_and_rollback(self):
        from backend.registry.transactions import RegistryTransactions
        from backend.registry.service import RegistryService
        from backend.registry.contracts import Actor,validate_command
        from backend.registry.errors import RegistryError
        from uuid import uuid4
        actor=Actor('fixture','1','test','Synthetic','administrator')
        body=dict(area_code='A',well_code='BEHAVIOR',name='Synthetic',reason='Synthetic only',idempotency_key=str(uuid4()))
        command=validate_command('well.create',None,actor,body,method='POST',route='/fixture/well.create')
        service=RegistryService(RegistryTransactions(self.registry_connect))
        first=service.execute(command); replay=service.execute(command)
        self.assertEqual(replay,first|{'replayed':True})
        conflict=validate_command('well.create',None,actor,body|{'name':'Changed'},method='POST',route='/fixture/well.create')
        with self.assertRaises(RegistryError): service.execute(conflict)
        with self.registry_connect() as conn:
            self.assertEqual(conn.execute('SELECT revision FROM device_registry.registry_head').fetchone()[0],2)
            self.assertEqual(conn.execute('SELECT count(*) FROM device_registry.audit_events').fetchone()[0],1)
        failing=validate_command('well.create',None,actor,body|{'idempotency_key':str(uuid4()),'well_code':'ROLLBACK'},method='POST',route='/fixture/well.create')
        def fail(cur,*args):
            cur.execute('UPDATE device_registry.registry_head SET revision=99')
            raise ValueError('synthetic failure before business commit')
        with self.assertRaises(ValueError): RegistryTransactions(self.registry_connect).run(failing,fail)
        with self.registry_connect() as conn: self.assertEqual(conn.execute('SELECT revision FROM device_registry.registry_head').fetchone()[0],2)
        # Rollback released EXCLUSIVE, so another transaction can take SHARED.
        from backend.registry.transactions import lock_history
        with self.connect('operator') as conn:
            conn.execute("SET LOCAL lock_timeout='100ms'"); lock_history(conn.cursor(),shared=True)

    def test_literal_history_guard_and_registry_writer_waits(self):
        from backend.registry import transactions
        self.assertTrue(hasattr(transactions,'lock_history'),'Task11 history coordination absent')
        literal=b'["waveform.registry-lock/1","history"]'
        self.assertEqual(sha256(literal).hexdigest(),'2f6faf87fb472cd6d7e379ef290d04433738b48edcbc4f88d6a78291ba9553ef')
        self.assertEqual(transactions.REGISTRY_HISTORY_GUARD,3418143640768687318)
        entered=threading.Event()
        with self.connect('operator') as shared:
            transactions.lock_history(shared.cursor(),shared=True)
            def writer():
                entered.set()
                return self.registry_command('well.create',area_code='A',well_code='LOCK',name='synthetic')
            with ThreadPoolExecutor(max_workers=1) as pool:
                future=pool.submit(writer); self.assertTrue(entered.wait(2))
                # Bounded query observes the writer waiting on an advisory lock.
                import time
                deadline=time.monotonic()+.7; waiting=False
                while time.monotonic()<deadline:
                    with self.registry_connect() as observer:
                        waiting=observer.execute("SELECT EXISTS(SELECT 1 FROM pg_locks WHERE locktype='advisory' AND NOT granted)").fetchone()[0]
                    if waiting: break
                    time.sleep(.01)
                self.assertTrue(waiting)
                shared.commit()
                self.assertEqual(future.result(timeout=3)['registry_revision'],'2')
