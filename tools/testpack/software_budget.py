"""Independent rational motion check and a counterexample to the fixed domain.

No production predictor, geometry, motion fitter or reference-load samples enter
this check. Finite sequence diagnostics cannot certify the full declared domain.
"""
from fractions import Fraction as F
from functools import lru_cache
import math


@lru_cache(maxsize=7)
def _weights(offsets):
    design = [[F(j)**k for k in range(4)] for j in offsets]
    matrix = [[sum(row[k]*row[l] for row in design) for l in range(4)]
              + [F(k == l) for l in range(4)] for k in range(4)]
    for col in range(4):
        pivot = next(i for i in range(col,4) if matrix[i][col])
        matrix[col],matrix[pivot] = matrix[pivot],matrix[col]
        divisor = matrix[col][col]
        matrix[col] = [v/divisor for v in matrix[col]]
        for i in range(4):
            if i != col:
                factor = matrix[i][col]
                matrix[i] = [v-factor*p for v,p in zip(matrix[i],matrix[col])]
    inverse = [row[4:] for row in matrix]
    return tuple(tuple(sum(inverse[k][l]*row[l] for l in range(4)) for row in design) for k in range(4))


def inspect_budget(cycle):
    channel = next(c for c in cycle.channels if c.role == 'crank_angle')
    if cycle.sample_rate_hz != 128 or len(channel.raw) != 1025 or any(channel.quality):
        raise ValueError('budget oracle supports complete 128 Hz native angle only')
    angles = [F(v)*F(channel.scale)+F(channel.offset) for v in channel.raw]
    motion = cycle.model['motion']; rate = math.tau/motion['period_s']
    maximum = F(0); worst = None
    for index in range(1,1024):
        first = max(0,min(index-3,1018))
        offsets = tuple(i-index for i in range(first,first+7))
        weights = _weights(offsets)
        local = [angles[i]-angles[first] for i in range(first,first+7)]
        coeff = [sum(w*v for w,v in zip(row,local)) for row in weights]
        residual = max(abs(sum(coeff[k]*F(j)**k for k in range(4))-v) for j,v in zip(offsets,local))
        sensitivity = 128*sum(abs(w) for w in weights[1])
        true_speed = rate*(1+motion['epsilon']*math.cos(rate*index/128))
        remaining = abs(coeff[1]*128-F.from_float(true_speed))-residual*sensitivity
        if remaining > maximum:
            maximum = remaining
            worst = {'index':index,'offsets':list(offsets),'remaining_exact':str(remaining),
                'fit_speed_rad_s':float(coeff[1]*128),'truth_speed_rad_s':true_speed,
                'residual_rad':float(residual),'residual_speed_term_rad_s':float(residual*sensitivity)}
    # Necessary torque allowance at a declared independent-domain corner. Even
    # this one contribution exceeds 100; adding other errors cannot restore it.
    # An interior speed avoids relying on a solver's endpoint rejection rule.
    delta = F(1,100000); speed = F(11,100); power_minus_loss = F(251200)
    corner = power_minus_loss*delta/(speed*(speed-delta))
    reasons = ['declared_domain_torque_budget_exceeded'] if corner > 100 else []
    if maximum > delta:
        reasons.append('sampled_speed_budget_exceeded')
    if cycle.diagnostics['category'] == 'signal_fault':
        reasons.append('perturbed_signal_not_covered_by_baseline_budget')
    return {'case_id':cycle.case_id,'passed':not reasons,'reasons':reasons,
        'field_validation':False,'policy_fixed_before_prediction':True,
        'parameter_approval_allowed':False,'approved_http_inversion_status':'blocked',
        'domain_speed_torque_bound_Nm':float(corner),'domain_speed_torque_bound_exact':str(corner),
        'fixed_torque_bound_Nm':100,'fixed_speed_bound_rad_s':float(delta),
        'uncovered_speed_error_rad_s':float(maximum),'worst_motion_point':worst,
        'scope':'finite native software sequence plus necessary broad-domain counterexample; not full certification',
        'method':'exact rational seven-point cubic vs analytic motion; no production inverse imports',
        'time_uncertainty_ns':0,'excluded_power_support_indices':[0,1024]}
