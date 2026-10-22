"""Installed, independent polar/energy source checker; no reference channels.

The generator uses Cartesian circle intersection and a torque formula. Here
beam angle follows the cosine law and power follows the derivative of energy.
Arb Taylor coefficients enclose derivatives on each whole integration panel.
No production inverse geometry/motion or testpack forward code is imported.
"""
from contextlib import contextmanager
from dataclasses import dataclass,field
from fractions import Fraction as Q
from functools import lru_cache
from hashlib import sha256
from pathlib import Path

from .contracts import canonical_bytes,strict_json
from .coverage_contracts import PredictionEvidence,channel_evidence_bytes,pin_wire
from .interval_runtime import RationalInterval,ProofBudget,ball,enclose,proof_context,check_fraction
from .parameter_contracts_v2 import ParameterSetV2,parse_parameter_set_v2
from .synthetic_registry import CaseDefinition,case_definition,counter_fraction
from .transactions import InversionError

EPOCH=1790784000000000000
_SEAL=object()


@dataclass(frozen=True,init=False)
class SourceProof:
    status: str
    reason_codes: tuple[str,...]
    case: CaseDefinition | None
    derivative_bounds: tuple[Q,Q,Q,Q] | None
    angle_error_rad: tuple[Q,...] | None
    differential_time_s: tuple[Q,...] | None
    common_time_s: Q | None
    power_error_W: tuple[Q,...] | None
    evidence_sha256: str
    derivative_support_ns: tuple[Q,Q] | None
    _seal: object=field(repr=False,compare=False)


def _result(status,reasons,case,digest,*,derivatives=None,angles=None,times=None,common=None,power=None,support=None):
    obj=object.__new__(SourceProof)
    for key,value in zip(SourceProof.__dataclass_fields__,(
        status,tuple(reasons),case,derivatives,angles,times,common,power,digest,support,_SEAL)):
        object.__setattr__(obj,key,value)
    return obj


def verified(proof):
    return type(proof) is SourceProof and proof._seal is _SEAL and proof.status=='verified'


def checker_sha256():
    root=Path(__file__).resolve().parent
    paths=('coverage_sources.py','synthetic_registry.py','interval_runtime.py','coverage_contracts.py',
        'parameter_contracts_v2.py','policy_v2.py','contracts.py','parameter_contracts.py','series.py','transactions.py')
    return sha256(canonical_bytes([{'path':name,'sha256':sha256((root/name).read_bytes()).hexdigest()} for name in paths])).hexdigest()


def installed_generator_sha256():
    # Read-only hashing; never imports or calls the generator's physical model.
    from tools.testpack.storage import generator_sources_v2,canonical_bytes as pack_bytes
    return sha256(pack_bytes(generator_sources_v2())).hexdigest()


def _require(condition):
    if not condition: raise InversionError('coverage_basis_mismatch')


@contextmanager
def _context():
    with proof_context() as flint:
        cap=flint.ctx.cap
        try:
            flint.ctx.cap=4
            yield flint
        finally: flint.ctx.cap=cap


def _hull(interval,flint):
    return flint.arb(ball((interval.lo+interval.hi)/2),ball(interval.width/2))


def _coeff(series,index):
    if series.prec<=index: raise InversionError('proof_series_truncated')
    value=series[index]
    if not value.is_finite(): raise InversionError('proof_nonfinite')
    return value


def _beta(g,theta,flint):
    bx,by,r,a,l=(ball(Q(g[k])) for k in ('pivot_x_m','pivot_y_m','crank_radius_m','rear_arm_m','pitman_length_m'))
    # Installed family has negative D_x,D_y for its entire revolution. This
    # fixes the argument branch; no atan2 jump or unproved unwrap is allowed.
    _require(Q(g['pivot_x_m'])>Q(g['crank_radius_m']) and Q(g['pivot_y_m'])>Q(g['crank_radius_m']))
    dx=g['rotation_sign']*r*theta.sin()-bx; dy=r*theta.cos()-by
    rho=(dx*dx+dy*dy).sqrt()
    cosine=(a*a+rho*rho-l*l)/(2*a*rho)
    return (dy/dx).atan()-flint.arb.pi()+g['branch']*cosine.acos()


