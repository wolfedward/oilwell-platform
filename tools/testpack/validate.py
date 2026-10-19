"""Actual offline/numeric checks; software network and hardware are not inferred."""
from copy import deepcopy
from hashlib import sha256
from io import BytesIO
import json
import math

from backend.gateway.contracts import UplinkBatch
from backend.waveforms.files import scan_file
from backend.waveforms.errors import ContractError
from backend.studies.contracts import StudyError,validate_envelope
from backend.studies.condition_models import train_conditions,predict_conditions
from backend.studies.recovery import fit_recovery
from backend.studies.energy_schedule import optimize_schedule
from backend.studies.energy_effects import evaluate_effects
from backend.studies.spectrum import analyze_spectrum
from backend.studies.protection import replay_protection
from .contracts import Artifact,Case,CheckResult
from .storage import canonical_bytes,load_pack,safe_path
from .waveforms import decode_channels
from .history import integrate_intervals

ENGINES={'condition_training':train_conditions,'condition_prediction':predict_conditions,
         'recovery_fit':fit_recovery,'energy_schedule':optimize_schedule,'energy_effects':evaluate_effects,
         'motor_spectrum':analyze_spectrum,'protection_replay':replay_protection}


def _case(pack,record):
    lookup={a['path']:a for a in pack.manifest['artifacts']}
    return Case(record['case_id'],record['category'],tuple(Artifact(path,lookup[path]['media_type'],
                (pack.root/path).read_bytes()) for path in record['artifact_paths']),record['facts'],tuple(record['expected']))


def _check(check,actual,passed,artifacts=()):
    return CheckResult(check['check_id'],check['layer'],'passed' if passed else 'failed',
                       {'assertion':check['assertion'],'value':check['expected']},actual,tuple(artifacts))


def offline_passed(checks):
    required=[c for c in checks if c.layer in ('offline','numeric') and c.check_id!='inversion-comparison']
    return bool(required) and all(c.status=='passed' for c in required)


def validate_pack(pack):
    pack=load_pack(pack.root)  # A caller-constructed PackIndex is not a trust bypass.
    checks=[]
    cases=[_case(pack,r) for r in pack.manifest['cases']]
    for case in cases:
        if case.facts.get('data_kind')=='study':
            continue
        for check in case.expected:
            path=check['target'];raw=safe_path(pack.root/path).read_bytes()
            actual={'input_sha256':sha256(raw).hexdigest()}
            try:
                if path.endswith('.owpwave'):
                    parsed=scan_file(BytesIO(raw));observed='valid'
                    actual.update(frame_count=sum(s.frame_count for s in parsed.manifest.streams),
                                  decoded_bytes=sum(s.decoded_bytes for s in parsed.manifest.streams))
                elif path.endswith('.jsonl'):
                    batches=[UplinkBatch.from_json(line) for line in raw.splitlines()]
                    observed=len(batches)
                    if any(len(b.samples)!=12 or any(s.source!='software-fixture-modbus-tcp' for s in b.samples) for b in batches):
                        raise ValueError('history point count or source mismatch')
                else:
                    raise ValueError('unsupported declarative target')
            except ContractError as exc:
                observed=exc.code
            except (ValueError,TypeError,KeyError) as exc:
                observed=type(exc).__name__
            actual['observed']=observed
            checks.append(_check(check,actual,observed==check['expected'],[path]))
        if case.facts.get('data_kind')=='cycle':
            checks.append(_cycle_energy(case))
        elif case.facts.get('data_kind')=='history':
            checks.append(_history_totals(case))
        elif case.category=='shape_only':
            shape=json.loads(case.artifacts[0].content)
            valid=shape['points'][0]==shape['points'][-1] and shape['inversion_eligible'] is False
            checks.append(CheckResult(case.case_id+'-display-only','offline','passed' if valid else 'failed',
                {'closed':True,'inversion_eligible':False},{'closed':shape['points'][0]==shape['points'][-1],
                'inversion_eligible':shape['inversion_eligible']},(case.artifacts[0].path,)))
    checks.extend(_studies(cases))
    kinds={kind:sum(c.facts.get('data_kind')==kind for c in cases) for kind in ('cycle','current','history')}
    want={'cycle':8,'current':4,'history':2} if pack.manifest['profile']=='smoke' else {'cycle':48,'current':8,'history':8}
    checks.append(CheckResult('inventory','offline','passed' if kinds==want else 'failed',want,kinds))
    for name,layer in (('inversion-comparison','numeric'),('modbus-mqtt-storage','software_network'),
                        ('waveform-evidence-http','http'),('studies-http','http'),('viewer-interaction','browser')):
        checks.append(CheckResult(name,layer,'not_run',{'required_for':'isolated-lab delivery'},
                                  {'reason':'not executed by offline validator'}))
    return tuple(checks)


