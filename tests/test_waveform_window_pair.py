"""Bounded XY uses declared originals and current historical-quality anchors."""
import importlib
import importlib.util
from dataclasses import replace
from fractions import Fraction
import unittest
from unittest.mock import patch
from uuid import UUID
from tests.waveform_dataset_support import DatasetCase
from tests.waveform_pairing_support import pure_case,CREATOR,REVIEWER


class XYTests(unittest.TestCase):
    def module(self):
        self.assertIsNotNone(importlib.util.find_spec('backend.waveforms.window_pair'),'Bounded XY consumer missing')
        return importlib.import_module('backend.waveforms.window_pair')

    def test_actual_fractional_reference_cross_capture_adjacent_provenance(self):
        xy=self.module(); context,_=pure_case(); reference=context.streams[2]; load=context.streams[1]
        first=9007199254740993
        rs=replace(reference.stream,first_sample_index=first,rate_num=3)
        ls=replace(load.stream,first_sample_index=10,rate_num=2)
        ref=xy.SparseChannel(reference.key,rs,reference.channel,first,first+2,Fraction(1),Fraction(0),0,Fraction(0))
        target=xy.SparseChannel(load.key,ls,load.channel,10,12,Fraction(1),Fraction(0),-300000000,Fraction(50000000))
        ref.samples={first:(0,0),first+1:(1,0),first+2:(0,0)}; target.samples={10:(10,0),11:(30,0),12:(50,0)}
        result=[xy.align_original(ref,target,i) for i in range(first,first+3)]
        self.assertEqual([p.index for p in result],[str(first),str(first+1),str(first+2)])
        self.assertEqual([p.relative_ns for p in result],['0','1000000000/3','2000000000/3'])
        self.assertEqual([p.load for p in result],[20.,float(Fraction(100,3)),float(Fraction(140,3))])
        self.assertEqual([p.load_indices for p in result],[('10','11'),('11','12'),('11','12')])
        self.assertEqual([p.displacement for p in result],[0.,1.,0.])
        self.assertTrue(all(p.load_interpolated and not p.displacement_interpolated for p in result))

    def test_xy_envelope_union_requires_six_original_points(self):
        self.module()
        from backend.waveforms.windows import DisplaySample,select_summary
        x=[0,5,1,-4,2,3]; y=[0,2,8,1,-7,4]
        def source():
            return iter(DisplaySample(i,(Fraction(x[i]),Fraction(y[i]))) for i in range(6))
        self.assertEqual(select_summary(source,5).status,'budget_insufficient')
        self.assertEqual([p.index for p in select_summary(source,6).points],[0,1,2,3,4,5])

    def test_target_gap_between_summary_endpoints_survives(self):
        xy=self.module(); context,_=pure_case(); d=context.streams[2]; l=context.streams[1]
        from backend.waveforms.samples import Sample
        from backend.waveforms.windows import select_summary
        from types import SimpleNamespace
        stream=replace(d.stream,frame_count=5,rate_num=4)
        ref=xy.SparseChannel(d.key,stream,d.channel,0,4,Fraction(1),Fraction(0),0,Fraction(0))
        load=xy.SparseChannel(l.key,stream,l.channel,0,4,Fraction(1),Fraction(0),0,Fraction(0))
        def source():
            a=iter((i,Sample(i,d.channel.channel_id,i,0)) for i in range(5))
            b=iter((i,Sample(i,l.channel.channel_id,None if i==2 else 10,1 if i==2 else 0)) for i in range(5))
            return xy.xy_samples(SimpleNamespace(check=lambda:None),ref,load,a,b)
        self.assertEqual(select_summary(source,2).status,'gap_overflow')
        result=select_summary(source,4)
        self.assertEqual([p.index for p in result.points],[0,1,3,4])
        self.assertEqual([(g.first_index,g.last_index) for g in result.gaps],[('2','2')])

    def test_adjacent_cursor_skipped_reasons_have_constant_memory(self):
        xy=self.module()
        from backend.waveforms.samples import Sample
        import tracemalloc
        tracemalloc.start()
        try:
            cursor=xy.AdjacentCursor((i,Sample(i,'load',0,3)) for i in range(100000))
            data,reasons=cursor.take((99999,))
            _,peak=tracemalloc.get_traced_memory()
        finally: tracemalloc.stop()
        self.assertEqual(reasons,('bad',)); self.assertEqual(tuple(data),(99999,))
        self.assertLess(peak,131072,'Skipped reason storage must not grow with target source frames')

    def test_source_metadata_preserves_physical_source_not_route_identity(self):
        xy=self.module(); context,_=pure_case(); d=context.streams[1]
        self.assertTrue(hasattr(xy,'source_metadata'),'Self-contained physical source metadata missing')
        from backend.waveforms.contracts import CaptureKey,ManifestFrame
        frame=ManifestFrame(CaptureKey('COLLECTOR','ROUTE',UUID(int=7)),'a'*64,dict(source_kind='synthetic'),(d.stream,))
        value=xy.source_metadata(frame,d.stream,d.channel)
        self.assertEqual((value.key.gateway_id,value.key.device_id),('COLLECTOR','ROUTE'))
        self.assertEqual((value.source.gateway_id,value.source.device_id,value.source.boot_id),('GW','D','00000000-0000-0000-0000-000000000002'))
        self.assertEqual(value.source_kind,'synthetic')