@lru_cache(maxsize=128)
def _root(config_sha,case_id):
    g=case_definition(config_sha,case_id).body['geometry']
    with _context() as flint:
        # Enumerate every reversal using radial crank/pitman collinearity.
        # Strict triangle inequalities prove a non-toggle assembly for all θ.
        bx,by,r,a,l=(ball(Q(g[k])) for k in ('pivot_x_m','pivot_y_m','crank_radius_m','rear_arm_m','pitman_length_m'))
        distance=(bx*bx+by*by).sqrt()
        _require(enclose(distance-r).lo>abs(Q(g['rear_arm_m'])-Q(g['pitman_length_m']))
            and enclose(distance+r).hi<Q(g['rear_arm_m'])+Q(g['pitman_length_m']))
        delta=flint.arb.atan2(g['rotation_sign']*bx,by); tau=2*flint.arb.pi(); roots=[]
        for radial in (r+l,r-l):
            c=(radial*radial+distance*distance-a*a)/(2*radial*distance)
            _require(enclose(c).lo>-1 and enclose(c).hi<1)
            for direction in (-1,1):
                theta=delta+direction*c.acos()
                if enclose(theta).hi<0: theta+=tau
                elif enclose(theta).lo<0: raise InversionError('proof_root_ambiguous')
                assembly=g['branch']*(radial-r)*(g['rotation_sign']*theta.sin()*by-theta.cos()*bx)
                if enclose(assembly).hi<0: continue
                _require(enclose(assembly).lo>0)
                series=_beta(g,flint.arb_series([theta,1],prec=4),flint)
                derivative=enclose(_coeff(series,1)); curvature=enclose(2*_coeff(series,2)*ball(Q(g['horsehead_radius_m'])*g['displacement_sign']))
                _require(derivative.lo<=0<=derivative.hi)
                if curvature.lo>0: roots.append(enclose(theta))
        _require(len(roots)==1)
        return roots[0]


def _state(model,time,root,flint):
    g,b,d,m=(model[k] for k in ('geometry','bodies','drive','motion'))
    rate=2*flint.arb.pi()/ball(Q(m['period_s'])); epsilon=ball(Q(m['epsilon']))
    t=flint.arb_series([time,1],prec=4); phase=rate*t
    theta=_hull(root,flint)+phase+epsilon*phase.sin()
    omega=rate*(1+epsilon*phase.cos())
    beta=_beta(g,theta,flint)
    velocity=beta.derivative()*ball(Q(g['horsehead_radius_m'])*g['displacement_sign'])
    energy=ball(Q(b['gravity_m_s2']))*(-g['rotation_sign']*ball(Q(b['crank_first_moment_x_kg_m']))*theta.sin()
        +ball(Q(b['crank_first_moment_y_kg_m']))*theta.cos())
    energy+=ball((Q(b['crank_inertia_kg_m2'])+Q(d['upstream_inertia_kg_m2']))/2)*omega*omega
    e=(2*velocity/ball(Q(model['load']['transition_speed_m_s']))).exp()
    load=ball(Q(model['load']['mean_N']))+ball(Q(model['load']['half_span_N']))*(e-1)/(e+1)
    power=energy.derivative()+ball(Q(d['crank_friction_Nm']))*omega+ball(Q(d['viscous_friction_Nm_s']))*omega*omega
    power+=load*velocity+ball(Q(d['loss_W']))
    for index in range(3): _coeff(power,index)
    return power,theta


@lru_cache(maxsize=2048)
def _expected(config_sha,physics_id,index):
    case=case_definition(config_sha,physics_id); model=case.body
    root=_root(config_sha,physics_id); t=Q(index,128)
    with _context() as flint:
        center,theta=_state(model,ball(t),root,flint)
        width=Q(1,1000); h=width/64; total=flint.arb(0); remainder=Q(0)
        for i in range(64):
            lo=t-width/2+i*h; hi=lo+h
            mid,_=_state(model,ball((lo+hi)/2),root,flint)
            interval,_=_state(model,_hull(RationalInterval(lo,hi),flint),root,flint)
            second=enclose(2*_coeff(interval,2))
            total+=_coeff(mid,0)*ball(h)
            remainder=check_fraction(remainder+h**3*max(abs(second.lo),abs(second.hi))/24)
        mean=total/ball(width)+flint.arb(0,ball(remainder/width))
        mean_interval=enclose(mean); angle_interval=enclose(_coeff(theta,0))
        _require(mean_interval.width<=Q(2,100) and angle_interval.width<=Q(2,10**20))
        delta=enclose(mean-_coeff(center,0))
        return mean_interval,angle_interval,max(abs(delta.lo),abs(delta.hi))


