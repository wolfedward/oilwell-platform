"""Finite-input numeric diagnostics, never service approval or field accuracy."""
from collections import Counter
from fractions import Fraction as Q
from time import perf_counter

from backend.inversion.coverage_sources import inputs_from_evidence
from backend.inversion.solver_v2 import predict_v2


def report_prediction(result,elapsed_s):
    prediction=result.prediction
    names=('physical_N_error_Nm','physical_J_error_m','numerical_N_error_Nm','numerical_J_error_m','numerical_s_error_m')
    points=[]
    for actual,proof in zip(prediction.points,result.evidence):
        bounds=proof.motion
        points.append({'power_index':actual.power_index,'time_ns':str(actual.time_ns),'status':actual.status,
            'reason_codes':list(actual.reason_codes),'admission_status':proof.status,
            'angle_indices':list(bounds.angle_indices),'motion_status':bounds.status,
            **{name:str(getattr(proof,name)) if getattr(proof,name) is not None else None for name in names},
            **{name:[str(v) for v in getattr(bounds,name)] if getattr(bounds,name) is not None else None for name in ('base','physical','numerical')},
            'load_N':actual.load_N,'load_error_bound_N':actual.error_bound_N})
    maxima={name:str(max(values)) if (values:=[getattr(p,name) for p in result.evidence if getattr(p,name) is not None]) else None for name in names}
    return {'schema':'owp.testpack.finite-budget/2','scope':'finite_synthetic_input/1','source_kind':'synthetic',
        'purpose':'software_pair_test','field_accuracy_certified':False,'validation_status':'not_field_validated',
        'approved_execution_status':'not_run','passed':prediction.status=='applicable',
        'prediction_status':prediction.status,'reasons':list(prediction.reason_codes),
        'source_proof_status':result.source_proof.status,'evidence_sha256':result.source_proof.evidence_sha256,
        'original_prediction_coverage':str(prediction.coverage_fraction),'native_points':len(points),
        'classification_counts':dict(Counter(p.status for p in prediction.points)),
        'maximum_errors':maxima,'elapsed_s':elapsed_s,'points':points}


def inspect_v2(evidence,parameters):
    start=perf_counter()
    result=predict_v2(inputs_from_evidence(evidence,parameters),parameters,evidence)
    return report_prediction(result,perf_counter()-start)
