"""Re-read exact v2 package members and report offline assertions, not approvals."""
from time import perf_counter

from backend.inversion.comparison import compare
from backend.inversion.coverage_sources import inputs_from_evidence
from backend.inversion.solver_v2 import predict_v2
from .budget_v2 import report_prediction
from .contracts import CheckResult
from .inversion_binding_v2 import offline_binding
from .storage import load_pack


def fault_assertion(case_id,result,evidence):
    points=result.prediction.points
    refused=[p for p in points[1:-1] if p.status!='valid']
    passed=(result.source_proof.status=='verified' and len(points)==1025 and bool(refused)
        and all(p.load_N is None and p.reason_codes for p in refused))
    if case_id in ('cycle-noise','cycle-shifted'): passed=passed and result.prediction.status=='not_applicable'
    if case_id=='cycle-missing': passed=passed and all(p.load_N is None and 'invalid_power_quality' in p.reason_codes for p in points[400:420])
    reasons={reason for p in refused for reason in p.reason_codes}
    if case_id=='cycle-noise': passed=passed and 'motion_base_speed_budget_exceeded' in reasons
    if case_id=='cycle-shifted': passed=passed and 'physical_factor_budget_exceeded' in reasons
    if case_id=='cycle-clipped':
        clipped=[p for p,q in zip(points,evidence.power.quality) if q==2]
        passed=passed and bool(clipped) and all(p.load_N is None and 'invalid_power_quality' in p.reason_codes for p in clipped)
    return bool(passed)


def validate_pack_v2(pack):
    from .validate import validate_pack
    pack=load_pack(pack.root)
    if pack.manifest['configuration']['schema']!='owp.testpack.config/2':
        raise ValueError('v2 configuration required')
    checks=[check for check in validate_pack(pack) if check.check_id!='inversion-comparison']
    for record in pack.manifest['cases']:
        if record['facts'].get('data_kind')!='cycle': continue
        case_id=record['case_id'];start=perf_counter()
        binding=offline_binding(pack,case_id)
        result=predict_v2(inputs_from_evidence(binding.evidence,binding.parameters),binding.parameters,binding.evidence)
        report=report_prediction(result,perf_counter()-start)
        comparison=compare(result.prediction,binding.reference,binding.parameters.policy)
        joint=comparison.coverage['joint_fraction'].value;rmse=comparison.primary['load_rmse'].value
        report.update(joint_coverage=None if joint is None else str(joint),load_rmse_N=rmse,
            parameter_sha256=binding.parameters.raw_sha256,parameter_core_sha256=binding.parameters.parameter_core_sha256,
            original_file_sha256=binding.evidence.power.original_file_sha256,bound_file_sha256=binding.evidence.power.bound_file_sha256,
            independent_field_comparison=False)
        if record['category']=='mechanistic':
            expected={'prediction':'applicable','coverage_min':0.8,'joint_coverage_min':0.8,'load_rmse_max_N':500}
            passed=(report['passed'] and joint is not None and joint>=.8 and rmse is not None and rmse<=500)
        else:
            expected={'fault_handling':'preserve native rejection; no load at invalid points','approved_execution':'not_run'}
            passed=fault_assertion(case_id,result,binding.evidence)
        report['assertion_status']='passed' if passed else 'failed'
        checks.append(CheckResult(case_id+'-finite-inversion','numeric','passed' if passed else 'failed',
            expected,report,tuple(record['artifact_paths'])))
    return tuple(checks)


def validate_versioned(pack):
    if pack.manifest['configuration']['schema']=='owp.testpack.config/2': return validate_pack_v2(pack)
    if pack.manifest['configuration']['schema']=='owp.testpack.config/1':
        from .validate import validate_pack
        return validate_pack(pack)
    raise ValueError('unknown package configuration')
