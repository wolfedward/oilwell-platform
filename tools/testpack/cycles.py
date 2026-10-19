"""Native synthetic cycle samples plus explicitly non-mechanistic outlines."""
from copy import deepcopy
from decimal import Decimal, ROUND_HALF_EVEN
from functools import lru_cache
import json
import math

from .contracts import Artifact,Case,Channel,Cycle,PackConfig
from .mechanics import base_model,forward_state,position,turn_time,window_power,_slope
from .storage import canonical_bytes,derive_uniform

ROLES=('active_power','crank_angle','load','displacement')
UNITS=('W','rad','kN','m')
SCALES=('0.01','0.000001','0.001','0.000001')


def quantize(value,scale):
    result=int((Decimal(str(value))/Decimal(scale)).quantize(Decimal(1),rounding=ROUND_HALF_EVEN))
    if not -2**31<=result<2**31:
        raise ValueError('sample outside signed i32 range')
    return result


@lru_cache(maxsize=64)
def _trajectory(model_bytes):
    model=json.loads(model_bytes)
    states=[]
    powers=[]
    window_error=derivative_error=0.0
    for index in range(1025):
        t=index/128
        s=forward_state(model,t)
        mean16,mean32=window_power(model,t,panels=16),window_power(model,t,panels=32)
        window_error=max(window_error,abs(mean16-mean32))
        derivative_error=max(derivative_error,abs(_slope(model,s['theta_rad'],.0002)-_slope(model,s['theta_rad'],.0001)))
        powers.append(mean32)
        states.append((s['theta_rad'],s['load_N']/1000,s['displacement_m']))
    if window_error>.02 or derivative_error>1e-5:
        raise ValueError('independent numerical refinement budget exceeded')
    return tuple(powers),tuple(states),window_error,derivative_error


def generate_cycles(config:PackConfig)->tuple[Cycle,...]:
    base=base_model()
    names=['base','balance','friction','speed','noise','missing','clipped','shifted']
    if config.profile=='extended':
        names.extend(f'variant-{i:02d}' for i in range(40))
    output=[]
    for variant in names:
        case_id='cycle-'+variant
        model=deepcopy(base)
        if variant=='balance':
            model['bodies']['crank_first_moment_x_kg_m']*=.8
        elif variant=='friction':
            model['drive']['crank_friction_Nm']*=1.5
        elif variant=='speed':
            model['motion']['epsilon']=.1
        elif variant.startswith('variant-'):
            # These parameter ranges are fixed before any inversion evaluation.
            model['load']['mean_N']=42000+8000*derive_uniform(config.seed,case_id,'mean',0)
            model['load']['half_span_N']=10000+6000*derive_uniform(config.seed,case_id,'span',0)
            model['drive']['crank_friction_Nm']=250+300*derive_uniform(config.seed,case_id,'friction',0)
            model['bodies']['crank_first_moment_x_kg_m']=1400+600*derive_uniform(config.seed,case_id,'balance',0)
        powers,states,window_error,derivative_error=_trajectory(canonical_bytes(model))
        channels=[]
        for j,(role,unit,scale) in enumerate(zip(ROLES,UNITS,SCALES)):
            raw,quality=[],[]
            for i in range(1025):
                value=powers[i] if j==0 else states[i][j-1]
                q=0
                if variant=='noise':
                    amplitude=(10,1e-6,.05,1e-5)[j]
                    value+=amplitude*(2*derive_uniform(config.seed,case_id,role,i)-1)
                elif j==0 and variant=='missing' and 400<=i<=419:
                    value,q=0,1
                elif j==0 and variant=='clipped' and abs(value)>20000:
                    value,q=math.copysign(20000,value),2
                elif j==0 and variant=='shifted':
                    value,q=(powers[i+2],0) if i<1023 else (0,1)
                raw.append(quantize(value,scale)); quality.append(q)
            channels.append(Channel(role,unit,scale,'0',tuple(raw),tuple(quality)))
        turn=max(range(1025),key=lambda i:states[i][2])
        continuous=turn_time(model)
        event_error=abs(turn/128-continuous)
        if event_error>1/256:
            raise ValueError('discrete reversal outside half-sample event budget')
        diagnostics={'source_kind':'synthetic','category':'signal_fault' if variant in ('noise','missing','clipped','shifted') else 'mechanistic',
            'window_refinement_error_W':window_error,'derivative_refinement_error_m':derivative_error,
            'continuous_turn_s':continuous,'event_time_error_s':event_error,
            'power_window_ns':1000000,'power_sample_shift_ns':15625000 if variant=='shifted' else 0,
            'stroke_m':max(s[2] for s in states),'strokes_per_min':7.5,
            'load_min_kN':min(s[1] for s in states),'load_max_kN':max(s[1] for s in states),
            'reference_truth':'model before signal perturbations'}
        output.append(Cycle(case_id,128,tuple(i*7812500 for i in range(1025)),tuple(channels),turn,model,diagnostics))
    return tuple(output)