def _code(value,scale):
    lower=round(check_fraction(value.lo/scale)); upper=round(check_fraction(value.hi/scale))
    if lower!=upper: raise InversionError('proof_rounding_ambiguous')
    _require(-(2**31)<=lower<2**31)
    return lower


def _bound(value,minimum):
    result=Q(value); _require(result>=minimum)
    return result


def _parameters_match(parameters,case):
    model=case.body
    for group in ('geometry','bodies'):
        _require(all(Q(getattr(getattr(parameters,group),key))==Q(value) for key,value in model[group].items()))
    for key in ('ratio','upstream_inertia_kg_m2','crank_friction_Nm','viscous_friction_Nm_s'):
        _require(Q(getattr(parameters.drive,key))==Q(model['drive'][key]))
    _require(parameters.drive.supply_kind=='direct_line_induction')
    grid=parameters.drive.loss_grid
    _require(tuple(map(Q,grid.power_W))==(Q(-250000),Q(250000)) and tuple(map(Q,grid.motor_speed_rad_s))==(Q(1),Q(250)))
    _require(all(Q(v)==1200 for row in grid.loss_W for v in row) and all(Q(v)==Q(2,100) for row in grid.error_W for v in row))
    _require(parameters.phase.zero_reading_rad==0 and parameters.phase.encoder_direction==1 and parameters.phase.unwrap_kind=='unwrapped')


