"""Allowlisted, non-secret public reports derived from observed check results."""
from collections import Counter
from dataclasses import asdict
from datetime import datetime,timedelta
from hashlib import sha256
from pathlib import Path
import math
import re
from uuid import UUID

from .contracts import CheckResult
from .storage import canonical_bytes,load_pack,safe_path,strict_json

LAYERS=('offline','numeric','software_network','http','browser')
STATUSES={'passed','failed','not_run','blocked'}
REQUIRED={'modbus-mqtt-storage':'software_network','waveform-evidence-http':'http',
          'studies-http':'http','inversion-comparison':'numeric','viewer-interaction':'browser'}
COUNTS={'baseline_messages','baseline_samples','retained_messages','additional_fault_samples',
        'queued_during_outage','recovered','frame_count','decoded_bytes','elapsed_seconds',
        'retained_records','exported','records','false_samples_created','covered_seconds',
        'domain_speed_torque_bound_Nm','fixed_torque_bound_Nm','fixed_speed_bound_rad_s',
        'uncovered_speed_error_rad_s','coverage_fraction','rmse_N'}
FLAGS={'field_validation','control_enabled','control_eligible','dispatch_allowed','hardware_execution',
       'fault_confirmation','causal_effect_proven','shadow_only','hashes_verified','idempotent',
       'original_hashes_unchanged','broker_restart_tested','parameter_submitted','human_signatures'}
PUBLIC_KEYS={'schema','pack_id','pack_sha256','generator','counts','run_metadata','status','layers',
             'checks','hardware','limitations','private_evidence_sha256'}
LIMITS=['synthetic_not_field_measurement','no_hardware_or_field_validation','no_remote_control',
        'fixed_inversion_budget_not_approved','software_actor_reviews_not_human_signatures']
STUDY_KINDS={'condition_training','condition_prediction','recovery_fit','energy_schedule','energy_effects','motor_spectrum','protection_replay'}
STUDY_STATUS={'completed','fitted','feasible','descriptive','rejected','no_feasible_candidate','insufficient_evidence'}
STUDY_METRICS=('best_epoch','epochs_executed','accepted_count','excluded_count','evaluated_combinations',
               'metrics.rmse_m','metrics.r2','parameters.tau_minutes','parameters.h_inf_m')


def study_record_projection(rows):
    output=[]
    for row in rows:
        if row['kind'] not in STUDY_KINDS or row['status'] not in STUDY_STATUS:raise ValueError('unknown study result')
        item={k:row[k] for k in ('id','kind','status','version','input_sha256','result_sha256','dependency_current','eligible')}
        item['metrics']={}
        for path in STUDY_METRICS:
            value=row['result']
            for key in path.split('.'):value=value.get(key) if isinstance(value,dict) else None
            if type(value) in (int,float) and math.isfinite(value):item['metrics'][path]=value
        output.append(item)
    return output


def overall_status(checks):
    values=[c.status if isinstance(c,CheckResult) else c['status'] for c in checks]
    if any(v not in STATUSES for v in values):raise ValueError('unknown check status')
    if 'failed' in values:return 'failed'
    if 'blocked' in values:return 'blocked'
    if not values or 'not_run' in values:return 'incomplete'
    return 'passed'


def _metadata(value):
    if type(value) is not dict or set(value)-{'run_id','lab_id','started_at','finished_at','source_commit','deployment_commits'}:
        raise ValueError('unknown run metadata')
    for key,item in value.items():
        if key in ('run_id','lab_id'):UUID(item)
        elif key in ('started_at','finished_at'):
            parsed=datetime.fromisoformat(item.replace('Z','+00:00'))
            if parsed.utcoffset()!=timedelta(0):raise ValueError('explicit UTC run time required')
        elif key=='source_commit':
            if not re.fullmatch('[0-9a-f]{40,64}',item):raise ValueError('invalid commit')
        elif key=='deployment_commits':
            if set(item)!={'monitoring','review'} or any(not re.fullmatch('[0-9a-f]{40,64}',v) for v in item.values()):
                raise ValueError('invalid deployment commits')
    return value


