"""Versioned, bounded proof blocks. Runtime SourceProof seals are never stored."""
from dataclasses import dataclass,fields
from decimal import Decimal,InvalidOperation
from fractions import Fraction as Q
from hashlib import sha256
import json
import re

from .contracts import canonical_bytes
from .input_contracts import InputSnapshot
from .result_codec import PreparedResult,MAX_RESULT,POINT_COLUMNS,INTERMEDIATES,EVALUATION_COLUMNS,FILL_COLUMNS,exact
from .transactions import InversionError

PROOF_COLUMNS=tuple('''time_ns power_index status reason_codes angle_indices motion_status motion_reason_codes
    exact_center base physical numerical physical_N_error_Nm physical_J_error_m numerical_N_error_Nm
    numerical_J_error_m numerical_s_error_m'''.split())
PROOF_LIMITS=(Q(100),Q(1,10000),Q(1,100),Q(1,10**7),Q(1,10**7))
KINDS={'prediction':POINT_COLUMNS,'comparison':EVALUATION_COLUMNS,'display_only_comparison':EVALUATION_COLUMNS,
    'display_only_fills':FILL_COLUMNS,'proof':PROOF_COLUMNS}
POINT_STATUSES=frozenset('''valid evidence_refused budget_refused motion_unreliable unobservable outside_model_domain
    input_gap'''.split())


def refuse(code='result_chunk_invalid'):raise InversionError(code)


def rational(value,*,nullable=False,nonnegative=False):
    if value is None and nullable:return None
    if type(value) is not str or not 1<=len(value)<=2500 or not re.fullmatch(r'-?(?:0|[1-9]\d*)(?:/[1-9]\d*)?',value):refuse()
    try:q=Q(value)
    except (ValueError,ZeroDivisionError):refuse()
    if str(q)!=value or max(abs(q.numerator).bit_length(),q.denominator.bit_length())>4096 or nonnegative and q<0:refuse()
    return q


def scalar(value,*,nullable=False):
    if value is None and nullable:return None
    if type(value) is not str or not 1<=len(value)<=2500:refuse()
    if '/' in value:return rational(value)
    try:q=Decimal(value)
    except InvalidOperation:refuse()
    if not q.is_finite():refuse()
    return q


def reasons(value):
    if (type(value) is not list or len(value)>32 or len(set(value))!=len(value)
            or any(type(s) is not str or not re.fullmatch('[a-z][a-z0-9_]{0,127}',s) for s in value)):refuse()


def indices(value,maximum):
    if type(value) is not list or len(value)>maximum:refuse()
    result=[rational(v,nonnegative=True) for v in value]
    if any(v.denominator!=1 for v in result) or any(a>=b for a,b in zip(result,result[1:])):refuse()
    return result


def vector(value,size,*,nullable=False,nonnegative=False):
    if value is None and nullable:return None
    if type(value) is not list or len(value)!=size:refuse()
    return tuple(rational(v,nonnegative=nonnegative) for v in value)


