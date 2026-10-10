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
from .parameter_contracts import parse_parameter_set
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
    parameters=parse_parameter_set(canonical_bytes(body['applicability']['parameter_body']))
    def series(role):
        records=body['samples'][role]
        if not 1<=len(records)<=32768: raise InversionError('selection_sample_limit')
        return SampleSeries(role,tuple(Sample(Fraction(s['time_ns']),int(s['index']),
            None if s['value_si'] is None else Fraction(s['value_si']),s['quality']) for s in records),
            ChannelPin.from_wire(body['channel_pins'][role]))
    cycle=value['cycle']
    prediction=PredictionInputs(series('active_power'),series('crank_angle'),
        CycleWindow(Fraction(cycle['start_ns']),Fraction(cycle['end_ns']),cycle['physical_cycle_id']),
        value['power_window_ns'],Fraction(body['observation']['relative_alignment_ns']))
    return prediction,ReferenceInputs(series('load'),series('displacement')),parameters,snapshot

def compute(input_path:Path,output_path:Path)->None:
    from .solver import predict
    from .comparison import compare
    from .result_codec import encode,write
    inputs,reference,parameters,snapshot=load_input(input_path)
    prediction=predict(inputs,parameters,parameters.policy)
    comparison=compare(prediction,reference,parameters.policy)
    write(encode(prediction,comparison,snapshot),output_path)

if __name__=='__main__':
    import sys
    if len(sys.argv)!=3: raise SystemExit(2)
    compute(Path(sys.argv[1]),Path(sys.argv[2]))
