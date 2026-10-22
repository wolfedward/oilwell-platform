"""Credential-free numerical executable. Only supervisor-created local JSON paths.

This is an internal server transport, not a client-snapshot admission endpoint.
No pickle, dynamic type tags, supplied code, URLs or database configuration.
"""
from fractions import Fraction
from hashlib import sha256
import json
from pathlib import Path
from .contracts import canonical_bytes
from .input_contracts import InputSnapshot
from .version_dispatch import parse_parameters as parse_parameter_set
from .series import ChannelPin,Sample,SampleSeries,CycleWindow,PredictionInputs,ReferenceInputs
from .transactions import InversionError

def write_input(prepared,path,*,request_bytes):
    value=dict(snapshot=json.loads(prepared.snapshot.canonical),cycle=dict(start_ns=str(prepared.prediction_inputs.cycle.start_ns),
        end_ns=str(prepared.prediction_inputs.cycle.end_ns),physical_cycle_id=prepared.prediction_inputs.cycle.physical_cycle_id),
        power_window_ns=prepared.prediction_inputs.power_window_ns)
    raw=canonical_bytes(value)
    if max(len(raw),request_bytes+len(prepared.snapshot.canonical))>64*1024**2: raise InversionError('input_size_limit')
    with path.open('xb') as target: target.write(raw)

def load_input(path):
    if path.stat().st_size>64*1024**2: raise InversionError('input_size_limit')
    with path.open('rb') as source: value=json.load(source)
    if set(value)!= {'snapshot','cycle','power_window_ns'}: raise InversionError('input_transport_invalid')
    body=value['snapshot']; raw=canonical_bytes(body); snapshot=InputSnapshot(body,raw,sha256(raw).hexdigest())
    if body.get('schema') not in ('inversion.input/1','inversion.input/2'):raise InversionError('input_schema_unknown')
    parameters=parse_parameter_set(canonical_bytes(body['applicability']['parameter_body']))
    v2=body['schema']=='inversion.input/2'
    if (parameters.schema=='inversion.parameter-set/2')!=v2:raise InversionError('input_schema_mismatch')
    if v2:
        from .models import INSTALLED_MODELS
        model=body['applicability']['model_body'];artifact=model['artifact']
        if (artifact.get('installed_id')!='beam_energy_balance/2' or artifact.get('algorithm')!='beam_energy_balance/2'
                or artifact.get('parameter_schema')!=parameters.schema
                or canonical_bytes(artifact)!=canonical_bytes(INSTALLED_MODELS['beam_energy_balance/2'].snapshot())
                or sha256(canonical_bytes(model)).hexdigest()!=body['request']['model_sha256']
                or parameters.raw_sha256!=body['request']['parameter_sha256']):raise InversionError('artifact_changed')
    def series(role):
        records=body['samples'][role]
        if not 1<=len(records)<=32768: raise InversionError('selection_sample_limit')
        return SampleSeries(role,tuple(Sample(Fraction(s['time_ns']),int(s['index']),
            None if s['value_si'] is None else Fraction(s['value_si']),s['quality']) for s in records),
            ChannelPin.from_wire(body['channel_pins'][role]))
    cycle=value['cycle']
    prediction=PredictionInputs(series('active_power'),series('crank_angle'),
        CycleWindow(Fraction(cycle['start_ns']),Fraction(cycle['end_ns']),cycle['physical_cycle_id']),
        value['power_window_ns'],(Fraction(parameters.coverage_contract.body['time']['common_offset_bound_ns'])+
            Fraction(parameters.coverage_contract.body['time']['differential_bound_ns']) if v2 else Fraction(body['observation']['relative_alignment_ns'])))
    reference=ReferenceInputs(series('load'),series('displacement'))
    if v2:
        from .input_contracts_v2 import PreparedInputV2,parse_prediction_evidence
        PreparedInputV2(prediction,reference,parameters,parameters.policy,snapshot,
            parse_prediction_evidence(canonical_bytes(body['prediction_evidence'])))
    return prediction,reference,parameters,snapshot

def compute(input_path:Path,output_path:Path)->None:
    from .solver import predict
    from .comparison import compare
    from .result_codec import encode
    from .result_dispatch import write_result
    inputs,reference,parameters,snapshot=load_input(input_path)
    if parameters.schema=='inversion.parameter-set/2':
        from .input_contracts_v2 import parse_prediction_evidence
        from .solver_v2 import predict_v2
        from .result_codec_v2 import encode_v2
        value=predict_v2(inputs,parameters,parse_prediction_evidence(canonical_bytes(snapshot.body['prediction_evidence'])))
        result=encode_v2(value,compare(value.prediction,reference,parameters.policy),snapshot)
    else:
        prediction=predict(inputs,parameters,parameters.policy)
        result=encode(prediction,compare(prediction,reference,parameters.policy),snapshot)
    write_result(result,output_path)

if __name__=='__main__':
    import sys
    if len(sys.argv)!=3: raise SystemExit(2)
    compute(Path(sys.argv[1]),Path(sys.argv[2]))