@dataclass(frozen=True)
class PreparedResultV2(PreparedResult):
    def __post_init__(self):
        if type(self.snapshot) is not InputSnapshot or type(self.metadata) is not bytes or type(self.chunks) is not tuple:
            refuse('result_type_invalid')
        if any(type(c) is not bytes for c in self.chunks):refuse()
        if self.result_bytes>MAX_RESULT:refuse('result_size_limit')
        try:self._validate()
        except (KeyError,ValueError,TypeError,OverflowError,ZeroDivisionError) as error:
            raise InversionError('result_chunk_invalid') from error

    def _validate(self):
        body=json.loads(self.metadata)
        allowed=set('''schema input_sha256 cycle prediction_status prediction_reason_codes coverage_fraction point_count
            valid_point_count metadata comparison filled_comparison reference_sources review_state field_accuracy_certified
            external_evidence_admission_required source_kind input_applicability source_proof proof_columns parameter_core_sha256'''.split())
        if (set(body)!=allowed or canonical_bytes(body)!=self.metadata or body['schema']!='inversion.result/2'
                or self.snapshot.body.get('schema')!='inversion.input/2' or body['input_sha256']!=self.snapshot.content_sha256
                or body['source_kind']!='synthetic' or body['field_accuracy_certified'] is not False
                or body['external_evidence_admission_required'] is not True or body['review_state']!='unreviewed'
                or body['proof_columns']!=list(PROOF_COLUMNS)
                or body['parameter_core_sha256']!=self.snapshot.body['parameter_core_sha256']):refuse('result_identity_invalid')
        from .input_contracts_v2 import parse_prediction_evidence,check_evidence_binding
        from .parameter_contracts_v2 import parse_parameter_set_v2
        parameters=parse_parameter_set_v2(canonical_bytes(self.snapshot.body['applicability']['parameter_body']))
        native=parse_prediction_evidence(canonical_bytes(self.snapshot.body['prediction_evidence']))
        check_evidence_binding(native,parameters)
        n=len(native.power.raw)
        if type(body['point_count']) is not int or body['point_count']!=n:refuse('result_point_count_invalid')
        groups={k:[] for k in KINDS};last_kind=-1
        for raw in self.chunks:
            block=json.loads(raw);kind=block['kind']
            if (set(block)!= {'kind','columns','rows'} or canonical_bytes(block)!=raw or kind not in KINDS
                    or block['columns']!=list(KINDS[kind]) or type(block['rows']) is not list
                    or not 1<=len(block['rows'])<=256):refuse()
            ordinal=list(KINDS).index(kind)
            if ordinal<last_kind:refuse()
            last_kind=ordinal
            for row in block['rows']:
                if type(row) is not list or len(row)!=len(KINDS[kind]):refuse()
                groups[kind].append(dict(zip(KINDS[kind],row)))
            if len(groups[kind])>32768:refuse('result_size_limit')
        if len(groups['prediction'])!=n or len(groups['proof'])!=n or len(groups['comparison'])!=n:refuse('result_point_count_invalid')
        source=body['source_proof']
        if set(source)!=set('''status reason_codes case_id config_sha256 evidence_sha256 derivative_bounds angle_error_rad
            differential_time_s common_time_s power_error_W derivative_support_ns'''.split()):refuse()
        if source['status'] not in ('verified','refused'):refuse()
        reasons(source['reason_codes'])
        if source['status']=='verified':
            if source['reason_codes'] or source['case_id']!=native.case_id or source['config_sha256']!=native.config_sha256:refuse()
            vector(source['derivative_bounds'],4,nonnegative=True)
            vector(source['derivative_support_ns'],2)
            vector(source['angle_error_rad'],len(native.angle.raw),nonnegative=True)
            vector(source['power_error_W'],n,nonnegative=True)
            vector(source['differential_time_s'],len(native.angle.raw),nonnegative=True)
            rational(source['common_time_s'],nonnegative=True)
            expected_digest=sha256(canonical_bytes(dict(power=native.power.channel_sha256,angle=native.angle.channel_sha256,
                contract=parameters.coverage_contract.raw_sha256,checker=parameters.coverage_contract.body['binding']['checker_sha256']))).hexdigest()
            if source['evidence_sha256']!=expected_digest:refuse()
        if type(source['evidence_sha256']) is not str or not re.fullmatch('[0-9a-f]{64}',source['evidence_sha256']):refuse()
        valid=0;duration=Q(0);previous=None
        for i,(p,a) in enumerate(zip(groups['prediction'],groups['proof'])):
            if (p['power_index']!=str(i) or a['power_index']!=str(i)
                    or p['time_ns']!=str(native.power.time_ns[i]) or a['time_ns']!=p['time_ns']):refuse()
            if p['status'] not in POINT_STATUSES or a['status'] not in ('valid','refused') or a['motion_status'] not in ('valid','refused'):refuse()
            for item in (p['reason_codes'],a['reason_codes'],a['motion_reason_codes']):reasons(item)
            for k in ('s_m','load_N','error_bound_N'):scalar(p[k],nullable=True)
            for k in INTERMEDIATES:
                if k=='angle_indices':
                    if p[k] is not None:indices(p[k],7)
                else:scalar(p[k],nullable=True)
            selected=indices(a['angle_indices'],7)
            if a['angle_indices']!=(p['angle_indices'] or []) or any(i>=len(native.angle.raw) for i in selected):refuse()
            for k in ('exact_center','base','physical','numerical'):vector(a[k],3,nullable=True,nonnegative=k!='exact_center')
            budget=[rational(a[k],nullable=True,nonnegative=True) for k in PROOF_COLUMNS[-5:]]
            if a['motion_status']=='valid':
                if (len(selected)!=7 or any(b!=c+1 for c,b in zip(selected,selected[1:]))
                        or any(a[k] is None for k in ('exact_center','base','physical','numerical')) or a['motion_reason_codes']):refuse()
                if Q(a['base'][1])>Q(1,100000):refuse()
            if a['status']=='valid':
                if (a['motion_status']!='valid' or a['reason_codes'] or any(v is None or v>limit for v,limit in zip(budget,PROOF_LIMITS))):refuse()
            if p['status']=='valid':
                if (a['status']!='valid' or p['reason_codes'] or any(p[k] is None for k in ('load_N','s_m','error_bound_N'))
                        or not 0<=scalar(p['error_bound_N'])<=1000 or source['status']!='verified'):refuse()
                valid+=1
                if previous is not None and previous['status']=='valid':duration+=native.power.time_ns[i]-native.power.time_ns[i-1]
            elif p['load_N'] is not None or not p['reason_codes']:refuse()
            previous=p
        cycle=body['cycle']
        if set(cycle)!= {'start_ns','end_ns','physical_cycle_id'}:refuse()
        expected_cycle=parameters.coverage_contract.body['cycle']
        if any(str(cycle[k])!=str(expected_cycle[k]) for k in ('start_ns','end_ns','physical_cycle_id')):refuse()
        span=rational(cycle['end_ns'])-rational(cycle['start_ns'])
        if span<=0 or rational(body['coverage_fraction'])!=duration/span or type(body['valid_point_count']) is not int or body['valid_point_count']!=valid:refuse()
        reasons(body['prediction_reason_codes'])
        if body['prediction_status'] not in ('applicable','not_applicable'):refuse()
        if (body['prediction_status']=='applicable')!=(not body['prediction_reason_codes'] and valid>0 and duration/span>=Q(4,5) and source['status']=='verified'):refuse()
        # Existing comparison blocks remain separate; edge predecessors are
        # verified across chunk boundaries and must reference native centers.
        sources=body['reference_sources']
        if type(sources) is not list or not 1<=len(sources)<=4:refuse()
        from .series import ChannelPin
        for pin in sources:
            # Result source-table indices/review versions are canonical strings
            # (as in v1); the input pin contract requires actual integers.
            converted=json.loads(canonical_bytes(pin))
            for key in ('first_index','last_index'):
                number=rational(converted[key],nonnegative=True)
                if number.denominator!=1:refuse()
                converted[key]=int(number)
            for key in ('calibration_pin','sampling_pin','sync_pin'):
                if converted.get(key) is not None:
                    number=rational(converted[key]['review_version'],nonnegative=True)
                    if number.denominator!=1:refuse()
                    converted[key]['review_version']=int(number)
            ChannelPin.from_wire(converted)
        for kind in ('comparison','display_only_comparison'):
            previous=None
            for row in groups[kind]:
                index=rational(row['power_index'],nonnegative=True)
                if index.denominator!=1 or index>=n or row['time_ns']!=str(native.power.time_ns[int(index)]):refuse()
                if previous is not None and index<=Q(previous['power_index']):refuse()
                if type(row['valid']) is not bool or type(row['reference_edge_from_previous']) is not bool:refuse()
                if row['reference_edge_from_previous'] and (previous is None or index!=Q(previous['power_index'])+1):refuse()
                reasons(row['reason_codes'])
                for role in ('load','displacement'):
                    scalar(row[role+'_value_si'],nullable=True)
                    bracket=row[role+'_indices']
                    if type(bracket) is not list or len(bracket) not in (0,2):refuse()
                    weights=vector(row[role+'_weights'],len(bracket))
                    if bracket:
                        left,right=(rational(v,nonnegative=True) for v in bracket)
                        if left.denominator!=1 or right.denominator!=1 or left>right or sum(weights)!=1 or min(weights)<0:refuse()
                        if left==right and weights!=(Q(1),Q(0)):refuse()
                    source_index=row[role+'_source']
                    if type(source_index) is not int or not 0<=source_index<len(sources):refuse()
                previous=row
        previous=-1
        for row in groups['display_only_fills']:
            index=rational(row['power_index'],nonnegative=True)
            if index.denominator!=1 or not previous<index<n or row['time_ns']!=str(native.power.time_ns[int(index)]):refuse()
            previous=index;scalar(row['s_m']);scalar(row['load_N']);reasons(row['original_reason_codes'])
            indices(row['source_indices'],2);vector(row['weights'],2);vector(row['source_times_ns'],2)
            if row['periodic_assumption'] is not True or type(row['method']) is not str:refuse()


