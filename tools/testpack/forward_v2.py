"""Independent Cartesian, analytic interval forward model (not production inverse).

Formal Arb series give a midpoint second-order correction and a symmetric
whole-panel fourth-order remainder. No finite-difference agreement is a proof.
API: https://python-flint.readthedocs.io/en/stable/arb_series.html
"""
from contextlib import contextmanager
from fractions import Fraction as Q
from functools import lru_cache
import json
from types import MappingProxyType

from backend.inversion.contracts import canonical_bytes
from backend.inversion.interval_runtime import RationalInterval, ProofBudget, ball, enclose, proof_context, check_fraction
from backend.inversion.synthetic_registry import CaseDefinition, case_definition
from backend.inversion.transactions import InversionError


@contextmanager
def _context(*, series_cap: int = 4):
    if type(series_cap) is not int or series_cap not in (4,6):
        raise ValueError('Supported series cap required')
    with proof_context() as flint:
        cap=flint.ctx.cap
        try:
            flint.ctx.cap=series_cap
            yield flint
        finally: flint.ctx.cap=cap


def _interval(value, flint):
    return flint.arb(ball((value.lo+value.hi)/2),ball((value.hi-value.lo)/2))


def _coefficient(value, index):
    if value.prec<=index: raise InversionError('proof_series_truncated')
    result=value[index]
    if not result.is_finite(): raise InversionError('proof_nonfinite')
    return result


def _panel_quadrature(midpoint, segment, h: Q) -> tuple[object,Q]:
    """Integral center h*c0+h^3*c2/12, radius h^5*sup|c4|/80.

    c4 is P''''/24 over the WHOLE panel and root enclosure, not its midpoint.
    The center remains an Arb ball; its own rounding/parameter error is kept.
    """
    if type(h) is not Q or h<=0: raise ValueError('Positive exact panel width required')
    check_fraction(h)
    _coefficient(midpoint,4)  # A truncated power series must never imply zero c4.
    c0,c2=_coefficient(midpoint,0),_coefficient(midpoint,2)
    enclose(c0); enclose(c2)
    fourth=enclose(_coefficient(segment,4))
    h2=check_fraction(h*h); h3=check_fraction(h2*h)
    h4=check_fraction(h3*h); h5=check_fraction(h4*h)
    correction=c0*ball(h)+c2*ball(check_fraction(h3/12))
    enclose(correction)
    radius=check_fraction(check_fraction(h5*max(abs(fourth.lo),abs(fourth.hi)))/80)
    return correction,radius


def _positive_domain(value):
    constant=_coefficient(value,0) if hasattr(value,'prec') else value
    if not constant.is_finite() or not constant>0:
        raise InversionError('coverage_basis_mismatch')


def _joint(g, theta):
    """Two circle intersection, expressed without an inverse trig derivative."""
    bx,by,r,a,l=(ball(Q(g[k])) for k in ('pivot_x_m','pivot_y_m','crank_radius_m','rear_arm_m','pitman_length_m'))
    dx=g['rotation_sign']*r*theta.sin()-bx
    dy=r*theta.cos()-by
    squared=dx*dx+dy*dy
    _positive_domain(squared)
    distance=squared.sqrt()
    along=(a*a-l*l+distance*distance)/(2*distance)
    height_squared=a*a-along*along
    _positive_domain(height_squared)
    height=g['branch']*height_squared.sqrt()
    # Rear-arm vector relative to beam pivot.
    return (along*dx-height*dy)/distance,(along*dy+height*dx)/distance


def _jacobian(g, theta_ball, flint):
    theta=flint.arb_series([theta_ball,1],prec=4)
    x,y=_joint(g,theta)
    return (x*y.derivative()-y*x.derivative())*ball(Q(g['horsehead_radius_m'])*g['displacement_sign']/Q(g['rear_arm_m'])**2)


def _sign(value):
    interval=enclose(value)
    if interval.lo>0: return 1
    if interval.hi<0: return -1
    raise InversionError('proof_root_ambiguous')


@lru_cache(maxsize=128)
def _roots(geometry_bytes):
    g=json.loads(geometry_bytes)
    with _context() as flint:
        # Fixed search grid and exactly 96 bisections, no adaptive precision.
        tau=enclose(2*flint.arb.pi()).lo
        grid=tuple(tau*Q(i,720) for i in range(721))
        signs=tuple(_sign(_coefficient(_jacobian(g,ball(t),flint),0)) for t in grid)
        roots=[]
        for i in range(720):
            if signs[i]==signs[i+1]: continue
            lower,upper=grid[i:i+2]; left=signs[i]
            for _ in range(96):
                middle=(lower+upper)/2
                sign=_sign(_coefficient(_jacobian(g,ball(middle),flint),0))
                if sign==left: lower=middle
                else: upper=middle
            root=RationalInterval(lower,upper)
            # Prove simple root and distinguish displacement minimum from maximum.
            derivative=_coefficient(_jacobian(g,_interval(root,flint),flint),1)
            direction=_sign(derivative)
            roots.append((direction,root))
        if len(roots)!=2 or {x[0] for x in roots}!={-1,1}:
            raise InversionError('proof_root_isolation_failed')
        return tuple(root for direction,root in sorted(roots,key=lambda x:-x[0]))


