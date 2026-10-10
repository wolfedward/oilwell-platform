"""Lossless fixed-column result blocks; only bounded JSON, never pickle.

Integer indices, rationals and floating numerical outputs use canonical strings.
Floats use Python's shortest round-tripping finite decimal representation. Source
pins are deduplicated; original comparison and display-only fills stay separate.
"""
from collections.abc import Mapping
from dataclasses import dataclass, fields, is_dataclass
from decimal import Decimal
from fractions import Fraction
from hashlib import sha256
import json
import math
import struct
from uuid import UUID
from .contracts import canonical_bytes, freeze
from .input_contracts import InputSnapshot
from .transactions import InversionError

MAX_RESULT=16*1024**2
INTERMEDIATES=tuple('theta_rad omega_rad_s alpha_rad_s2 angle_indices window_span_ns residual_rad condition speed_error_bound_rad_s theta_error_estimate_rad power_W motor_speed_rad_s loss_W loss_error_W loss_numerical_error_W drive_Nm J_m torque_bound_Nm factor_bound_m N_Nm friction_Nm U_prime_Nm M_kg_m2 M_prime_kg_m2 nominal_load_N numerical_torque_error_Nm geometry_factor_error_m geometry_displacement_error_m energy_numerical_torque_error_Nm'.split())
POINT_COLUMNS=('time_ns','power_index','s_m','load_N','error_bound_N','status','reason_codes')+INTERMEDIATES
LEGACY_EVALUATION_COLUMNS=('time_ns','power_index','valid','reason_codes','load_value_si','load_indices','load_weights','load_source','load_reason','displacement_value_si','displacement_indices','displacement_weights','displacement_source','displacement_reason')
EVALUATION_COLUMNS=LEGACY_EVALUATION_COLUMNS+('reference_edge_from_previous',)
FILL_COLUMNS=tuple('time_ns power_index s_m load_N original_reason_codes source_indices weights source_times_ns periodic_assumption method'.split())

def exact(value):
    if value is None or type(value) in (bool,str): return value
    if isinstance(value,(Fraction,Decimal,UUID)) or type(value) is int: return str(value)
    if type(value) is float:
        if not math.isfinite(value): raise InversionError('nonfinite_result')
        return repr(value)
    if is_dataclass(value): return {f.name:exact(getattr(value,f.name)) for f in fields(value)}
    if isinstance(value,Mapping): return {k:exact(v) for k,v in value.items()}
    if isinstance(value,(tuple,list)): return [exact(v) for v in value]
    raise InversionError('result_type_invalid')

@dataclass(frozen=True)
class PreparedResult:
    snapshot: InputSnapshot
    metadata: bytes
    chunks: tuple[bytes,...]

    def __post_init__(self):
        if type(self.snapshot) is not InputSnapshot or type(self.metadata) is not bytes or type(self.chunks) is not tuple:
            raise ValueError('Immutable canonical result required')
        if self.result_bytes>MAX_RESULT: raise InversionError('result_size_limit')
        body=json.loads(self.metadata)
        if (canonical_bytes(body)!=self.metadata or body['schema']!='inversion.result/1'
            or body['input_sha256']!=self.snapshot.content_sha256
            or body['prediction_status'] not in ('applicable','not_applicable')):
            raise InversionError('result_identity_invalid')
        shapes={'prediction':POINT_COLUMNS,'comparison':EVALUATION_COLUMNS,
                'display_only_comparison':EVALUATION_COLUMNS,'display_only_fills':FILL_COLUMNS}
        counts={name:0 for name in shapes}
        comparison_shapes={}
        previous_evaluation={}
        for chunk in self.chunks:
            if type(chunk) is not bytes: raise InversionError('result_chunk_invalid')
            block=json.loads(chunk); kind=block.get('kind')
            columns=block.get('columns')
            comparison_kind=kind in ('comparison','display_only_comparison')
            accepted=(list(EVALUATION_COLUMNS),list(LEGACY_EVALUATION_COLUMNS)) if comparison_kind else (list(shapes.get(kind,())),)
            if (canonical_bytes(block)!=chunk or set(block)!= {'kind','columns','rows'} or kind not in shapes
                or columns not in accepted or type(block['rows']) is not list
                or not 1<=len(block['rows'])<=256
                or any(type(row) is not list or len(row)!=len(columns) for row in block['rows'])):
                raise InversionError('result_chunk_invalid')
            if comparison_kind:
                if kind in comparison_shapes and comparison_shapes[kind]!=columns:
                    raise InversionError('result_chunk_invalid')
                comparison_shapes[kind]=columns
                for row in block['rows']:
                    if columns==list(EVALUATION_COLUMNS):
                        edge=row[-1]; previous=previous_evaluation.get(kind)
                        try:
                            valid_edge=(type(edge) is bool and (not edge or previous is not None
                                and int(row[1])==int(previous[1])+1 and Fraction(row[0])>Fraction(previous[0])))
                        except (ValueError,TypeError,ZeroDivisionError): valid_edge=False
                        if not valid_edge: raise InversionError('result_chunk_invalid')
                    previous_evaluation[kind]=row
            counts[kind]+=len(block['rows'])
            if counts[kind]>32768: raise InversionError('result_size_limit')
        if counts['prediction']!=body['point_count']: raise InversionError('result_point_count_invalid')

    @property
    def body(self): return json.loads(self.metadata)
    @property
    def result_bytes(self): return len(self.metadata)+sum(map(len,self.chunks))
    @property
    def status(self): return self.body['prediction_status']
    @property
    def content_sha256(self):
        return sha256(canonical_bytes(dict(metadata_sha256=sha256(self.metadata).hexdigest(),
            chunks=[sha256(c).hexdigest() for c in self.chunks]))).hexdigest()
    @property
    def diagnostics(self):
        body=self.body
        return dict(reasons=body['prediction_reason_codes'],coverage_fraction=body['coverage_fraction'],
                    point_count=body['point_count'],valid_point_count=body['valid_point_count'])

