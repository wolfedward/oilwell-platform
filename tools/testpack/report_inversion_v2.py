"""Strict public projection; never recursively copy a private workflow object."""
from fractions import Fraction as Q
from hashlib import sha256
import math
import re
from uuid import UUID

from backend.inversion.queries import wire
from backend.inversion.result_codec_v2 import PROOF_COLUMNS,PROOF_LIMITS
from .storage import canonical_bytes

FIELDS=set('''case_id assertion_status expected_outcome observed_outcome phase reason_code approved_execution_status
    source_kind field_accuracy_certified independent_field_comparison metrics proof_summary record_ids digests evidence_sha256'''.split())
METRICS={'original_coverage','joint_coverage','load_rmse_N','native_prediction_count','native_proof_count','export_bytes','enqueue_to_terminal_seconds'}
BOUNDS=dict(zip(PROOF_COLUMNS[-5:],PROOF_LIMITS))|{'max_base_speed_rad_s':Q(1,100000),'max_load_error_N':Q(1000)}


def validate(value):
    if type(value) is not dict or set(value)!=FIELDS:raise ValueError('inversion public fields')
    for key in ('case_id','phase','reason_code','expected_outcome','observed_outcome'):
        if type(value[key]) is not str or not re.fullmatch('[a-z0-9_.-]{0,128}',value[key]):raise ValueError('inversion public code')
    if (value['assertion_status'] not in ('passed','failed','blocked') or value['approved_execution_status'] not in ('passed','refused','not_run','failed')
        or value['source_kind']!='synthetic' or value['field_accuracy_certified'] is not False or value['independent_field_comparison'] is not False):
        raise ValueError('inversion public state')
    if type(value['metrics']) is not dict or set(value['metrics'])-METRICS:raise ValueError('inversion public metrics')
    for v in value['metrics'].values():
        if type(v) is str:
            if len(v)>2500 or not re.fullmatch(r'-?\d+(?:\.\d+|/\d+)?',v):raise ValueError('inversion public numeric')
            Q(v)
        elif type(v) not in (float,int) or not math.isfinite(v):raise ValueError('inversion public numeric')
    if set(value['record_ids'])-{'run_id','result_id','export_id'} or set(value['digests'])-{'result_sha256','export_sha256'}:raise ValueError('inversion public identity fields')
    if any(str(UUID(v))!=v for v in value['record_ids'].values()):raise ValueError('inversion public id')
    if any(not re.fullmatch('[0-9a-f]{64}',v) for v in [value['evidence_sha256'],*value['digests'].values()]):raise ValueError('inversion public digest')
    proof=value['proof_summary']
    if proof:
        if set(proof)!=set(BOUNDS)|{'verified'} or proof['verified'] is not True:raise ValueError('inversion proof fields')
        if any(type(proof[k]) is not str or len(proof[k])>2500 or not 0<=Q(proof[k])<=limit for k,limit in BOUNDS.items()):
            raise ValueError('inversion proof outside frozen limit')
    if value['approved_execution_status']=='passed':
        m=value['metrics']
        if (value['assertion_status']!='passed' or value['expected_outcome']!='completed' or value['observed_outcome']!='completed' or not proof or
            set(value['record_ids'])!={'run_id','result_id','export_id'} or set(value['digests'])!={'result_sha256','export_sha256'} or
            not {'original_coverage','joint_coverage','load_rmse_N','native_prediction_count','native_proof_count'}<=m.keys() or
            not Q(4,5)<=Q(m['original_coverage'])<=1 or not Q(4,5)<=Q(m['joint_coverage'])<=1 or
            not 0<=Q(m['load_rmse_N'])<=500 or m['native_prediction_count']!=1025 or m['native_proof_count']!=1025):
            raise ValueError('approved execution evidence incomplete')
    return value


def project(value):
    from .inversion_http_v2 import InversionOutcome
    if type(value) is not InversionOutcome:raise ValueError('typed observed inversion outcome required')
    output={k:getattr(value,k) for k in ('case_id','assertion_status','expected_outcome','observed_outcome','phase','reason_code','approved_execution_status')}
    output.update(source_kind='synthetic',field_accuracy_certified=False,independent_field_comparison=False,
        metrics={},proof_summary={},record_ids={},digests={},evidence_sha256=sha256(canonical_bytes(wire(value))).hexdigest())
    f=value.facts
    if value.approved_execution_status=='passed':
        try:
            run,result,review,export=f['run'],f['result'],f['review'],f['export'];body=result['body'];comp=f['comparison']['comparison']
            if (run['state']!='completed' or run['result_id']!=result['id'] or result['execution']!='completed' or result['source']!='synthetic' or
                review['state']!='approved' or result['current_applicability']['state']!='applicable' or
                body['prediction_status']!='applicable' or body['source_proof']['status']!='verified' or
                result['field_validation']['certified'] is not False or result['field_validation']['independent_comparison'] is not False or
                export['intent']!='quantitative' or export['result_sha256']!=result['content_sha256'] or comp['scope']!='original_valid_support'):
                raise ValueError('approved execution was not completed and reviewed')
            output['metrics']={'original_coverage':body['coverage_fraction'],'joint_coverage':comp['coverage']['joint_fraction']['value'],
                'load_rmse_N':comp['primary']['load_rmse']['value'],**{k:f[k] for k in ('native_prediction_count','native_proof_count')}}
            for k in ('export_bytes','enqueue_to_terminal_seconds'):
                if k in f:output['metrics'][k]=f[k]
            output['proof_summary']=wire(f['proof_summary'])
            output['record_ids']={'run_id':run['run_id'],'result_id':result['id'],'export_id':export['id']}
            output['digests']={'result_sha256':result['content_sha256'],'export_sha256':f['export_file_sha256']}
        except (KeyError,TypeError) as error:raise ValueError('approved execution evidence missing') from error
    return validate(output)