def _safe_actual(actual):
    output={'counts':{},'flags':{},'record_ids':[],'digests':[],'study_records':[]}
    if isinstance(actual,dict) and 'study_records' in actual:
        output['study_records']=study_record_projection(actual['study_records'])
    def visit(value,depth=0):
        if depth>24:return
        if isinstance(value,dict):
            for key,item in value.items():
                if key in COUNTS and type(item) in (float,int) and math.isfinite(item):output['counts'].setdefault(key,item)
                if key in FLAGS and type(item) is bool:output['flags'].setdefault(key,item)
                if key in ('record_id','run_id','result_id','id','import_id','capture_id') and type(item) is str:
                    try:
                        if str(UUID(item))==item and item not in output['record_ids']:output['record_ids'].append(item)
                    except ValueError:pass
                if (key.endswith('_sha256') or key=='sha256') and type(item) is str and re.fullmatch('[0-9a-f]{64}',item):
                    if item not in output['digests']:output['digests'].append(item)
                if isinstance(item,(dict,list,tuple)):visit(item,depth+1)
        elif isinstance(value,(list,tuple)):
            for child in value[:20000]:visit(child,depth+1)
    visit(actual)
    return output


def _counts(pack):
    facts=[c['facts'] for c in pack.manifest['cases']]
    return {'cases':len(facts),'files':len(pack.manifest['artifacts']),
        'generated_history_frames':sum(f.get('frame_count',0) for f in facts if f.get('data_kind')=='history'),
        'generated_daily_rows':sum(f.get('daily_count',0) for f in facts if f.get('data_kind')=='history'),
        'cycle_cases':sum(f.get('data_kind')=='cycle' for f in facts),
        'shape_only_cases':sum(c['category']=='shape_only' for c in pack.manifest['cases'])}


def _layers(checks):
    return {layer:dict(Counter(c['status'] for c in checks if c['layer']==layer)) for layer in LAYERS}


def write_report(pack,checks,run_root,*,run_metadata):
    pack=load_pack(pack.root);root=safe_path(Path(run_root));_metadata(run_metadata)
    path=root/'report.json'
    if path.exists():raise ValueError('report already exists; preserve historical report')
    chosen=list(checks);ids=[c.check_id for c in chosen]
    if len(set(ids))!=len(ids):raise ValueError('duplicate check id')
    for key,layer in REQUIRED.items():
        if key not in ids:chosen.append(CheckResult(key,layer,'not_run',{},{}))
    public=[]
    for check in chosen:
        if check.layer not in LAYERS or not re.fullmatch('[a-zA-Z0-9_-]{1,120}',check.check_id):raise ValueError('check layer/id')
        overall_status((check,))
        public.append({'check_id':check.check_id,'layer':check.layer,'status':check.status,
            'expected_sha256':sha256(canonical_bytes(check.expected)).hexdigest(),
            'actual_sha256':sha256(canonical_bytes(check.actual)).hexdigest(),'actual':_safe_actual(check.actual)})
    evidence={'checks':[asdict(c) for c in chosen],'run_metadata':run_metadata}
    result={'schema':'owp.testpack.public-report/1','pack_id':pack.manifest['pack_id'],
        'pack_sha256':sha256((pack.root/'manifest.json').read_bytes()).hexdigest(),
        'generator':pack.manifest['generator'],'counts':_counts(pack),'run_metadata':run_metadata,
        'status':overall_status(chosen),'layers':_layers(public),'checks':public,
        'hardware':{'status':'not_run'},'limitations':LIMITS,
        'private_evidence_sha256':sha256(canonical_bytes(evidence)).hexdigest()}
    validate_public(result)
    with path.open('xb') as stream:stream.write(canonical_bytes({'schema':'owp.testpack.private-report/1','public':result,'evidence':evidence}))
    return path


