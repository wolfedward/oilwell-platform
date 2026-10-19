"""Interval-mean telemetry with volume/energy integration, not field production."""
import csv
from datetime import datetime,timedelta,timezone
import io
import math
from uuid import NAMESPACE_URL,uuid5

from backend.gateway.contracts import PointDefinition,TelemetrySample,UplinkBatch
from .contracts import Artifact,Case,PackConfig
from .storage import canonical_bytes,derive_uniform,generator_fingerprint,pack_identity

BUSINESS_TZ=timezone(timedelta(hours=8),'Asia/Shanghai')
POINTS=('flow_rate_m3_h','active_power_kw','voltage_v','current_a','power_factor','strokes_per_min',
        'stroke_m','load_min_kn','load_max_kn','liquid_level_m','motor_temperature_c','pump_running')
UNITS=('m3/h','kW','V','A','1','1/min','m','kN','kN','m','degC','1')


def telemetry_point_map(unit_id:int=1):
    return tuple(PointDefinition(name,unit_id,4,index*2,'float32',unit=unit)
                 for index,(name,unit) in enumerate(zip(POINTS,UNITS)))


def _instant(value):
    parsed=datetime.fromisoformat(value.replace('Z','+00:00'))
    if parsed.utcoffset()!=timedelta(0):
        raise ValueError('interval timestamps must be UTC')
    return parsed


def _utc(value):
    return value.astimezone(timezone.utc).isoformat(timespec='microseconds').replace('+00:00','Z')


def integrate_intervals(intervals):
    days={}
    previous=None
    for row in intervals:
        start,end=_instant(row['start']),_instant(row['end'])
        if end<=start or previous is not None and start<previous:
            raise ValueError('intervals must be ordered, positive and non-overlapping')
        previous=end
        if row['quality'] not in ('good','bad','uncertain'):
            raise ValueError('invalid quality')
        flow,power=row['flow_m3_h'],row['power_kw']
        if any(type(x) not in (int,float) or not math.isfinite(x) for x in (flow,power)) or flow<0:
            raise ValueError('finite interval means required')
        while start<end:
            local=start.astimezone(BUSINESS_TZ)
            day=local.date().isoformat()
            midnight=local.replace(hour=0,minute=0,second=0,microsecond=0)
            boundary=(midnight+timedelta(days=1)).astimezone(timezone.utc)
            stop=min(end,boundary)
            seconds=(stop-start).total_seconds()
            data=days.setdefault(day,{'date':day,'start_utc':_utc(midnight),'end_utc':_utc(boundary),
                                     'liquid':[],'import':[],'export':[],'covered_seconds':0,'missing_seconds':0})
            if row['quality']=='good':
                data['liquid'].append(flow*seconds/3600)
                data['import'].append(max(power,0)*seconds/3600)
                data['export'].append(max(-power,0)*seconds/3600)
                data['covered_seconds']+=seconds
            else:
                data['missing_seconds']+=seconds
            start=stop
    output=[]
    for data in days.values():
        covered=data['covered_seconds']
        liquid,imported,exported=(math.fsum(data.pop(key)) if covered else None for key in ('liquid','import','export'))
        data.update(liquid_m3=liquid,imported_kwh=imported,exported_kwh=exported,
                    net_kwh=imported-exported if covered else None,coverage_fraction=covered/86400,
                    unobserved_seconds=86400-covered-data['missing_seconds'],complete_day=covered==86400)
        output.append(data)
    return output