def encode_v2(value,comparison,snapshot):
    from .solver_v2 import PredictionV2
    from .comparison import ComparisonReport
    from .parameter_contracts_v2 import parse_parameter_set_v2
    from .display_fill import fill_deadpoints
    if type(value) is not PredictionV2 or type(comparison) is not ComparisonReport:refuse('result_type_invalid')
    parameters=parse_parameter_set_v2(canonical_bytes(snapshot.body['applicability']['parameter_body']))
    prediction=value.prediction;sources=[];source_ids={};chunks=[];size=0
    def blocks(kind,columns,rows):
        nonlocal size
        pending=[]
        def append():
            nonlocal size
            raw=canonical_bytes(dict(kind=kind,columns=columns,rows=pending));size+=len(raw)
            if size>MAX_RESULT:refuse('result_size_limit')
            chunks.append(raw)
        for row in rows:
            pending.append(row)
            if len(pending)==256:append();pending=[]
        if pending:append()
    def source(pin):
        body=exact(pin);key=canonical_bytes(body)
        if key not in source_ids:source_ids[key]=len(sources);sources.append(body)
        return source_ids[key]
    def evaluations(report):
        for e in report.evaluations:
            row=[str(e.time_ns),str(e.power_index),e.valid,list(e.reason_codes)]
            for ref in (e.load,e.displacement):row.extend((exact(ref.value_si),exact(ref.indices),exact(ref.weights),source(ref.source),ref.reason))
            yield row+[e.reference_edge_from_previous]
    def summary(report):
        return None if report is None else {f.name:exact(getattr(report,f.name)) for f in fields(report) if f.name not in ('evaluations','filled_comparison')}
    blocks('prediction',POINT_COLUMNS,([exact(getattr(p,k)) for k in POINT_COLUMNS[:7]]+[exact(p.intermediates.get(k)) for k in INTERMEDIATES] for p in prediction.points))
    blocks('comparison',EVALUATION_COLUMNS,evaluations(comparison))
    if comparison.filled_comparison:blocks('display_only_comparison',EVALUATION_COLUMNS,evaluations(comparison.filled_comparison))
    blocks('display_only_fills',FILL_COLUMNS,([exact(getattr(p,k)) for k in FILL_COLUMNS] for p in fill_deadpoints(prediction,parameters.policy).fills))
    blocks('proof',PROOF_COLUMNS,([str(a.motion.time_ns),str(a.power_index),a.status,list(a.reason_codes),exact(a.motion.angle_indices),
        a.motion.status,list(a.motion.reason_codes),exact(a.motion.exact_center),exact(a.motion.base),exact(a.motion.physical),exact(a.motion.numerical)]
        +[exact(getattr(a,k)) for k in PROOF_COLUMNS[-5:]] for a in value.evidence))
    proof=value.source_proof
    public_proof={key:exact(getattr(proof,key)) for key in ('status','reason_codes','evidence_sha256','derivative_bounds','angle_error_rad',
        'differential_time_s','common_time_s','power_error_W','derivative_support_ns')}
    public_proof.update(case_id=None if proof.case is None else proof.case.case_id,config_sha256=None if proof.case is None else proof.case.config_sha256)
    body=dict(schema='inversion.result/2',input_sha256=snapshot.content_sha256,cycle=exact(prediction.cycle),
        prediction_status=prediction.status,prediction_reason_codes=list(prediction.reason_codes),coverage_fraction=str(prediction.coverage_fraction),
        point_count=len(prediction.points),valid_point_count=sum(p.status=='valid' for p in prediction.points),metadata=exact(prediction.metadata),
        comparison=summary(comparison),filled_comparison=summary(comparison.filled_comparison),reference_sources=sources,review_state='unreviewed',
        field_accuracy_certified=False,external_evidence_admission_required=True,source_kind='synthetic',
        input_applicability=exact({k:v for k,v in snapshot.body['applicability'].items() if k in ('independent_comparison','independence_reasons','field_accuracy_certified')}),
        source_proof=public_proof,proof_columns=PROOF_COLUMNS,parameter_core_sha256=parameters.parameter_core_sha256)
    return PreparedResultV2(snapshot,canonical_bytes(body),tuple(chunks))