class XYDatabaseTests(DatasetCase):
    def service(self):
        self.assertIsNotNone(importlib.util.find_spec('backend.waveforms.window_pair'),'Bounded XY consumer missing')
        from backend.waveforms.windows import WaveformWindows
        from backend.waveforms.queries import WaveformQueries
        return WaveformWindows(WaveformQueries(lambda:self.connect('reader')),actor=CREATOR)

    def test_same_run_pair_preview_is_bounded_original_order(self):
        service=self.service(); pair,vector=self.pair()
        from backend.waveforms.window_pair import read_pair
        from backend.waveforms.quality_context import ContextBuilder
        self.assertEqual(pair.left_quality_run_id,pair.right_quality_run_id)
        with patch.object(ContextBuilder,'prepare',side_effect=AssertionError('Full quality read forbidden')):
            result=read_pair(service,pair.id,start=0,count=3,mode='raw',budget=20000)
        self.assertEqual(result.status,'ok',result.restrictions)
        self.assertEqual([p.displacement for p in result.points],[0.,4.,0.])
        self.assertEqual([p.load for p in result.points],[10.,10.,10.])
        self.assertFalse(result.closed); self.assertFalse(result.quantitative_use_allowed)

    def test_current_revocation_refuses_xy_numbers(self):
        service=self.service(); pair,vector=self.pair()
        from backend.waveforms.window_pair import read_pair
        cal=next(c['calibration'] for c in vector['selection']['channels'] if c['role']=='load'); id=UUID(cal['id'])
        vector['evidence_service'].revoke(id,vector['evidence_command']('revoke',id,2,REVIEWER))
        result=read_pair(service,pair.id,start=0,count=3,mode='raw',budget=20000)
        self.assertEqual(result.points,()); self.assertIn('selected_evidence_changed',result.restrictions)

    def test_two_capture_approved_pair_uses_real_reference(self):
        service=self.service(); pair,vector=self.pair(two=True)
        from backend.waveforms.window_pair import read_pair
        result=read_pair(service,pair.id,start=0,count=3,mode='summary',budget=3)
        self.assertEqual(result.status,'ok',result.restrictions)
        self.assertEqual([p.index for p in result.points],['0','1','2'])
        self.assertEqual(result.displacement_source.source.device_id,'D1')
        self.assertEqual(result.load_source.source.device_id,'D1')
        self.assertEqual(result.displacement_source.source_kind,'synthetic')
        self.assertEqual(result.load_source.key,vector['frames'][1].key)

    def test_two_capture_final_dependencies_share_one_reader_snapshot(self):
        service=self.service()
        self.assertTrue(hasattr(service.queries,'recheck_pair'),'Final joint dependency snapshot missing')
        pair,vector=self.pair(two=True)
        from backend.waveforms.window_pair import read_pair
        original=service.queries.recheck_pair; connect=service.queries.connect; calls=[]
        def final(*args,**kwargs):
            with patch.object(service.queries,'connect',side_effect=lambda:(calls.append(True),connect())[1]):
                return original(*args,**kwargs)
        with patch.object(service.queries,'recheck_pair',side_effect=final):
            result=read_pair(service,pair.id,start=0,count=3,mode='raw')
        self.assertEqual(result.status,'ok'); self.assertEqual(len(calls),1)

    def test_multichunk_xy_constant_read_passes_without_full_quality(self):
        from tests.waveform_pairing_support import seed_pair
        from backend.waveforms.pairing import PairService
        from backend.waveforms.commands import Command,pair_request_hash
        from tests.waveform_support import LIMITS
        from uuid import uuid4
        from backend.waveforms.window_pair import read_pair
        from backend.waveforms.quality_context import ContextBuilder
        service=self.service(); vector=seed_pair(self,count=8193)
        pairs=PairService(lambda:self.connect('operator'),limits=LIMITS,shared_accounting_bucket='GW',read_connect=lambda:self.connect('reader'))
        body=dict(selection=vector['selection'],method_version='linear_adjacent/1'); command=Command(CREATOR,uuid4(),0,'Synthetic','0'*64)
        pair=pairs.propose(body,replace(command,request_hash=pair_request_hash('propose',None,command,body=body)))
        seen=[]; raw=service.queries.raw
        def audit(id,actor): seen.append(id); return raw(id,actor)
        with patch.object(service.queries,'raw',side_effect=audit),patch.object(ContextBuilder,'prepare',side_effect=AssertionError('No full preparation')):
            result=read_pair(service,pair.id,start=4095,count=4,mode='raw')
        self.assertEqual(len(result.points),4); self.assertIn('pair_not_approved',result.restrictions)
        self.assertEqual(sorted(seen.count(id) for id in set(seen)),[1,2,2])
        seen.clear()
        with patch.object(service.queries,'raw',side_effect=audit):
            result=read_pair(service,pair.id,start=0,count=8193,mode='summary',budget=16)
        self.assertEqual(sorted(seen.count(id) for id in set(seen)),[1,4,4,4])
        self.assertLessEqual(len(result.points),16)

    def test_historical_validator_and_capture_anchor_are_checked(self):
        service=self.service(); pair,vector=self.pair(); original=service.queries.pair_window_snapshot
        from backend.waveforms.window_pair import read_pair
        from backend.waveforms.errors import WaveformError
        from uuid import uuid4
        def unsupported(id,**kwargs):
            value=original(id,**kwargs); value['runs'][0]['validator_version']='waveform.quality/unsupported'; return value
        with patch.object(service.queries,'pair_window_snapshot',side_effect=unsupported):
            result=read_pair(service,pair.id,start=0,count=3,mode='raw')
        self.assertEqual(result.points,()); self.assertIn('unsupported_validator',result.restrictions)
        bad_id=uuid4()
        def bad_anchor(id,**kwargs):
            value=original(id,**kwargs); value['runs'][0]['capture_pk']=bad_id; return value
        with patch.object(service.queries,'pair_window_snapshot',side_effect=bad_anchor),self.assertRaises(WaveformError) as caught:
            read_pair(service,pair.id,start=0,count=3,mode='raw')
        self.assertEqual(caught.exception.code,'waveform.window_integrity')

    def test_corrected_target_interpolation_audits_neighbor_chunk_only(self):
        from collections import Counter
        from tests.waveform_pairing_support import seed_pair,calibration_body
        from tests.waveform_vectors import encoded
        from tests.waveform_support import LIMITS
        from backend.waveforms.commands import Command,pair_request_hash
        from backend.waveforms.pairing import PairService
        from backend.waveforms.quality_context import ContextBuilder
        from backend.waveforms.window_pair import read_pair
        from uuid import uuid4
        # Customize only new fixture input, before real evidence/intake/assembly.
        # At8000Hz one period is125000ns; load is corrected half a period early.
        def corrected(config):
            body=calibration_body(config)
            if config.body['quantity']=='load': body['time_correction_ns']='-62500'
            return body
        def declared(body):
            if body['kind']=='manifest':
                body['cycle']['end_ns']='1999875000'
                body['cycle']['reference_events'][-1]['sample_index']='15999'
            return encoded(body)
        with patch('tests.waveform_pairing_support.calibration_body',side_effect=corrected),patch('tests.waveform_vectors.encoded',side_effect=declared):
            vector=seed_pair(self,count=16001)
        service=self.service(); frame=vector['frames'][0]
        pairs=PairService(lambda:self.connect('operator'),limits=LIMITS,shared_accounting_bucket='GW',read_connect=lambda:self.connect('reader'))
        body=dict(selection=vector['selection'],method_version='linear_adjacent/1'); command=Command(CREATOR,uuid4(),0,'Synthetic neighboring chunk','0'*64)
        pair=pairs.propose(body,replace(command,request_hash=pair_request_hash('propose',None,command,body=body)))
        self.assertEqual(pair.body['joint_result']['status'],'eligible',pair.body['joint_result'])
        # Obtain identities independently from accepted-slot SQL, not window ranges.
        with self.connect('reader') as conn:
            capture=conn.execute('SELECT capture_pk,manifest_fact_id FROM waveforms.capture_metadata WHERE capture_id=%s',(frame.key.capture_id,)).fetchone()
            slots={(s,i):fact for s,i,fact in conn.execute('SELECT stream_id,chunk_index,fact_id FROM waveforms.chunk_slots WHERE capture_pk=%s',(capture[0],)).fetchall()}
        self.assertEqual(set(slots),{(s,i) for s in ('electrical','sensor') for i in range(4)})
        manifest,chunk0,neighbor=capture[1],slots[('sensor',0)],slots[('sensor',1)]
        allowed={manifest,chunk0,neighbor}; seen=[]; raw=service.queries.raw
        def audit(id,actor):
            self.assertIn(id,allowed,'Unrelated accepted chunk must not be read')
            seen.append(id); return raw(id,actor)
        with patch.object(service.queries,'raw',side_effect=audit),patch.object(ContextBuilder,'prepare',side_effect=AssertionError('Bounded read must not reevaluate full quality')):
            result=read_pair(service,pair.id,start=4095,count=1,mode='raw')
        self.assertEqual(result.status,'ok',result.restrictions); self.assertEqual(result.gaps,())
        self.assertEqual(Counter(seen),Counter({manifest:1,chunk0:2,neighbor:1}))
        self.assertEqual(len(result.points),1); point=result.points[0]
        # Literal oracle: t=4095*125000, target4095/4096 at t-/+62500.
        self.assertEqual((point.index,point.relative_ns,point.displacement,point.load),('4095','511875000',2.,10.))
        self.assertEqual(point.displacement_indices,('4095',)); self.assertEqual(point.load_indices,('4095','4096'))
        self.assertFalse(point.displacement_interpolated); self.assertTrue(point.load_interpolated)
        self.assertEqual(point.displacement_qualities,(0,)); self.assertEqual(point.load_qualities,(0,0))

    def test_pending_unconfigured_pair_has_explicit_restriction(self):
        from backend.waveforms.pairing import PairService
        from backend.waveforms.commands import Command,pair_request_hash
        from tests.waveform_support import LIMITS
        from uuid import uuid4
        from backend.waveforms.window_pair import read_pair
        service=self.service()
        pairs=PairService(lambda:self.connect('operator'),limits=LIMITS,shared_accounting_bucket='GW',read_connect=lambda:self.connect('reader'))
        selection=dict(purpose='raw_archive',profile=None,captures=[dict(key=dict(gateway_id='GW',device_id='D',capture_id=str(uuid4())),manifest_sha256=None,origins=[])],channels=[],cycle=None,sync=None)
        body=dict(selection=selection,method_version='linear_adjacent/1'); command=Command(CREATOR,uuid4(),0,'Synthetic missing dependencies','0'*64)
        pair=pairs.propose(body,replace(command,request_hash=pair_request_hash('propose',None,command,body=body)))
        try: result=read_pair(service,pair.id,start=0,count=1,mode='raw')
        except TypeError: self.fail('Missing selected profile must not crash bounded display')
        self.assertEqual(result.points,()); self.assertIn('historical_quality_ineligible',result.restrictions)

    def test_mixed_capture_dependency_views_are_refused_in_joint_snapshot(self):
        import threading
        from backend.waveforms.window_pair import references
        from backend.waveforms.pair_contracts import capture_key
        from backend.waveforms.errors import WaveformError
        service=self.service(); pair,vector=self.pair(two=True)
        initial=service.queries.pair_window_snapshot(pair.id); refs=references(vector['selection']); params=dict(evidence_ids=tuple(refs))
        first,second=[capture_key(c['key']) for c in vector['selection']['captures']]
        old=service.queries.window_snapshot(first,**params)
        changed=threading.Event(); failures=[]
        def revoke():
            try:
                sync=vector['sync']
                vector['evidence_service'].revoke(sync.id,vector['evidence_command']('revoke',sync.id,2,REVIEWER))
            except Exception as error: failures.append(error)
            finally: changed.set()
        thread=threading.Thread(target=revoke); thread.start(); self.assertTrue(changed.wait(5)); thread.join(5)
        self.assertEqual(failures,[])
        newer=service.queries.window_snapshot(second,**params)
        self.assertNotEqual(old['evidence'],newer['evidence'])
        with self.assertRaises(WaveformError) as caught:
            service.queries.recheck_pair(pair.id,initial,{first:(old,params),second:(newer,params)})
        self.assertEqual(caught.exception.code,'waveform.stale_dependency')


from tests.waveform_registry_support import WaveformRegistryCase


class XYFreshnessTests(DatasetCase,WaveformRegistryCase):
    def test_real_registry_change_refuses_reuse_and_mid_read_change_stales(self):
        from backend.waveforms.windows import WaveformWindows
        from backend.waveforms.queries import WaveformQueries
        from backend.waveforms.window_pair import read_pair
        from backend.waveforms.errors import WaveformError
        pair,vector=self.pair(); service=WaveformWindows(WaveformQueries(lambda:self.connect('reader')),actor=CREATOR)
        raw=service.queries.raw; changed=False
        def race(id,actor):
            nonlocal changed
            value=raw(id,actor)
            if not changed:
                changed=True; self.registry_command('well.create',area_code='A',well_code='NEW-WINDOW',name='Synthetic')
            return value
        with patch.object(service.queries,'raw',side_effect=race),self.assertRaises(WaveformError) as caught:
            read_pair(service,pair.id,start=0,count=3,mode='raw')
        self.assertEqual(caught.exception.code,'waveform.stale_dependency')
        result=read_pair(service,pair.id,start=0,count=3,mode='raw')
        self.assertEqual(result.points,()); self.assertIn('current_quality_recheck_required',result.restrictions)
