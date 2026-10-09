"""Independent synthetic arithmetic, never field timing certification."""
from dataclasses import replace
from fractions import Fraction
import importlib
import struct
import unittest
from uuid import UUID

from backend.waveforms.contracts import CaptureKey, ChannelSpec, StreamSpec


def stream(*, first=0, num=100, den=1, offset=0, count=4, name='s'):
    channel=ChannelSpec('c','load','kN','i32le','synthetic','instantaneous','vendor',None)
    return StreamSpec(name,{'gateway_id':'GW','device_id':'D','boot_id':str(UUID(int=2))},
        'clock',num,den,first,offset,count,5,count,1,count*5,'a'*64,(channel,))


class TimingTests(unittest.TestCase):
    def setUp(self):
        try: self.t=importlib.import_module('backend.waveforms.timing')
        except ModuleNotFoundError: self.fail('Task11 timing implementation is absent')

    def data(self,s,values,qualities=None,correction=0):
        raw=b''.join(struct.pack('<iB',0 if v is None else v,q) for v,q in zip(values,qualities or [0]*len(values)))
        return self.t.ChannelData(CaptureKey('GW','D',UUID(int=1)),s,s.channels[0],raw,
            s.first_sample_index,s.first_sample_index+s.frame_count-1,correction)

    def test_delay_sign_and_unknown_budget(self):
        self.assertEqual(self.t.sample_time_ns(stream(),1,correction_ns=-1000000),Fraction(9000000))
        self.assertIsNone(self.t.sync_bound_ns(None,10000000000))

    def test_large_counter_fraction_and_correction_once(self):
        s=stream(first=9007199254741001,num=3,den=2,offset=-100000000)
        self.assertEqual([self.t.sample_time_ns(s,i,correction_ns=-200000000) for i in range(s.first_sample_index,s.first_sample_index+4)],
                         [Fraction(-300000000),Fraction(1100000000,3),Fraction(3100000000,3),Fraction(1700000000)])

    def test_interpolation_never_bridges_missing(self):
        s=stream(first=9007199254741001,num=3,den=2,offset=-100000000)
        d=self.data(s,[0,6,None,18],[0,0,1,0],-200000000)
        p=self.t.interpolate(d,Fraction(100000000,3))
        self.assertEqual((p.value,p.indices,p.interpolated),(Fraction(3),(9007199254741001,9007199254741002),True))
        for time in (Fraction(3100000000,3),Fraction(1000000000),Fraction(4000000000,3)):
            self.assertIsNone(self.t.interpolate(d,time).value)
        for time in (-300000001,1700000001):
            self.assertEqual(self.t.interpolate(d,Fraction(time)).gap,'outside_support')
        for flag in (2,3,4):
            other=self.data(s,[0,6,12,18],[0,0,flag,0],-200000000)
            self.assertIsNone(self.t.interpolate(other,Fraction(1000000000)).value)
            self.assertEqual(other.sample(s.first_sample_index+2),(12,flag))

    def test_negative_value_and_different_rate_goldens(self):
        d=self.data(stream(first=42,num=6,offset=100000000),[-3,0,3,6],correction=-100000000)
        for time,value,indices in ((0,-3,(42,)),(Fraction(100000000,3),Fraction(-12,5),(42,43)),
                                    (Fraction(1100000000,3),Fraction(18,5),(44,45)),(500000000,6,(45,))):
            point=self.t.interpolate(d,Fraction(time))
            self.assertEqual((point.value,point.indices),(value,indices))
        calibrated=replace(self.data(stream(count=3),[-2,0,2]),scale=Fraction('1.5'),offset=Fraction(1))
        self.assertEqual([calibrated.value(i) for i in range(3)],[-2,1,4])

    def test_absolute_correspondence_60_passes_61_fails_without_budget_utc(self):
        from backend.waveforms.contracts import ManifestFrame
        from backend.waveforms.evidence_contracts import sync_proof
        from tests.waveform_pairing_support import COMMON
        for difference,status in ((60,'resolved'),(61,'ineligible')):
            a=CaptureKey('GW','D',UUID(int=1)); b=CaptureKey('GW','D2',UUID(int=2))
            sa=stream(count=3,num=1); sb=replace(sa,source=sa.source|{'device_id':'D2'},offset_ns=difference)
            frames={a:ManifestFrame(a,'a'*64,dict(source_kind='synthetic',utc_start='2026-09-29T00:00:00.000000Z',utc_uncertainty_ns='20'),(sa,)),
                    b:ManifestFrame(b,'b'*64,dict(source_kind='synthetic',utc_start='2026-09-29T00:00:00.000000Z',utc_uncertainty_ns='30'),(sb,))}
            entries=[]
            for key,s,offset,residual in ((a,sa,0,0),(b,sb,5,5)):
                ep=dict(capture=dict(gateway_id=key.gateway_id,device_id=key.device_id,capture_id=str(key.capture_id)),
                        source=s.source,stream_id=s.stream_id,clock_domain_id=s.clock_domain_id)
                entries.append(dict(endpoint=ep,reference_sample_index='0',reference_event='documented edge',nominal_reference_ns=str(s.offset_ns),
                    rate=dict(num=1,den=1),valid_start_ns='0',valid_end_ns='3000000000',offset_error_ns=str(offset),frequency_error_ppm='0',residual_ns=str(residual),clock_divisor=None))
            body=dict(COMMON,attachment_ids=[str(UUID(int=9))],method='verified_linear_mapping',reference=entries[0]['endpoint'],endpoints=entries)
            self.assertEqual(sync_proof(body,frames,approved=True)['status'],status)
        budget=self.t.SyncBudget((dict(identity='a',reference_ns=0,start_ns=0,end_ns=1,offset_ns=0,residual_ns=0,ppm=Fraction(0)),
                                 dict(identity='b',reference_ns=0,start_ns=0,end_ns=1,offset_ns=5,residual_ns=5,ppm=Fraction(0))),('a','b'),(0,0))
        self.assertEqual(self.t.sync_bound_ns(budget,1),10)

    def test_drift_ages_from_original_reference(self):
        endpoints=(dict(identity='ref',reference_ns=Fraction(1000000000),start_ns=Fraction(11000000000),end_ns=Fraction(13000000000),offset_ns=0,residual_ns=25,ppm=Fraction(0)),
                   dict(identity='target',reference_ns=Fraction(1000000000),start_ns=Fraction(11000000000),end_ns=Fraction(13000000000),offset_ns=100,residual_ns=50,ppm=Fraction(2)))
        context=self.t.SyncBudget(endpoints,('ref','target'),(100,200))
        self.assertEqual(self.t.sync_bound_ns(context,2000000000),Fraction(24475))
        self.assertEqual(self.t.sync_bound_ns(self.t.SyncBudget(endpoints,('ref','ref'),(80,120)),2000000000),200)
        self.assertIsNone(self.t.sync_bound_ns(self.t.SyncBudget(endpoints,('ref','ref'),(None,120)),2000000000))

    def test_strict_index_and_no_extrapolation(self):
        from backend.waveforms.errors import ContractError
        for index in (True,-1,4,1.0):
            with self.assertRaises(ContractError): self.t.sample_time_ns(stream(),index,correction_ns=0)

    def test_preview_requires_original_sensor_times_and_limits_only_response(self):
        from backend.waveforms.errors import ContractError
        d=self.data(stream(num=1,count=3),[4,1,-2])
        result=self.t.align_pair((d,d,d),reference_times=(Fraction(0),Fraction(1000000000),Fraction(2000000000)))
        self.assertEqual(result.reference_indices,(0,1,2))
        self.assertEqual([p.value for p in result.left],[4,1,-2])
        for times in ((Fraction(500000000),),(Fraction(0),)*20001):
            with self.assertRaises(ContractError): self.t.align_pair((d,d,d),reference_times=times)
