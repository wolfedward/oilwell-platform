"""Literal display oracles and real owned-database window boundaries."""
from fractions import Fraction
import importlib
import importlib.util
import unittest
from tests.waveform_dataset_support import DatasetCase
from tests.waveform_pairing_support import seed_pair,CREATOR,REVIEWER
from unittest.mock import patch
from uuid import UUID


class SummaryTests(unittest.TestCase):
    def module(self):
        self.assertIsNotNone(importlib.util.find_spec('backend.waveforms.windows'), 'Task14 windows implementation missing')
        return importlib.import_module('backend.waveforms.windows')

    def summary(self, values, budget, start=0):
        w=self.module()
        def source():
            for i,value in enumerate(values,start):
                yield w.DisplaySample(i,(Fraction(value),) if value is not None else None,('missing_sample',) if value is None else ())
        return w.select_summary(source,budget)

    def test_summary_points_are_original_ordered_samples(self):
        result=self.summary([2,5,5,-1,-1,3],4)
        self.assertEqual([p.index for p in result.points],[0,1,3,5])
        self.assertEqual([p.values[0] for p in result.points],[2,5,-1,3])

    def test_missing_not_connected(self):
        result=self.summary([1,None,2,None,3],3)
        self.assertEqual([p.index for p in result.points],[0,2,4])
        self.assertEqual([(g.first_index,g.last_index) for g in result.gaps],[('1','1'),('3','3')])

    def test_budget_including_segment_boundaries(self):
        self.assertEqual(self.summary([1,None,2,None,3],2).status,'gap_overflow')
        self.assertEqual(self.summary([1,None,2,None,3],2).points,())
        self.assertEqual(self.summary([0,5,-4,1],3).status,'budget_insufficient')
        self.assertEqual(self.summary([3,1,-2,1,3],2).status,'budget_insufficient')
        self.assertEqual([p.index for p in self.summary([3,1,-2,1,3],3).points],[0,2,4])

    def test_raw_window_precision(self):
        w=self.module()
        from tests.waveform_pairing_support import pure_case
        from dataclasses import replace
        stream=replace(pure_case()[0].streams[0].stream,first_sample_index=9007199254740992,rate_num=3)
        self.assertEqual(w.point_time(stream,9007199254740993,0),'1000000000/3')
        self.assertEqual(w.point_time(stream,9007199254740993,-7),'999999979/3')
        result=self.summary([0,4,1],3,9007199254740992)
        self.assertIn(9007199254740993,[p.index for p in result.points])

    def test_singleton_all_missing_two_buckets(self):
        self.assertEqual([p.index for p in self.summary([None,7,None],1,10).points],[11])
        result=self.summary([None]*100,1)
        self.assertEqual(result.status,'ok'); self.assertEqual(result.points,())
        self.assertEqual([(g.first_index,g.last_index) for g in result.gaps],[('0','99')])
        self.assertEqual([p.index for p in self.summary([0,3,1,-2,2,4,-1,6,2,5],8).points],[0,1,3,4,5,6,7,9])

    def test_summary_source_above_raw_cap_and_actual_samples(self):
        values=[(i%101)-50 for i in range(20003)]
        result=self.summary(values,2000)
        self.assertLessEqual(len(result.points),2000)
        self.assertEqual([p.index for p in result.points],sorted(set(p.index for p in result.points)))
        self.assertEqual((result.points[0].index,result.points[-1].index),(0,20002))
        for p in result.points: self.assertEqual(p.values,(Fraction(values[p.index]),))

    def test_f32_extremes_and_defensive_conversion_overflow(self):
        w=self.module()
        import struct
        maximum=struct.unpack('<f',bytes.fromhex('ffff7f7f'))[0]
        self.assertTrue(w.finite_float(Fraction(maximum)*999999999999999999)>0)
        self.assertTrue(w.finite_float(Fraction(-maximum))<0)
        result=self.summary([1,maximum,-maximum,2],4)
        self.assertEqual([p.index for p in result.points],[0,1,2,3])
        # Defensive helper input is outside the approved evidence grammar.
        self.assertIsNone(w.finite_float(Fraction(10**1000)))

    def test_gap_storage_bounded_for_adversarial_input(self):
        result=self.summary([None if i%2 else i for i in range(20003)],2)
        self.assertEqual((result.status,result.points,result.gaps),('gap_overflow',(),()))

    def test_pinned_calibration_identity_and_half_open_validity(self):
        w=self.module()
        from tests.waveform_pairing_support import config_body,calibration_body,evidence,pure_case
        from backend.waveforms.contracts import ManifestFrame
        from dataclasses import replace
        from backend.waveforms.evidence_contracts import freeze,validate_body
        context,_=pure_case(); stream=context.streams[1].stream; channel=context.streams[1].channel
        config=evidence('sampling_config',config_body('load','kN',rate=3))
        body=calibration_body(config)|dict(scale='1.5',offset='-2',valid_to='2026-09-29T00:00:00.333333Z')
        cal=evidence('calibration',body); channel=replace(channel,calibration_id=cal.id)
        stream=replace(stream,rate_num=3,channels=(channel,),frame_bytes=5)
        frame=ManifestFrame(context.key,'a'*64,dict(source_kind='synthetic',utc_start='2026-09-29T00:00:00.000000Z',utc_uncertainty_ns='0'),(stream,))
        deps={str(config.id):config,str(cal.id):cal}
        value=w.Calibration(frame,stream,channel,deps,0,1)
        self.assertIn('calibration_expired_or_revoked',value.reasons)
        self.assertEqual(value.value(-2,0),(Fraction(-5),None))
        self.assertEqual(w.Calibration(frame,stream,channel,deps,0,0).reasons,set())
        wrong=replace(cal,body=freeze(body|dict(sampling_config_sha256='0'*64)))
        value=w.Calibration(frame,stream,channel,deps|{str(cal.id):wrong},0,0)
        self.assertEqual(value.value(100,0),(None,'config_mismatch'))
        for scale in ('1e999','1000000000000000000'):
            with self.assertRaises(Exception): validate_body('calibration',body|dict(scale=scale))

    def test_mismatched_range_unit_is_a_visible_historical_restriction(self):
        w=self.module()
        from tests.waveform_pairing_support import pure_case
        from backend.waveforms.contracts import ManifestFrame
        from backend.waveforms.evidence_contracts import freeze,thaw
        from dataclasses import replace
        context,_=pure_case(); d=context.streams[1]; key=(d.key,d.stream.stream_id,d.channel.channel_id)
        config=context.sampling_configs[key]; cal=context.calibrations[key]; body=thaw(cal.body)
        body['calibrated_range']['unit']='W'; cal=replace(cal,body=freeze(body))
        frame=ManifestFrame(d.key,'a'*64,dict(source_kind='synthetic',utc_start=None,utc_uncertainty_ns=None),(d.stream,))
        value=w.Calibration(frame,d.stream,d.channel,{str(cal.id):cal,str(config.id):config},0,2)
        self.assertIn('unknown_units',value.reasons)


