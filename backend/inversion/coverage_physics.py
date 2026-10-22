"""Separate physical and numerical N/J bounds; no reference or Fhat admission."""
from dataclasses import dataclass
from datetime import datetime,timezone,timedelta
from fractions import Fraction as Q
from functools import lru_cache
from types import MappingProxyType

from .coverage_sources import SourceProof,verified,matches_inputs,_context,_hull,_beta,_root,_coeff
from .interval_runtime import RationalInterval as I,ball,enclose,check_fraction
from .motion import MotionPoint
from .motion_bounds import MotionBounds
from .transactions import InversionError


@dataclass(frozen=True)
class AdmissionPoint:
    power_index: int
    status: str
    reason_codes: tuple[str,...]
    motion: MotionBounds
    physical_N_error_Nm: Q | None
    physical_J_error_m: Q | None
    numerical_N_error_Nm: Q | None
    numerical_J_error_m: Q | None
    numerical_s_error_m: Q | None


@dataclass(frozen=True)
class NominalState:
    N: Q
    J: Q
    s: Q
    terms: object


LIMITS=(Q(100),Q(1,10000),Q(1,100),Q(1,10**7),Q(1,10**7))
REASONS=('physical_torque_budget_exceeded','physical_factor_budget_exceeded',
    'numerical_torque_budget_exceeded','numerical_factor_budget_exceeded','numerical_displacement_budget_exceeded')


def budget_status(*values):
    if len(values)!=5 or any(value is None for value in values): return 'refused',('error_budget_unknown',)
    if any(type(value) is not Q or check_fraction(value)<0 for value in values): return 'refused',('error_budget_invalid',)
    reasons=tuple(reason for value,limit,reason in zip(values,LIMITS,REASONS) if value>limit)
    return ('refused' if reasons else 'valid'),reasons


def domain_status(parameters,*,power,omega,alpha,window_ns):
    reasons=[]
    for name,interval in (('power_W',power),('speed_rad_s',omega),('acceleration_rad_s2',alpha)):
        bound=getattr(parameters.domain,name)
        if interval.lo<Q(bound.min) or interval.hi>Q(bound.max): reasons.append(name+'_interval_outside_domain')
    if omega.lo<=Q(parameters.domain.speed_rad_s.min): reasons.append('speed_interval_not_strictly_observable')
    ratio=Q(parameters.drive.ratio); grid=parameters.drive.loss_grid
    if power.lo<Q(grid.power_W[0]) or power.hi>Q(grid.power_W[-1]) or ratio*omega.lo<Q(grid.motor_speed_rad_s[0]) or ratio*omega.hi>Q(grid.motor_speed_rad_s[-1]):
        reasons.append('loss_interval_outside_domain')
    if not parameters.domain.window_ns.min<=window_ns<=parameters.domain.window_ns.max: reasons.append('power_window_outside_domain')
    # Whole declared operating-condition ranges, not just an invented center.
    for condition in parameters.provenance.operating_conditions:
        for name in ('temperature_K','supply_voltage_V','supply_frequency_Hz'):
            actual=getattr(condition,name); domain=getattr(parameters.domain,name)
            if actual.min<domain.min or actual.max>domain.max: reasons.append(name+'_interval_outside_domain')
    return tuple(dict.fromkeys(reasons))


def enclose_terms(parameters,case,*,theta,omega,alpha,power,loss):
    """Independent exact-parameter polar geometry and complete numerator."""
    with _context() as flint:
        t,w,a,p,l=(_hull(value,flint) for value in (theta,omega,alpha,power,loss))
        g=case.body['geometry']; b=parameters.bodies; d=parameters.drive
        beta=_beta(g,flint.arb_series([t,1],prec=4),flint)
        root=_root(case.config_sha256,case.case_id)
        bottom=_beta(g,_hull(root,flint),flint)
        jacobian=ball(Q(g['horsehead_radius_m'])*g['displacement_sign'])*_coeff(beta,1)
        displacement=ball(Q(g['horsehead_radius_m'])*g['displacement_sign'])*(_coeff(beta,0)-bottom)
        uprime=ball(Q(b.gravity_m_s2))*(-g['rotation_sign']*ball(Q(b.crank_first_moment_x_kg_m))*t.cos()-ball(Q(b.crank_first_moment_y_kg_m))*t.sin())
        numerator=(p-l)/w-uprime-ball(Q(b.crank_inertia_kg_m2)+Q(d.upstream_inertia_kg_m2))*a
        numerator-=ball(Q(d.crank_friction_Nm))+ball(Q(d.viscous_friction_Nm_s))*w
        return MappingProxyType({'N':enclose(numerator),'J':enclose(jacobian),'s':enclose(displacement)})


@lru_cache(maxsize=2)
def _geometry(raw):
    from .parameter_contracts_v2 import parse_parameter_set_v2
    from .geometry import build_geometry
    parameters=parse_parameter_set_v2(raw)
    return build_geometry(parameters)