def verify_sources(evidence: PredictionEvidence, parameters: ParameterSetV2) -> SourceProof:
    case=None; digest='0'*64
    try:
        _require(type(evidence) is PredictionEvidence and type(parameters) is ParameterSetV2 and not parameters.missing)
        # No caller-created dataclass can bypass the raw contract parser.
        _require(parse_parameter_set_v2(parameters.raw)==parameters)
        case=case_definition(evidence.config_sha256,evidence.case_id)
        coverage=parameters.coverage_contract.body; measurement=coverage['measurement']; motion=coverage['motion']
        digest=sha256(canonical_bytes({'power':evidence.power.channel_sha256,'angle':evidence.angle.channel_sha256,
            'contract':parameters.coverage_contract.raw_sha256,'checker':checker_sha256()})).hexdigest()
        _require(coverage['binding']['checker_sha256']==checker_sha256()
            and evidence.generator_sha256==installed_generator_sha256())
        for key in ('pack_id','case_id','config_sha256','generator_sha256'): _require(coverage[key]==getattr(evidence,key))
        _parameters_match(parameters,case)
        _require(int(coverage['cycle']['start_ns'])==EPOCH and int(coverage['cycle']['end_ns'])==EPOCH+8*10**9)
        _require(any(c.start_ns==EPOCH and c.end_ns==EPOCH+8*10**9 and c.physical_cycle_id==coverage['cycle']['physical_cycle_id'] for c in parameters.provenance.calibration_cycles))
        _require(int(coverage['power_support']['window_ns'])==1000000)
        variant=case.body['variant']; noisy=variant=='noise'; shifted=variant=='shifted'
        physics_id='cycle-base' if variant in ('noise','missing','clipped','shifted') else case.case_id
        root=_root(case.config_sha256,physics_id)
        with _context() as flint:
            rate=2*flint.arb.pi()/8; eps=Q(case.body['motion']['epsilon'])
            _require(max(abs(Q(motion['theta0_rad'])-root.lo),abs(Q(motion['theta0_rad'])-root.hi))<=Q(1,10**20))
            rate_image=enclose(rate)
            _require(max(abs(Q(motion['rate_rad_s'])-rate_image.lo),abs(Q(motion['rate_rad_s'])-rate_image.hi))<=Q(1,10**20))
            _require(Q(motion['epsilon_rad'])==eps)
            calculated=tuple(enclose(value).hi for value in (rate*ball(1+abs(eps)),rate**2*ball(abs(eps)),rate**3*ball(abs(eps)),rate**4*ball(abs(eps))))
        derivatives=tuple(_bound(motion[name],minimum) for name,minimum in zip(('omega_max_rad_s','acceleration_max_rad_s2','third_derivative_max_rad_s3','fourth_derivative_max_rad_s4'),calculated))
        common=_bound(coverage['time']['common_offset_bound_ns'],Q(15625000) if shifted else Q(0))/10**9
        differential=_bound(coverage['time']['differential_bound_ns'],Q(0))/10**9
        support=(Q(motion['support_start_ns']),Q(motion['support_end_ns']))
        _require(Q(EPOCH-250000000)<=support[0]<=EPOCH and EPOCH+8*10**9<=support[1]<=EPOCH+8250000000)
        angle_noise=_bound(measurement['angle_noise_error_rad'],Q(1,10**6) if noisy else Q(0))
        power_noise=_bound(measurement['power_noise_error_W'],Q(10) if noisy else Q(0))
        angle_error=_bound(measurement['angle_quantization_rad'],Q(1,2*10**8))+_bound(measurement['angle_calibration_error_rad'],Q(0))+angle_noise
        power_error=_bound(measurement['power_quantization_W'],Q(1,200))+power_noise
        _bound(measurement['loss_error_W'],Q(2,100))
        average_bound=Q(measurement['power_average_mismatch_W'])
        for channel in (evidence.power,evidence.angle):
            role=channel.role; item=coverage['inputs'][role]; encoding=item['encoding']
            _require(len(channel.raw)==1025 and channel.pin.first_index==0 and channel.pin.last_index==1024)
            _require(channel.time_ns==tuple(Q(EPOCH+i*7812500) for i in range(1025)))
            _require(channel.offset==0 and channel.scale==(Q(1,100) if role=='active_power' else Q(1,10**8)))
            _require(pin_wire(channel.pin)==strict_json(canonical_bytes(item['pin'])))
            for name in ('original_file_sha256','bound_file_sha256','channel_sha256'): _require(item[name]==getattr(channel,name))
            _require(sha256(channel_evidence_bytes(channel)).hexdigest()==channel.channel_sha256)
            for name,value in (('raw',channel.raw),('time',[str(t) for t in channel.time_ns]),('quality',channel.quality)):
                _require(item[name+'_sha256']==sha256(canonical_bytes(value)).hexdigest())
            _require(Q(encoding['scale'])==channel.scale and Q(encoding['offset'])==channel.offset)
            pre=_bound(encoding['pre_encoding_error_bound'],Q(1,100) if role=='active_power' else Q(1,10**20))
            if role=='active_power': power_error+=pre+average_bound
            else: angle_error+=pre
        budget=ProofBudget()
        for i in range(1025):
            budget.consume(129)  # Charge even cache hits; no resource-limit bypass.
            mean,angle,mismatch=_expected(case.config_sha256,physics_id,i)
            _require(mismatch<=average_bound)
            if noisy:
                angle_noise_value=Q(1,10**6)*(2*counter_fraction(case.case_id,'crank_angle',i)-1)
                angle=RationalInterval(angle.lo+angle_noise_value,angle.hi+angle_noise_value)
            _require(evidence.angle.quality[i]==0 and _code(angle,evidence.angle.scale)==evidence.angle.raw[i])
            quality=0
            if variant=='missing' and 400<=i<=419 or shifted and i>=1023:
                mean=RationalInterval(Q(0),Q(0)); quality=1
            elif shifted:
                budget.consume(129)
                mean,_,mismatch=_expected(case.config_sha256,physics_id,i+2)
                _require(mismatch<=average_bound)
            elif noisy:
                noise=10*(2*counter_fraction(case.case_id,'active_power',i)-1)
                mean=RationalInterval(mean.lo+noise,mean.hi+noise)
            elif variant=='clipped':
                if mean.lo>20000: mean=RationalInterval(Q(20000),Q(20000)); quality=2
                elif mean.hi<-20000: mean=RationalInterval(Q(-20000),Q(-20000)); quality=2
                else: _require(mean.lo>=-20000 and mean.hi<=20000)
            _require(evidence.power.quality[i]==quality and _code(mean,evidence.power.scale)==evidence.power.raw[i])
        return _result('verified',(),case,digest,derivatives=derivatives,angles=(angle_error,)*1025,
            times=(differential,)*1025,common=common,power=(power_error,)*1025,support=support)
    except InversionError as error:
        return _result('refused',(error.code.removeprefix('inversion.'),),case,digest)
