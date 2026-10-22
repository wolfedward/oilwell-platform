"""Native-point v2 inversion: prove inputs/budgets before solving load."""
from dataclasses import dataclass,replace
from fractions import Fraction as Q
from types import MappingProxyType

from .coverage_contracts import PredictionEvidence
from .coverage_sources import SourceProof,verify_sources,verified,inputs_from_evidence
from .coverage_physics import AdmissionPoint,admit_point,nominal_state
from .motion import estimate_motion,_angle_runs
from .motion_bounds import MotionBounds,bound_window
from .parameter_contracts_v2 import ParameterSetV2
from .series import PredictionInputs,Sample
from .solver import PredictionPoint,PredictionSeries,_full_cycle,_MOTION_STATUS
from .losses import _upper


@dataclass(frozen=True)
class PredictionV2:
    prediction: PredictionSeries
    evidence: tuple[AdmissionPoint,...]
    source_proof: SourceProof


def predict_v2(inputs:PredictionInputs,parameters:ParameterSetV2,evidence:PredictionEvidence)->PredictionV2:
    if type(inputs) is not PredictionInputs or type(parameters) is not ParameterSetV2 or type(evidence) is not PredictionEvidence:
        raise ValueError('v2_prediction_contract_required')
    if parameters.is_draft or parameters.missing: raise ValueError('incomplete_parameters')
    if inputs!=inputs_from_evidence(evidence,parameters): raise ValueError('coverage_input_mismatch')
    proof=verify_sources(evidence,parameters)
    motions=estimate_motion(inputs,parameters,parameters.policy) if verified(proof) else ()
    full_cycle,cycle_detail=_full_cycle(inputs,parameters,parameters.policy) if verified(proof) else (False,None)
    mapped={}
    if verified(proof):
        inputs=proof._inputs  # Equal, immutable input; cheap identity checks in per-point admission.
        for samples,angles,reason in _angle_runs(inputs,parameters):
            if reason is None:
                mapped.update((sample.index,replace(sample,value_si=angle)) for sample,angle in zip(samples,angles))
    points=[]; admissions=[]
    uN=Q('100.01'); uJ=Q('0.0001001')
    for offset,sample in enumerate(inputs.power.samples):
        diagnostic={}; displacement=None; load=None; error=None
        if not verified(proof):
            bounds=MotionBounds(sample.index,sample.time_ns,(),'refused',proof.reason_codes,None,None,None,None)
            admission=AdmissionPoint(sample.index,'refused',proof.reason_codes,bounds,None,None,None,None,None)
            status='evidence_refused'; reasons=proof.reason_codes
        else:
            moving=motions[offset]; indices=moving.angle_indices
            selected=tuple(mapped[i] for i in indices) if all(i in mapped for i in indices) else ()
            bounds=bound_window(power_index=sample.index,center_ns=sample.time_ns,samples=selected,nominal=moving,
                angle_error_rad=tuple(proof.angle_error_rad[i] for i in indices),
                differential_time_s=tuple(proof.differential_time_s[i] for i in indices),
                common_time_s=proof.common_time_s,derivative_bounds=proof.derivative_bounds,
                derivative_support_ns=proof.derivative_support_ns)
            admission=admit_point(inputs,parameters,moving,bounds,proof)
            diagnostic={key:getattr(moving,key) for key in ('theta_rad','omega_rad_s','alpha_rad_s2',
                'angle_indices','window_span_ns','residual_rad','condition')}
            if not full_cycle:
                status='motion_unreliable'; reasons=('full_cycle_unconfirmed',)
            elif admission.status!='valid':
                status=_MOTION_STATUS.get(moving.status,'budget_refused'); reasons=admission.reason_codes
            else:
                nominal=nominal_state(inputs,parameters,moving)
                diagnostic.update(nominal.terms)
                displacement=float(nominal.s)
                if abs(nominal.J)<=uJ:
                    status='unobservable'; reasons=('factor_uncertain',)
                else:
                    exact_load=nominal.N/nominal.J; candidate=float(exact_load)
                    round_error=abs(Q.from_float(candidate)-exact_load)*abs(nominal.J)
                    round_error+=abs(nominal.N-Q.from_float(float(nominal.N)))
                    numerical=admission.numerical_N_error_Nm+round_error
                    admission=replace(admission,numerical_N_error_Nm=numerical)
                    if numerical>Q('0.01'):
                        admission=replace(admission,status='refused',reason_codes=('numerical_torque_budget_exceeded',))
                        status='budget_refused'; reasons=admission.reason_codes
                    elif not Q(parameters.domain.load_N.min)<=Q.from_float(candidate)<=Q(parameters.domain.load_N.max):
                        status='outside_model_domain'; reasons=('load_outside_domain',)
                    else:
                        bound=(uN+abs(Q.from_float(candidate))*uJ)/(abs(nominal.J)-uJ)
                        error=_upper(bound)
                        if Q.from_float(error)>Q(parameters.policy.max_load_error_N):
                            status='unobservable'; reasons=('load_error_exceeded',)
                        else:
                            load=candidate; status='valid'; reasons=()
                    diagnostic.update(numerical_torque_error_Nm=_upper(numerical),
                        torque_bound_Nm=_upper(uN),factor_bound_m=_upper(uJ))
        admissions.append(admission)
        points.append(PredictionPoint(sample.time_ns,sample.index,displacement,load,error,status,tuple(reasons),MappingProxyType(diagnostic)))
    duration=sum((b.time_ns-a.time_ns for a,b in zip(points,points[1:])
        if a.status==b.status=='valid' and b.power_index==a.power_index+1),Q(0))
    coverage=duration/(inputs.cycle.end_ns-inputs.cycle.start_ns)
    reasons=list(proof.reason_codes) if not verified(proof) else []
    if verified(proof) and not full_cycle: reasons.append('full_cycle_unconfirmed')
    if not any(p.status=='valid' for p in points): reasons.append('no_valid_points')
    if duration<=0 or coverage<Q(4,5): reasons.append('insufficient_coverage')
    metadata=MappingProxyType(dict(algorithm=parameters.policy.algorithm,model_version=parameters.policy.model_version,
        parameter_sha256=parameters.raw_sha256,policy_sha256=parameters.policy.raw_sha256,
        applicability_scope='finite_synthetic_input/1',source_kind='synthetic',purpose='software_pair_test',
        field_accuracy_certified=False,validation_status='not_field_validated',external_evidence_admission_required=True,
        coverage_evidence_sha256=proof.evidence_sha256,source_proof_status=proof.status,full_cycle=cycle_detail,
        numerical_assumptions=('Native QR and binary64 geometry/energy retained; checked against independent exact/Arb enclosures',
            'Fixed physical and numerical budgets; no reference-channel admission; original adjacent support only')))
    prediction=PredictionSeries(tuple(points),inputs.cycle,'not_applicable' if reasons else 'applicable',tuple(dict.fromkeys(reasons)),coverage,metadata)
    return PredictionV2(prediction,tuple(admissions),proof)