def nominal_state(inputs,parameters,motion):
    """Keep the existing QR/float geometry/energy and exact final numerator."""
    from .mechanics import energy_terms
    from .losses import lookup_loss
    power=next(s for s in inputs.power.samples if s.index==motion.power_index)
    geometry=_geometry(parameters.raw); point=geometry.at(motion.theta_rad)
    energy=energy_terms(geometry,motion.theta_rad,parameters)
    omega=Q.from_float(motion.omega_rad_s); alpha=Q.from_float(motion.alpha_rad_s2)
    loss=lookup_loss(parameters,power.value_si,Q(parameters.drive.ratio)*omega)
    drive_exact=(power.value_si-Q.from_float(loss.loss_W))/omega-Q(parameters.drive.upstream_inertia_kg_m2)*alpha
    drive=float(drive_exact)
    friction=Q(parameters.drive.crank_friction_Nm)+Q(parameters.drive.viscous_friction_Nm_s)*omega
    numerator=Q.from_float(drive)-Q.from_float(energy.U_prime_Nm)-Q.from_float(energy.M_kg_m2)*alpha-Q.from_float(energy.M_prime_kg_m2)*omega*omega/2-friction
    return NominalState(numerator,Q.from_float(point.J_m),Q.from_float(point.s_m),MappingProxyType(dict(
        N_Nm=float(numerator),J_m=point.J_m,omega_rad_s=motion.omega_rad_s,alpha_rad_s2=motion.alpha_rad_s2,
        theta_rad=motion.theta_rad,drive_Nm=drive,power_W=float(power.value_si),loss_W=loss.loss_W,
        U_prime_Nm=energy.U_prime_Nm,M_kg_m2=energy.M_kg_m2,M_prime_kg_m2=energy.M_prime_kg_m2,
        friction_Nm=float(friction),motor_speed_rad_s=float(Q(parameters.drive.ratio)*omega))))


def _distance(left,right):
    return check_fraction(max(abs(left.lo-right.hi),abs(left.hi-right.lo)))


def admit_point(inputs,parameters,motion:MotionPoint,bounds:MotionBounds,proof:SourceProof)->AdmissionPoint:
    values=(None,)*5
    def refused(reasons): return AdmissionPoint(motion.power_index,'refused',tuple(reasons),bounds,*values)
    if not verified(proof): return refused(proof.reason_codes if type(proof) is SourceProof else ('coverage_basis_missing',))
    if not matches_inputs(proof,inputs,parameters): return refused(('coverage_input_mismatch',))
    if bounds.status!='valid': return refused(bounds.reason_codes)
    if bounds.power_index!=motion.power_index or bounds.time_ns!=motion.time_ns: return refused(('motion_window_mismatch',))
    try:
        theta,omega,alpha=bounds.exact_center
        physical=tuple(I(c-e,c+e) for c,e in zip(bounds.exact_center,bounds.physical))
        sample=next(s for s in inputs.power.samples if s.index==motion.power_index)
        if sample.quality!=0 or sample.value_si is None: return refused(('invalid_power_quality',))
        power_error=proof.power_error_W[motion.power_index]
        power=I(sample.value_si-power_error,sample.value_si+power_error)
        domain=domain_status(parameters,power=power,omega=physical[1],alpha=physical[2],window_ns=inputs.power_window_ns)
        if domain: return refused(domain)
        half=Q(inputs.power_window_ns,2)+inputs.time_bound_ns
        if sample.time_ns-half<inputs.cycle.start_ns or sample.time_ns+half>inputs.cycle.end_ns:
            return refused(('power_support_outside_cycle',))
        loss_error=Q(parameters.coverage_contract.body['measurement']['loss_error_W'])
        center=enclose_terms(parameters,proof.case,theta=I(theta,theta),omega=I(omega,omega),alpha=I(alpha,alpha),
            power=I(sample.value_si,sample.value_si),loss=I(Q(1200),Q(1200)))
        box=enclose_terms(parameters,proof.case,theta=physical[0],omega=physical[1],alpha=physical[2],
            power=power,loss=I(1200-loss_error,1200+loss_error))
        nominal=nominal_state(inputs,parameters,motion)
        values=(_distance(box['N'],center['N']),_distance(box['J'],center['J']),
            _distance(I(nominal.N,nominal.N),center['N']),_distance(I(nominal.J,nominal.J),center['J']),
            _distance(I(nominal.s,nominal.s),center['s']))
        status,reasons=budget_status(*values)
        return AdmissionPoint(motion.power_index,status,reasons,bounds,*values)
    except InversionError as error:
        return refused((error.code.removeprefix('inversion.'),))
    except (ValueError,TypeError,OverflowError,ZeroDivisionError,StopIteration):
        return refused(('numerical_failure',))