def _cycle_energy(case):
    metadata=json.loads(next(a.content for a in case.artifacts if a.path.endswith('.json')))
    channels=decode_channels(case)
    power=channels['active_power'];load=channels['load'];displacement=channels['displacement']
    dt=1/metadata['sample_rate_hz']
    energy=work=duration=0.0
    for i in range(1,metadata['frame_count']):
        if any(ch['quality'][j]!=0 for ch in (power,load,displacement) for j in (i-1,i)):
            continue
        energy+=(power['values'][i-1]+power['values'][i])*dt/2
        work+=(load['values'][i-1]+load['values'][i])*500*(displacement['values'][i]-displacement['values'][i-1])
        duration+=dt
    model=metadata['model'];motion=model['motion'];drive=model['drive']
    omega=math.tau/motion['period_s']
    # Integral omega² dt for theta=theta0+omega*t+epsilon*sin(omega*t).
    dissipation=(drive['loss_W']*motion['period_s']+drive['crank_friction_Nm']*math.tau+
                 drive['viscous_friction_Nm_s']*omega*math.tau*(1+motion['epsilon']**2/2))
    residual=energy-work-dissipation
    numeric=all(math.isfinite(v) for v in (energy,work,duration,residual))
    valid=numeric and (case.category!='mechanistic' or duration==8 and abs(residual)<=200)
    return CheckResult(case.case_id+'-energy','numeric','passed' if valid else 'failed',
        {'closed_mechanistic_cycle_residual_max_J':200,'fault_cases':'report valid support, not energy closure'},
        {'electrical_net_J':energy,'reference_work_J':work,'valid_duration_s':duration,
         'full_cycle_loss_J':dissipation,'closure_residual_J':residual if case.category=='mechanistic' else None,
         'category':case.category},tuple(a.path for a in case.artifacts))


def _history_totals(case):
    from datetime import timedelta
    from .history import _instant,_utc
    lines=next(a.content for a in case.artifacts if a.path.endswith('.jsonl')).splitlines()
    meta=json.loads(next(a.content for a in case.artifacts if a.path.endswith('.json')))
    intervals=[]
    for raw in lines:
        batch=UplinkBatch.from_json(raw)
        samples={s.point_code:s for s in batch.samples}
        flow,power=samples['flow_rate_m3_h'],samples['active_power_kw']
        if flow.unit!='m3/h' or power.unit!='kW':
            raise ValueError('history integration units mismatch')
        intervals.append({'start':_utc(flow.measured_at),'end':_utc(flow.measured_at+timedelta(seconds=meta['interval_seconds'])),
            'flow_m3_h':flow.value,'power_kw':power.value,
            'quality':'good' if flow.quality.value==power.quality.value=='good' else 'bad'})
    daily=integrate_intervals(intervals)
    return CheckResult(case.case_id+'-integration','numeric','passed' if daily==meta['daily'] else 'failed',
        {'daily_count':len(meta['daily']),'method':'interval-mean integration'},
        {'daily_count':len(daily),'daily_match':daily==meta['daily'],'integrated_liquid_m3':math.fsum(d['liquid_m3'] or 0 for d in daily),
         'integrated_net_kwh':math.fsum(d['net_kwh'] or 0 for d in daily)},tuple(a.path for a in case.artifacts))


def _studies(cases):
    studies=[c for c in cases if c.facts.get('data_kind')=='study']
    priority={name:i for i,name in enumerate(ENGINES)}
    studies.sort(key=lambda c:(priority[c.facts['study_kind']],c.case_id))
    results={};checks=[]
    for case in studies:
        template=json.loads(case.artifacts[0].content)
        envelope=template['envelope'];payload=deepcopy(envelope['payload']);kind=envelope['kind']
        check=case.expected[0]
        actual={'input_sha256':sha256(case.artifacts[0].content).hexdigest(),'mode':'pure_engine_no_service_approval'}
        try:
            if kind=='condition_prediction':
                upstream=results['study-condition-training']
                payload.update(model_record_id='offline-engine-only',artifact=upstream['artifact'])
            elif kind=='energy_schedule':
                upstream=results['study-recovery-fit']
                for well in payload['wells']:
                    if well['well_id']!=upstream['well_id']:
                        raise ValueError('offline recovery well mismatch')
                    well.update(recovery_record_id='offline-engine-only',recovery=upstream)
            if not template['dependencies']:
                validate_envelope(envelope)
            result=ENGINES[kind](payload)
            results[case.case_id]=result
            observed=result['status']
            actual.update(result_sha256=sha256(canonical_bytes(result)).hexdigest(),status=observed)
            for flag in ('field_validation','control_eligible','dispatch_allowed','hardware_execution','fault_confirmation','causal_effect_proven','shadow_only'):
                if flag in result:actual[flag]=result[flag]
            valid=observed==check['expected'] and all(actual.get(flag,False) is False for flag in
                ('field_validation','control_eligible','dispatch_allowed','hardware_execution','fault_confirmation','causal_effect_proven'))
        except StudyError as exc:
            actual['error_code']=exc.code
            valid=check['assertion']=='rejects' and exc.code==check['expected']
        except (ValueError,KeyError,TypeError) as exc:
            actual['error_code']=type(exc).__name__;valid=False
        checks.append(_check(check,actual,valid,[case.artifacts[0].path]))
    return checks