def encode(prediction,comparison,snapshot)->PreparedResult:
    from .display_fill import fill_deadpoints
    from .parameter_contracts import parse_parameter_set
    parameters=parse_parameter_set(canonical_bytes(snapshot.body['applicability']['parameter_body']))
    sources=[]; source_ids={}; chunks=[]; size=0
    def blocks(kind,columns,rows):
        nonlocal size
        pending=[]
        for row in rows:
            pending.append(row)
            if len(pending)==256:
                payload=canonical_bytes(dict(kind=kind,columns=columns,rows=pending)); size+=len(payload)
                if size>MAX_RESULT: raise InversionError('result_size_limit')
                chunks.append(payload); pending=[]
        if pending:
            payload=canonical_bytes(dict(kind=kind,columns=columns,rows=pending)); size+=len(payload)
            if size>MAX_RESULT: raise InversionError('result_size_limit')
            chunks.append(payload)
    def source(pin):
        body=exact(pin); key=canonical_bytes(body)
        if key not in source_ids: source_ids[key]=len(sources); sources.append(body)
        return source_ids[key]
    def evaluations(report):
        for e in report.evaluations:
            row=[str(e.time_ns),str(e.power_index),e.valid,list(e.reason_codes)]
            for ref in (e.load,e.displacement):
                row.extend((exact(ref.value_si),exact(ref.indices),exact(ref.weights),source(ref.source),ref.reason))
            row.append(e.reference_edge_from_previous)
            yield row
    def summary(report):
        if report is None: return None
        return {f.name:exact(getattr(report,f.name)) for f in fields(report) if f.name not in ('evaluations','filled_comparison')}
    blocks('prediction',POINT_COLUMNS,([exact(getattr(p,k)) for k in POINT_COLUMNS[:7]]+
        [exact(p.intermediates.get(k)) for k in INTERMEDIATES] for p in prediction.points))
    blocks('comparison',EVALUATION_COLUMNS,evaluations(comparison))
    if comparison.filled_comparison:
        blocks('display_only_comparison',EVALUATION_COLUMNS,evaluations(comparison.filled_comparison))
    fills=fill_deadpoints(prediction,parameters.policy).fills
    blocks('display_only_fills',FILL_COLUMNS,([exact(getattr(p,k)) for k in FILL_COLUMNS] for p in fills))
    body=dict(schema='inversion.result/1',input_sha256=snapshot.content_sha256,cycle=exact(prediction.cycle),
        prediction_status=prediction.status,prediction_reason_codes=list(prediction.reason_codes),
        coverage_fraction=str(prediction.coverage_fraction),point_count=len(prediction.points),
        valid_point_count=sum(p.status=='valid' for p in prediction.points),metadata=exact(prediction.metadata),
        comparison=summary(comparison),filled_comparison=summary(comparison.filled_comparison),
        reference_sources=sources,review_state='unreviewed',field_accuracy_certified=False,
        external_evidence_admission_required=True,source_kind=parameters.source_kind,
        input_applicability=exact({k:v for k,v in snapshot.body['applicability'].items()
            if k in ('independent_comparison','independence_reasons','field_accuracy_certified')}))
    return PreparedResult(snapshot,canonical_bytes(body),tuple(chunks))

def write(prepared,path):
    """A bounded framed file; framing bytes count against staging, not DB artifacts."""
    total=prepared.result_bytes+8*(1+len(prepared.chunks))
    if total>MAX_RESULT: raise InversionError('result_size_limit')
    with path.open('xb') as stream:
        for item in (prepared.metadata,*prepared.chunks): stream.write(struct.pack('>Q',len(item))); stream.write(item)

def read(path,snapshot):
    if path.stat().st_size>MAX_RESULT: raise InversionError('result_size_limit')
    items=[]
    with path.open('rb') as stream:
        while header:=stream.read(8):
            if len(header)!=8: raise InversionError('result_truncated')
            size=struct.unpack('>Q',header)[0]
            if size>MAX_RESULT: raise InversionError('result_size_limit')
            item=stream.read(size)
            if len(item)!=size: raise InversionError('result_truncated')
            items.append(item)
    if not items: raise InversionError('result_truncated')
    return PreparedResult(snapshot,items[0],tuple(items[1:]))