def validate_public(body):
    if type(body) is not dict or set(body)!=PUBLIC_KEYS or body['schema']!='owp.testpack.public-report/1':raise ValueError('public fields')
    if not re.fullmatch('tp-[0-9a-f]{24}',body['pack_id']):raise ValueError('pack id')
    for key in ('pack_sha256','private_evidence_sha256'):
        if not re.fullmatch('[0-9a-f]{64}',body[key]):raise ValueError('public digest')
    from .storage import _generator
    _generator(body['generator']);_metadata(body['run_metadata'])
    if set(body['counts'])!={'cases','files','generated_history_frames','generated_daily_rows','cycle_cases','shape_only_cases'} or any(type(v) is not int or v<0 for v in body['counts'].values()):raise ValueError('public counts')
    if body['hardware']!={'status':'not_run'} or body['limitations']!=LIMITS:raise ValueError('public scope')
    if type(body['checks']) is not list or len(body['checks'])>10000:raise ValueError('public checks')
    seen=set()
    for check in body['checks']:
        if set(check)!={'check_id','layer','status','expected_sha256','actual_sha256','actual'}:raise ValueError('public check fields')
        if not re.fullmatch('[a-zA-Z0-9_-]{1,120}',check['check_id']) or check['check_id'] in seen or check['layer'] not in LAYERS:raise ValueError('public check identity')
        seen.add(check['check_id'])
        for key in ('actual_sha256','expected_sha256'):
            if not re.fullmatch('[0-9a-f]{64}',check[key]):raise ValueError('check digest')
        actual=check['actual']
        if set(actual)!={'counts','flags','record_ids','digests','study_records'} or set(actual['counts'])-COUNTS or set(actual['flags'])-FLAGS:raise ValueError('public actual fields')
        if any(type(v) not in (int,float) or not math.isfinite(v) for v in actual['counts'].values()):raise ValueError('public metric')
        if any(type(v) is not bool for v in actual['flags'].values()):raise ValueError('public flag')
        if any(str(UUID(v))!=v for v in actual['record_ids']):raise ValueError('public record id')
        if any(not re.fullmatch('[0-9a-f]{64}',v) for v in actual['digests']):raise ValueError('public actual digest')
        if not isinstance(actual['study_records'],list) or len(actual['study_records'])>100:raise ValueError('study record count')
        for row in actual['study_records']:
            if set(row)!={'id','kind','status','version','input_sha256','result_sha256','dependency_current','eligible','metrics'}:raise ValueError('study fields')
            if str(UUID(row['id']))!=row['id'] or row['kind'] not in STUDY_KINDS or row['status'] not in STUDY_STATUS:raise ValueError('study identity')
            if type(row['version']) is not int or row['version']<1 or any(type(row[k]) is not bool for k in ('dependency_current','eligible')):raise ValueError('study state')
            if any(not re.fullmatch('[0-9a-f]{64}',row[k]) for k in ('input_sha256','result_sha256')):raise ValueError('study digest')
            if set(row['metrics'])-set(STUDY_METRICS) or any(type(v) not in (int,float) or not math.isfinite(v) for v in row['metrics'].values()):raise ValueError('study metric')
    if not REQUIRED.keys()<=seen:raise ValueError('missing required checks')
    if body['status']!=overall_status(body['checks']) or body['layers']!=_layers(body['checks']):raise ValueError('report status not derived from checks')
    return body


def export_public_report(run_root,target):
    root=safe_path(Path(run_root));path=safe_path(root/'report.json')
    if path.stat().st_size>32*1024*1024:raise ValueError('private report too large')
    record=strict_json(path.read_bytes())
    if set(record)!={'schema','public','evidence'} or record['schema']!='owp.testpack.private-report/1':raise ValueError('private report fields')
    public=validate_public(record['public'])
    if sha256(canonical_bytes(record['evidence'])).hexdigest()!=public['private_evidence_sha256']:raise ValueError('private evidence changed')
    evidence=record['evidence']
    if set(evidence)!={'checks','run_metadata'} or evidence['run_metadata']!=public['run_metadata'] or len(evidence['checks'])!=len(public['checks']):
        raise ValueError('public evidence binding mismatch')
    for original,shown in zip(evidence['checks'],public['checks']):
        expected={'check_id':original['check_id'],'layer':original['layer'],'status':original['status'],
            'expected_sha256':sha256(canonical_bytes(original['expected'])).hexdigest(),
            'actual_sha256':sha256(canonical_bytes(original['actual'])).hexdigest(),'actual':_safe_actual(original['actual'])}
        if shown!=expected:raise ValueError('public check differs from retained evidence')
    target=safe_path(Path(target),missing=True)
    if target.exists():raise ValueError('public target already exists')
    target.mkdir();output=target/'report.json'
    with output.open('xb') as stream:stream.write(canonical_bytes(public))
    return output
