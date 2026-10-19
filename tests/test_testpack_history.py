from dataclasses import replace
import json
import math
import unittest
from backend.gateway.contracts import UplinkBatch
from tools.testpack.contracts import PackConfig
from tools.testpack.history import generate_history,integrate_intervals,telemetry_point_map


class TestPackHistory(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.cases=generate_history(replace(PackConfig(),profile='extended'))

    def test_extended_history_exact_counts(self):
        frames=[];days=[]
        for case in self.cases:
            frames += [json.loads(line) for a in case.artifacts if a.path.endswith('.jsonl') for line in a.content.splitlines()]
            days += [d for a in case.artifacts if a.path.endswith('.json') for d in json.loads(a.content)['daily']]
        self.assertEqual(len(frames),10752)
        self.assertEqual(sum(len(f['samples']) for f in frames),129024)
        self.assertEqual(len(days),112)
        self.assertTrue(all(d['coverage_fraction']==1 for d in days))
        self.assertEqual(days[0]['date'],'2026-10-01')
        self.assertEqual(days[-1]['date'],'2026-10-14')

    def test_energy_and_volume_are_integrals(self):
        intervals=[{'start':'2026-09-30T16:00:00Z','end':'2026-09-30T16:30:00Z',
                    'flow_m3_h':2,'power_kw':1,'quality':'good'},
                   {'start':'2026-09-30T16:30:00Z','end':'2026-09-30T17:00:00Z',
                    'flow_m3_h':0,'power_kw':1,'quality':'good'},
                   {'start':'2026-09-30T17:00:00Z','end':'2026-09-30T18:00:00Z',
                    'flow_m3_h':0,'power_kw':-.5,'quality':'good'}]
        totals=integrate_intervals(intervals)[0]
        self.assertEqual(totals['liquid_m3'],1)
        self.assertEqual(totals['imported_kwh'],1)
        self.assertEqual(totals['exported_kwh'],.5)
        self.assertEqual(totals['net_kwh'],.5)

    def test_daily_timezone_and_missing_coverage(self):
        rows=[{'start':'2026-10-01T15:30:00Z','end':'2026-10-01T16:30:00Z',
               'flow_m3_h':2,'power_kw':1,'quality':'good'},
              {'start':'2026-10-01T16:30:00Z','end':'2026-10-01T17:00:00Z',
               'flow_m3_h':999,'power_kw':999,'quality':'bad'}]
        a,b=integrate_intervals(rows)
        self.assertEqual(a['date'],'2026-10-01')
        self.assertEqual(b['date'],'2026-10-02')
        self.assertEqual(a['liquid_m3'],1)
        self.assertEqual(b['liquid_m3'],1)
        self.assertEqual(b['covered_seconds'],1800)
        self.assertEqual(b['missing_seconds'],1800)
        self.assertLess(b['coverage_fraction'],1)

    def test_same_condition_daily_fluctuation_correlates(self):
        days=json.loads(next(a.content for a in self.cases[0].artifacts if a.path.endswith('.json')))['daily']
        x=[d['liquid_m3'] for d in days];y=[d['net_kwh'] for d in days]
        xm,ym=sum(x)/len(x),sum(y)/len(y)
        correlation=sum((a-xm)*(b-ym) for a,b in zip(x,y))/math.sqrt(sum((a-xm)**2 for a in x)*sum((b-ym)**2 for b in y))
        self.assertGreater(max(x)-min(x),1)
        self.assertGreater(correlation,.8)

    def test_smoke_two_unit_banks_and_wire_provenance(self):
        cases=generate_history(PackConfig())
        self.assertEqual(len(cases),2)
        self.assertNotEqual(cases[0].facts['device_id'],cases[1].facts['device_id'])
        for unit,case in enumerate(cases,1):
            point_map=telemetry_point_map(unit)
            self.assertEqual([p.address for p in point_map],list(range(0,24,2)))
            self.assertTrue(all(p.unit_id==unit and p.function_code==4 and not p.writable for p in point_map))
            lines=next(a.content for a in case.artifacts if a.path.endswith('.jsonl')).splitlines()
            self.assertEqual(len(lines),8)
            for line in lines:
                batch=UplinkBatch.from_json(line)
                self.assertEqual(len(batch.samples),12)
                self.assertTrue(all(s.source=='software-fixture-modbus-tcp' for s in batch.samples))
                values={s.point_code:s.value for s in batch.samples}
                self.assertAlmostEqual(values['current_a']*math.sqrt(3)*values['voltage_v']*values['power_factor']/1000,
                                       values['active_power_kw'],delta=.0001)

    def test_overlapping_intervals_rejected(self):
        interval={'start':'2026-10-01T00:00:00Z','end':'2026-10-01T01:00:00Z','flow_m3_h':1,'power_kw':1,'quality':'good'}
        with self.assertRaises(ValueError):
            integrate_intervals([interval,interval])


if __name__=='__main__':
    unittest.main()