def generate_shape_cases(config:PackConfig)->tuple[Case,...]:
    if config.profile!='extended':
        return ()
    # Normalized, hand-authored shapes. Their names describe outlines only,
    # never confirmed faults or classifier ground truth.
    outlines=[
        ('normal','正常闭合轮廓',[(0,.1),(.12,.95),(.85,1),(1,.85),(.9,.05),(.1,0)]),
        ('travel-valve','游动阀漏失轮廓',[(0,0),(.15,.5),(.45,.9),(.8,1),(1,.8),(.92,.05)]),
        ('standing-valve','固定阀漏失轮廓',[(0,.4),(.1,.9),(.95,1),(.75,.55),(.4,0),(.08,.05)]),
        ('double-valve','双阀漏失窄环',[(0,.75),(.15,.84),(.85,.8),(1,.72),(.7,.7),(.2,.72)]),
        ('rod-break','抽油杆断脱窄环',[(0,.05),(.2,.1),(.8,.09),(1,.04),(.8,0),(.2,.01)]),
        ('underfill','供液不足轮廓',[(0,0),(.1,.1),(.22,.95),(.5,1),(1,.95),(.95,.7),(.4,.7),(.2,0)]),
        ('gas','气体影响轮廓',[(0,0),(.15,.8),(.4,1),(1,.93),(.8,.6),(.45,.3),(.2,0)]),
        ('gas-lock','气锁斜窄环',[(0,0),(.5,.65),(1,1),(.9,.75),(.4,.25)]),
        ('restriction','管线堵塞轮廓',[(0,.1),(.2,.5),(.4,1.3),(.8,1),(1,.95),(.82,.6),(.75,0),(.2,.1)]),
        ('sand','砂阻波动轮廓',[(0,.1),(.04,.8),(.2,1),(.3,.92),(.4,1.1),(.55,.95),(.7,1.06),(1,.9),(.95,.25),(.7,.2),(.6,.4),(.5,.1),(.3,.3)]),
        ('plunger-out','活塞部分脱出轮廓',[(0,0),(.2,.9),(.4,1),(.65,.85),(.67,.05),(1,0),(.8,-.03),(.3,.05)]),
        ('pump-impact','碰泵轮廓',[(0,.05),(.15,.95),(.4,1.1),(.5,1),(.95,1),(.9,.8),(.82,.05),(.3,0)]),
    ]
    cases=[]
    for name,label,points in outlines:
        points=points+[points[0]]
        actual=[[round(x*3.6,6),round(32+y*28,6)] for x,y in points]
        data={'schema':'owp.testpack.outline/1','label':label,'x_unit':'m','y_unit':'kN','points':actual,
              'source_kind':'synthetic','category':'shape_only','inversion_eligible':False}
        cases.append(Case('shape-'+name,'shape_only',(Artifact(f'shapes/{name}.json','application/json',canonical_bytes(data)),),
                          {'label':label,'inversion_eligible':False,'diagnosis_ground_truth':False},()))
    return tuple(cases)