class WindowDatabaseTests(DatasetCase):
    def service(self):
        w=importlib.import_module('backend.waveforms.windows')
        self.assertTrue(hasattr(w,'WaveformWindows'),'Audited engineering windows missing')
        from backend.waveforms.queries import WaveformQueries
        return w.WaveformWindows(WaveformQueries(lambda:self.connect('reader')),actor=CREATOR)

    def test_multichannel_bounded_raw_and_summary_audit_counts(self):
        service=self.service(); vector=seed_pair(self,count=8193); frame=vector['frames'][0]
        from backend.waveforms.quality_context import ContextBuilder
        with patch.object(ContextBuilder,'prepare',side_effect=AssertionError('Full quality preparation forbidden')):
            seen=[]; raw=service.queries.raw
            def observed(id,actor): seen.append(id); return raw(id,actor)
            with patch.object(service.queries,'raw',side_effect=observed):
                result=service.read(frame.key,'sensor',start=4095,count=4,channel_ids=('displacement','load'),mode='raw',budget=20000)
            self.assertEqual([c.metadata.channel_id for c in result.channels],['load','displacement'])
            self.assertEqual([p.index for p in result.channels[0].points],['4095','4096','4097','4098'])
            self.assertEqual([p.value for p in result.channels[0].points],[10.,10.,10.,10.])
            self.assertEqual(len(seen),3); self.assertEqual(len(set(seen)),3)
            seen.clear()
            with patch.object(service.queries,'raw',side_effect=observed):
                result=service.read(frame.key,'sensor',start=0,count=8193,channel_ids=('load','displacement'),mode='summary',budget=16)
            self.assertEqual(len(seen),7)
            self.assertEqual(sorted(seen.count(id) for id in set(seen)),[1,2,2,2])
            for channel in result.channels:
                self.assertLessEqual(len(channel.points),16)
                self.assertEqual((channel.points[0].index,channel.points[-1].index),('0','8192'))

    def test_summary_above_raw_cap_and_non_good_quality_gap(self):
        service=self.service(); vector=seed_pair(self,count=20003,bad_index=20001,quality=1); frame=vector['frames'][0]
        result=service.read(frame.key,'sensor',start=0,count=20003,channel_ids=('load',),mode='summary',budget=2000)
        self.assertLessEqual(len(result.channels[0].points),2000)
        self.assertEqual([(g.first_index,g.last_index) for g in result.channels[0].gaps],[('20001','20001')])
        from backend.waveforms.errors import WaveformError
        with self.assertRaises(WaveformError): service.read(frame.key,'sensor',start=0,count=20001,channel_ids=('load',),mode='raw',budget=20000)

    def test_revoked_calibration_is_visible_historical_display(self):
        service=self.service(); vector=seed_pair(self); frame=vector['frames'][0]
        cal=next(c['calibration'] for c in vector['selection']['channels'] if c['role']=='load'); id=UUID(cal['id'])
        vector['evidence_service'].revoke(id,vector['evidence_command']('revoke',id,2,REVIEWER))
        result=service.read(frame.key,'sensor',start=0,count=3,channel_ids=('load',),mode='raw',budget=20000).channels[0]
        self.assertEqual([p.value for p in result.points],[10.,10.,10.])
        self.assertIn('historical_only',result.restrictions); self.assertIn('calibration_expired_or_revoked',result.restrictions)
        self.assertEqual(result.metadata.calibration.review_state,'revoked')

    def test_review_change_during_read_refuses_stale_result(self):
        service=self.service(); vector=seed_pair(self); frame=vector['frames'][0]
        cal=next(c['calibration'] for c in vector['selection']['channels'] if c['role']=='load'); id=UUID(cal['id'])
        raw=service.queries.raw; calls=0
        def changed(fact,actor):
            nonlocal calls
            value=raw(fact,actor); calls+=1
            if calls==2: vector['evidence_service'].revoke(id,vector['evidence_command']('revoke',id,2,REVIEWER))
            return value
        from backend.waveforms.errors import WaveformError
        with patch.object(service.queries,'raw',side_effect=changed),self.assertRaises(WaveformError) as caught:
            service.read(frame.key,'sensor',start=0,count=3,channel_ids=('load',),mode='raw',budget=20000)
        self.assertEqual(caught.exception.code,'waveform.stale_dependency')

    def cloned(self,vector,*,skip=(),first_index=None,route_device=None):
        """New synthetic capture facts; never rewrites the accepted original."""
        from tests.waveform_vectors import encoded
        from tests.test_waveform_repository import observation
        from backend.waveforms.repository import WaveformRepository
        from backend.waveforms.wire import parse_frame
        from tests.waveform_support import LIMITS
        from uuid import uuid4
        from hashlib import sha256
        import json
        wire=vector['wire_captures'][0]; body=json.loads(wire[0]); body['capture_id']=str(uuid4())
        if route_device: body['device_id']=route_device
        if first_index is not None:
            for stream in body['streams']: stream['first_sample_index']=str(first_index)
            if body['cycle']:
                for event in body['cycle']['reference_events']: event['sample_index']=str(first_index+int(event['sample_index']))
        manifest=encoded(body); frame=parse_frame(manifest)
        repo=WaveformRepository(self.connect,limits=LIMITS,prefix='oil',ingress_gateway_id='GW')
        def put(payload): return repo.ingest(observation(payload,topic=f'oil/v2/GW/{frame.key.device_id}/waveform'))
        put(manifest); pending=[]
        for raw in wire[1:]:
            piece=json.loads(raw); piece.update(capture_id=body['capture_id'],device_id=body['device_id'],manifest_sha256=sha256(manifest).hexdigest()); piece=encoded(piece)
            decoded=json.loads(piece)
            if (decoded['stream_id'],decoded['chunk_index']) in skip: pending.append(piece)
            else: put(piece)
        return frame,put,pending

    def test_audited_raw_large_original_index_fractional_time_and_flags(self):
        from backend.waveforms.queries import public
        service=self.service(); vector=seed_pair(self,count=7,bad_index=2,quality=2)
        frame,_,_=self.cloned(vector,first_index=9007199254740992,route_device='COLLECTOR')
        result=service.read(frame.key,'sensor',start=9007199254740993,count=3,channel_ids=('load',),mode='raw',budget=20000)
        points=public(result)['channels'][0]['points']
        self.assertEqual([p['index'] for p in points],['9007199254740993','9007199254740994','9007199254740995'])
        self.assertEqual([p['relative_ns'] for p in points],['1000000000/3','2000000000/3','1000000000'])
        self.assertEqual([p['quality'] for p in points],[0,2,0]); self.assertEqual([p['value'] for p in points],[10.,None,10.])
        self.assertEqual([p['raw_index'] for p in points],[p['index'] for p in points])
        self.assertEqual((result.key.device_id,result.source.device_id,result.source_kind),('COLLECTOR','D0','synthetic'))

    def test_collecting_missing_slot_and_late_chunk_stales_summary(self):
        service=self.service(); vector=seed_pair(self,count=8193)
        frame,put,pending=self.cloned(vector,skip=(('sensor',1),))
        result=service.read(frame.key,'sensor',start=4095,count=4098,channel_ids=('load',),mode='raw',budget=20000)
        self.assertEqual([p.index for p in result.channels[0].points],['4095','8192'])
        self.assertEqual([(g.first_index,g.last_index,g.reasons) for g in result.channels[0].gaps],[('4096','8191',('missing_chunk',))])
        raw=service.queries.raw; count=0
        def late(fact,actor):
            nonlocal count
            data=raw(fact,actor); count+=1
            if count==2: put(pending[0])
            return data
        from backend.waveforms.errors import WaveformError
        with patch.object(service.queries,'raw',side_effect=late),self.assertRaises(WaveformError) as caught:
            service.read(frame.key,'sensor',start=4095,count=4098,channel_ids=('load',),mode='summary',budget=8)
        self.assertEqual(caught.exception.code,'waveform.stale_dependency')

    def test_conflicting_candidate_is_never_added_to_curve(self):
        service=self.service(); vector=seed_pair(self)
        from tests.waveform_vectors import encoded
        from tests.test_waveform_repository import observation
        from backend.waveforms.repository import WaveformRepository
        from tests.waveform_support import LIMITS
        import json,base64,struct
        from hashlib import sha256
        frame=vector['frames'][0]; chunk=next(json.loads(r) for r in vector['wire_captures'][0][1:] if json.loads(r)['stream_id']=='sensor')
        data=bytearray(base64.b64decode(chunk['data_base64'])); data[:4]=struct.pack('<i',999)
        chunk.update(data_base64=base64.b64encode(data).decode(),data_sha256=sha256(data).hexdigest())
        repo=WaveformRepository(self.connect,limits=LIMITS,prefix='oil',ingress_gateway_id='GW')
        repo.ingest(observation(encoded(chunk),topic=f'oil/v2/GW/{frame.key.device_id}/waveform'))
        result=service.read(frame.key,'sensor',start=0,count=3,channel_ids=('load',),mode='raw',budget=20000)
        self.assertEqual([p.value for p in result.channels[0].points],[10.,10.,10.])
        self.assertIn('capture_conflict',result.restrictions)

    def test_complete_missing_slot_is_integrity_failure(self):
        service=self.service(); vector=seed_pair(self); original=service.queries.window_snapshot
        def missing(*args,**kw):
            pin=original(*args,**kw)
            if kw.get('ranges'): pin['slots']=()
            return pin
        from backend.waveforms.errors import WaveformError
        with patch.object(service.queries,'window_snapshot',side_effect=missing),self.assertRaises(WaveformError) as caught:
            service.read(vector['frames'][0].key,'sensor',start=0,count=3,channel_ids=('load',),mode='raw',budget=20000)
        self.assertEqual(caught.exception.code,'waveform.window_integrity')