def _checked_case(case):
    if type(case) is not CaseDefinition or case!=case_definition(case.config_sha256,case.case_id):
        raise InversionError('coverage_basis_mismatch')
    return case.body


def reversal_brackets(case):
    model=_checked_case(case)
    return _roots(canonical_bytes(model['geometry']))


def jacobian_enclosure(case, theta: RationalInterval):
    model=_checked_case(case)
    with _context() as flint:
        return enclose(_coefficient(_jacobian(model['geometry'],_interval(theta,flint),flint),0))


def _state(model, time_ball, root, flint, budget):
    budget.consume()
    g,b,d,m=(model[k] for k in ('geometry','bodies','drive','motion'))
    t=flint.arb_series([time_ball,1],prec=6)
    rate=2*flint.arb.pi()/ball(Q(m['period_s']))
    phase=t*rate; epsilon=ball(Q(m['epsilon']))
    theta=_interval(root,flint)+phase+epsilon*phase.sin()
    omega=rate*(1+epsilon*phase.cos())
    _positive_domain(omega)
    alpha=-epsilon*rate*rate*phase.sin()
    x,y=_joint(g,theta)
    velocity=(x*y.derivative()-y*x.derivative())*ball(Q(g['horsehead_radius_m'])*g['displacement_sign']/Q(g['rear_arm_m'])**2)
    jacobian=velocity/omega
    exp=(2*velocity/ball(Q(model['load']['transition_speed_m_s']))).exp()
    _positive_domain(exp+1)
    load=ball(Q(model['load']['mean_N']))+ball(Q(model['load']['half_span_N']))*(exp-1)/(exp+1)
    uprime=ball(Q(b['gravity_m_s2']))*(-g['rotation_sign']*ball(Q(b['crank_first_moment_x_kg_m']))*theta.cos()
            -ball(Q(b['crank_first_moment_y_kg_m']))*theta.sin())
    inertia=ball(Q(b['crank_inertia_kg_m2'])+Q(d['upstream_inertia_kg_m2']))
    torque=uprime+inertia*alpha+ball(Q(d['crank_friction_Nm']))+ball(Q(d['viscous_friction_Nm_s']))*omega
    power=torque*omega+load*velocity+ball(Q(d['loss_W']))
    for series in (power,theta,omega,alpha,load,jacobian):
        for k in range(3): _coefficient(series,k)
    _coefficient(power,4)
    return {'power_W':power,'theta_rad':theta,'omega_rad_s':omega,'alpha_rad_s2':alpha,
            'load_N':load,'J_m':jacobian,'x':x,'y':y}


def forward_enclosures(case: CaseDefinition, time_s: Q, *, budget=None):
    model=_checked_case(case); check_fraction(time_s)
    if not Q(-1,1000)<=time_s<=Q(8001,1000): raise InversionError('coverage_time_unsupported')
    budget=ProofBudget() if budget is None else budget
    root=reversal_brackets(case)[0]
    with _context(series_cap=6) as flint:
        state=_state(model,ball(time_s),root,flint,budget)
        width=Q(model['motion']['power_window_ns'],10**9); h=width/64
        integral=flint.arb(0); error=Q(0)
        for i in range(64):
            lo=time_s-width/2+i*h; hi=lo+h
            middle=_state(model,ball((lo+hi)/2),root,flint,budget)['power_W']
            segment=_state(model,_interval(RationalInterval(lo,hi),flint),root,flint,budget)['power_W']
            correction,radius=_panel_quadrature(middle,segment,h)
            integral+=correction
            enclose(integral)
            error=check_fraction(error+radius)
        mean=integral/ball(width)
        mean+=flint.arb(0,ball(error/width))
        output={key:enclose(_coefficient(value,0)) for key,value in state.items() if key not in ('x','y')}
        output['power_instant_W']=output['power_W']
        output['power_W']=enclose(mean)
        x0,y0=_joint(model['geometry'],_interval(root,flint))
        x,y=_coefficient(state['x'],0),_coefficient(state['y'],0)
        output['displacement_m']=enclose(flint.arb.atan2(x0*y-y0*x,x0*x+y0*y)
            *ball(Q(model['geometry']['horsehead_radius_m'])*model['geometry']['displacement_sign']))
        if output['power_W'].width>Q(2,100) or output['theta_rad'].width>Q(2,10**20):
            raise InversionError('generator_error_budget_exceeded')
        return MappingProxyType(output)


def quantize_interval(value: RationalInterval, scale: Q):
    check_fraction(scale)
    if scale<=0: raise ValueError('Positive exact scale required')
    # Python rounds Fraction ties to even using integer arithmetic.
    lower=round(check_fraction(value.lo/scale)); upper=round(check_fraction(value.hi/scale))
    if lower!=upper: raise InversionError('proof_rounding_ambiguous')
    if not -(2**31)<=lower<2**31: raise InversionError('generator_i32_overflow')
    return lower
