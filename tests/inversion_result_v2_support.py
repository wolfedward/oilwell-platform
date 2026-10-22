"""Pure internal transport fixture: generated, never an approval or DB fixture."""
from dataclasses import asdict
from functools import lru_cache
from hashlib import sha256
import json

from backend.inversion.contracts import canonical_bytes
from backend.inversion.input_contracts import InputSnapshot
from backend.inversion.input_contracts_v2 import PreparedInputV2,prediction_evidence_bytes
from backend.inversion.parameter_contracts_v2 import parse_parameter_set_v2
from backend.inversion.models import INSTALLED_MODELS
from backend.inversion.result_codec import exact
from backend.inversion.coverage_contracts import pin_wire
from tests.inversion_v2_support import registered_fixture,prediction_fixture


@lru_cache(maxsize=1)
def prepared_input():
    body,evidence=registered_fixture();parameters=parse_parameter_set_v2(canonical_bytes(body))
    prediction,reference=prediction_fixture(evidence,parameters)
    model={'artifact':INSTALLED_MODELS['beam_energy_balance/2'].snapshot(),'registrar':{'domain':'test','user_id':'author'}}
    channels=(prediction.power,prediction.crank_angle,reference.load,reference.displacement)
    snapshot_body=dict(schema='inversion.input/2',purpose='software_pair_test',
        parameter_core_sha256=parameters.parameter_core_sha256,prediction_evidence=json.loads(prediction_evidence_bytes(evidence)),
        applicability=dict(parameter_body=body,model_body=model,independent_comparison=False,
            independence_reasons=['calibration_cycle_overlap'],field_accuracy_certified=False),
        request=dict(parameter_sha256=parameters.raw_sha256,model_sha256=sha256(canonical_bytes(model)).hexdigest()),
        observation=dict(relative_alignment_ns='0'),
        channel_pins={c.role:pin_wire(c.source) for c in channels},
        samples={c.role:[dict(time_ns=str(s.time_ns),index=str(s.index),value_si=None if s.value_si is None else str(s.value_si),quality=s.quality) for s in c.samples] for c in channels})
    raw=canonical_bytes(snapshot_body);snapshot=InputSnapshot(snapshot_body,raw,sha256(raw).hexdigest())
    return PreparedInputV2(prediction,reference,parameters,parameters.policy,snapshot,evidence)


@lru_cache(maxsize=1)
def calculated():
    from backend.inversion.solver_v2 import predict_v2
    from backend.inversion.comparison import compare
    item=prepared_input();value=predict_v2(item.prediction_inputs,item.parameters,item.prediction_evidence)
    return value,compare(value.prediction,item.reference_inputs,item.policy),item.snapshot


def rows(prepared,kind):
    return [dict(zip(block['columns'],row)) for raw in prepared.chunks
        if (block:=json.loads(raw))['kind']==kind for row in block['rows']]
