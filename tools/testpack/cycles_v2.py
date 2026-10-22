"""Fixed v2 native samples; reference channels never enter source admission."""
from fractions import Fraction as Q
from functools import lru_cache
from hashlib import sha256
import math

from backend.inversion.contracts import canonical_bytes
from backend.inversion.interval_runtime import ProofBudget, RationalInterval
from backend.inversion.synthetic_registry import case_definition, case_ids, counter_fraction
from .config_v2 import config_dict_v2
from .contracts import Channel, Cycle
from .forward_v2 import forward_enclosures, quantize_interval, reversal_brackets

ROLES=('active_power','crank_angle','load','displacement')
UNITS=('W','rad','kN','m')
SCALES=('0.01','0.00000001','0.001','0.000001')
NOISE=(Q(10),Q(1,10**6),Q(1,20),Q(1,100000))


def clip_interval(value):
    if value.lo>=-20000 and value.hi<=20000: return value,0
    if value.lo>20000: return RationalInterval(Q(20000),Q(20000)),2
    if value.hi<-20000: return RationalInterval(Q(-20000),Q(-20000)),2
    raise ValueError('Unresolved clipping boundary')


@lru_cache(maxsize=4)
def _trajectory(config_sha, physics_id):
    case=case_definition(config_sha,physics_id)
    budget=ProofBudget()
    states=tuple(forward_enclosures(case,Q(i,128),budget=budget) for i in range(1025))
    return states,budget.used


def _display_model(case):
    """Binary64 projection for old views/study fixtures, NEVER proof constants."""
    def numeric(value):
        if isinstance(value,dict) or hasattr(value,'items'):
            return {key:numeric(item) for key,item in value.items()}
        if isinstance(value,str):
            try: return float(Q(value))
            except ValueError: return value
        return value
    model=numeric(case.body)
    bottom,top=reversal_brackets(case)
    theta=float((bottom.lo+bottom.hi)/2)
    g=model['geometry']; dx=g['rotation_sign']*g['crank_radius_m']*math.sin(theta)-g['pivot_x_m']
    dy=g['crank_radius_m']*math.cos(theta)-g['pivot_y_m']; distance=math.hypot(dx,dy)
    along=(g['rear_arm_m']**2-g['pitman_length_m']**2+distance**2)/(2*distance)
    height=g['branch']*math.sqrt(g['rear_arm_m']**2-along**2)
    beta=math.atan2(along*dy+height*dx,along*dx-height*dy)
    model['motion'].update(theta_start_rad=theta,theta_top_rad=float((top.lo+top.hi)/2),beta_bottom_rad=beta)
    model['schema']='owp.testpack.mechanics/2'
    model['assumptions']=['Synthetic rigid four-bar case; binary64 display projection is not a proof source.']
    return model


def generate_case_v2(case):
    if case!=case_definition(case.config_sha256,case.case_id):
        raise ValueError('Only installed cases may be generated')
    variant=case.body['variant']
    physics_id='cycle-base' if variant in ('noise','missing','clipped','shifted') else case.case_id
    states,evaluations=_trajectory(case.config_sha256,physics_id)
    channels=[]
    for j,(role,unit,scale) in enumerate(zip(ROLES,UNITS,SCALES)):
        raw=[]; quality=[]
        key=('power_W','theta_rad','load_N','displacement_m')[j]
        for i in range(1025):
            value=states[i][key]; q=0
            if j==2: value=RationalInterval(value.lo/1000,value.hi/1000)
            if variant=='noise':
                noise=NOISE[j]*(2*counter_fraction(case.case_id,role,i)-1)
                value=RationalInterval(value.lo+noise,value.hi+noise)
            elif j==0 and variant=='missing' and 400<=i<=419:
                value=RationalInterval(Q(0),Q(0)); q=1
            elif j==0 and variant=='clipped':
                value,q=clip_interval(value)
            elif j==0 and variant=='shifted':
                value,q=(states[i+2][key],0) if i<1023 else (RationalInterval(Q(0),Q(0)),1)
            raw.append(quantize_interval(value,Q(scale))); quality.append(q)
        channels.append(Channel(role,unit,scale,'0',tuple(raw),tuple(quality)))
    turn=max(range(1025),key=lambda i:(states[i]['displacement_m'].lo+states[i]['displacement_m'].hi)/2)
    model=_display_model(case)
    # Presentation-only time; source proof independently verifies all native times.
    target=(model['motion']['theta_top_rad']-model['motion']['theta_start_rad'])%math.tau
    lo,hi=0.,8.
    for _ in range(64):
        t=(lo+hi)/2; phase=math.tau*t/8
        if phase+model['motion']['epsilon']*math.sin(phase)<target: lo=t
        else: hi=t
    continuous=(lo+hi)/2
    if abs(turn/128-continuous)>1/256:
        raise ValueError('Discrete reversal outside half-sample event budget')
    diagnostics={'source_kind':'synthetic','scope':'finite_synthetic_input/1','purpose':'software_pair_test',
        'field_accuracy_certified':False,'validation_status':'not_field_validated',
        'category':'signal_fault' if variant in ('noise','missing','clipped','shifted') else 'mechanistic',
        'physical_evaluations':evaluations,
        'power_enclosure_width_max_W':str(max(s['power_W'].width for s in states)),
        'angle_enclosure_width_max_rad':str(max(s['theta_rad'].width for s in states)),
        'power_pre_encoding_error_bound_W':'0.01','angle_pre_encoding_error_bound_rad':'0.00000000000000000001',
        'continuous_turn_s':continuous,'event_time_error_s':abs(turn/128-continuous),
        'power_window_ns':1000000,'power_sample_shift_ns':15625000 if variant=='shifted' else 0,
        'stroke_m':float(max(s['displacement_m'].hi for s in states)), 'strokes_per_min':7.5,
        'load_min_kN':float(min(s['load_N'].lo for s in states)/1000),
        'load_max_kN':float(max(s['load_N'].hi for s in states)/1000),
        'reference_truth':'Registered model before signal perturbations; not field measurements'}
    return Cycle(case.case_id,128,tuple(i*7812500 for i in range(1025)),tuple(channels),turn,model,diagnostics)


def generate_cycles_v2(config):
    config_sha=sha256(canonical_bytes(config_dict_v2(config))).hexdigest()
    return tuple(generate_case_v2(case_definition(config_sha,case_id)) for case_id in case_ids(config_sha))