def generate_history(config:PackConfig)->tuple[Case,...]:
    start=_instant(config.start_utc)
    if start.astimezone(BUSINESS_TZ).time().isoformat()!='00:00:00':
        raise ValueError('history start must be Shanghai midnight for full-day accounting')
    wells,count=(2,8) if config.profile=='smoke' else (8,14*96)
    pack_id=pack_identity(config,generator_fingerprint())
    cases=[]
    for unit_id in range(1,wells+1):
        well=f'TESTPACK-WELL-{unit_id:03d}'
        device=f'TESTPACK-PUMP-{unit_id:03d}'
        case_id=f'history-{unit_id:03d}'
        boot=str(uuid5(NAMESPACE_URL,pack_id+'/'+case_id+'/boot'))
        lines=[];intervals=[]
        table=io.StringIO(newline='')
        writer=csv.writer(table,lineterminator='\n')
        writer.writerow(['well_id','simulated_at','interval_end','source',*(f'{name} [{unit}]' for name,unit in zip(POINTS,UNITS))])
        for i in range(count):
            day=i//96
            day_variation=math.sin(day*1.23)+.25*(2*derive_uniform(config.seed,case_id,'day',day)-1)
            intraday=math.sin(math.tau*(i%96)/96)
            duty=.68+.13*day_variation+.035*intraday
            fill=.75+.018*unit_id+.02*math.sin(.7*day+unit_id)
            stroke=3.2+.07*unit_id
            spm=5.4+.14*unit_id+.15*intraday
            area=math.pi*(.057/2)**2
            flow=area*stroke*spm*60*fill*duty
            # Positive hydraulic work plus mechanical/electrical losses;
            # power is the same interval's signed mean at the supply boundary.
            load_span_N=28000*fill
            power=(load_span_N*stroke*spm/60/1000/.8+.6)*duty+.3*(1-duty)
            voltage=398+3*math.sin(i*.19+unit_id)
            pf=.84+.015*intraday
            current=power*1000/(math.sqrt(3)*voltage*pf)
            values=(flow,power,voltage,current,pf,spm,stroke,32.0,32+load_span_N/1000,
                    120+8*day_variation+2*intraday,42+10*duty,1.0)
            at=start+timedelta(minutes=15*i);end=at+timedelta(minutes=15)
            values=tuple(round(v,9) for v in values)
            samples=tuple(TelemetrySample('REVIEW-GW',device,name,at,i*12+j,value,unit=unit,
                           source='software-fixture-modbus-tcp') for j,(name,unit,value) in enumerate(zip(POINTS,UNITS,values)))
            batch=UplinkBatch.create('REVIEW-GW',device,boot,samples,
                   message_id=str(uuid5(NAMESPACE_URL,pack_id+'/'+case_id+f'/frame-{i}')),created_at=at)
            raw=batch.to_json().encode('utf-8')
            UplinkBatch.from_json(raw)
            lines.append(raw)
            intervals.append({'start':_utc(at),'end':_utc(end),'flow_m3_h':values[0],'power_kw':values[1],'quality':'good'})
            writer.writerow([well,_utc(at),_utc(end),'software-fixture-modbus-tcp',*values])
        metadata={'schema':'owp.testpack.history/1','well_id':well,'device_id':device,'unit_id':unit_id,
                  'pack_id':pack_id,'interval_seconds':900,'frame_count':count,
                  'source_kind':'synthetic','daily':integrate_intervals(intervals),
                  'assumptions':{'plunger_diameter_m':.057,'electrical_efficiency':.8,
                    'mean_power':'positive hydraulic work plus declared losses, interval mean',
                    'current':'balanced three-phase RMS: abs(P)/(sqrt(3)*U*PF); no instantaneous ADC measurement',
                    'pump_running':'enabled state; runtime duty fraction enters interval means',
                    'quantities':'daily values are observed integrals; incomplete days are not extrapolated'}}
        stem='history/'+case_id
        cases.append(Case(case_id,'mechanistic',(
            Artifact(stem+'.jsonl','application/x-ndjson',b'\n'.join(lines)+b'\n'),
            Artifact(stem+'.csv','text/csv',table.getvalue().encode('utf-8')),
            Artifact(stem+'.json','application/json',canonical_bytes(metadata))),
            {'data_kind':'history','well_id':well,'device_id':device,'unit_id':unit_id,
             'frame_count':count,'sample_count':count*12,'daily_count':len(metadata['daily']),
             'network_received_count':0},
            ({'check_id':case_id+'-wire','layer':'offline','assertion':'equals','target':stem+'.jsonl','expected':count},)))
    return tuple(cases)
