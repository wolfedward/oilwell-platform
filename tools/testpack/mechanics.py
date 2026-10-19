"""Independent Cartesian forward model, not a field-validated pump model.

The reference uses two-circle intersection, angular finite differences and a
potential-energy difference. It never imports the production inverse kernels.
Only crank mass/inertia are nonzero in this intentionally rigid baseline.
"""
import math


def _joint(model, theta):
    g = model['geometry']
    bx, by, r, arm, rod = (g[k] for k in ('pivot_x_m','pivot_y_m','crank_radius_m','rear_arm_m','pitman_length_m'))
    px, py = g['rotation_sign']*r*math.sin(theta), r*math.cos(theta)
    dx, dy = px-bx, py-by
    distance = math.hypot(dx,dy)
    if not abs(arm-rod) < distance < arm+rod:
        raise ValueError('linkage has no nonsingular circle intersection')
    along = (arm*arm-rod*rod+distance*distance)/(2*distance)
    height = math.sqrt(arm*arm-along*along)*g['branch']
    x = bx+along*dx/distance-height*dy/distance
    y = by+along*dy/distance+height*dx/distance
    return x, y, math.atan2(y-by,x-bx)


def _angle_delta(a,b):
    return math.atan2(math.sin(a-b),math.cos(a-b))


def _slope(model,theta,h=0.0002):
    beta = _joint(model,theta)[2]
    slope = sum(w*_angle_delta(_joint(model,theta+i*h)[2],beta)
                for i,w in ((-2,1),(-1,-8),(1,8),(2,-1)))/(12*h)
    g = model['geometry']
    return slope*g['horsehead_radius_m']*g['displacement_sign']


def _extrema(model):
    roots=[]
    for i in range(720):
        lo,hi=i*math.tau/720,(i+1)*math.tau/720
        left,right=_slope(model,lo),_slope(model,hi)
        if left*right >= 0:
            continue
        for _ in range(45):
            mid=(lo+hi)/2
            if left*_slope(model,mid) <= 0:
                hi=mid
            else:
                lo,left=mid,_slope(model,mid)
        roots.append((lo+hi)/2)
    if len(roots)!=2:
        raise ValueError('expected exactly two displacement reversals')
    return sorted(roots,key=lambda theta:model['geometry']['displacement_sign']*_joint(model,theta)[2])


def base_model():
    model={
        'schema':'owp.testpack.rigid-model/1',
        'assumptions':['rigid linkage','no rod-string elasticity','no valve dynamics',
                       'only crank downstream inertia and first mass moment nonzero'],
        'geometry':{'pivot_x_m':3.0,'pivot_y_m':2.0,'crank_radius_m':.5,'rear_arm_m':2.0,
                    'pitman_length_m':3.0,'horsehead_radius_m':6.0,'branch':1,'rotation_sign':1,'displacement_sign':1},
        'bodies':{'crank_inertia_kg_m2':200.0,'crank_first_moment_x_kg_m':1800.0,
                  'crank_first_moment_y_kg_m':0.0,'beam_inertia_kg_m2':0.0,
                  'beam_first_moment_x_kg_m':0.0,'beam_first_moment_y_kg_m':0.0,
                  'pitman_mass_kg':0.0,'pitman_com_fraction':.5,'pitman_inertia_kg_m2':0.0,'gravity_m_s2':9.80665},
        'drive':{'upstream_inertia_kg_m2':30.0,'ratio':100.0,'crank_friction_Nm':300.0,
                 'viscous_friction_Nm_s':100.0,'loss_W':1200.0},
        'load':{'mean_N':46000.0,'half_span_N':14000.0,'transition_speed_m_s':.05},
        'motion':{'period_s':8.0,'epsilon':0.0,'power_window_ns':1000000},
    }
    bottom,top=_extrema(model)
    model['motion'].update(theta_start_rad=bottom,theta_top_rad=top,
                           beta_bottom_rad=_joint(model,bottom)[2])
    return model


def position(model:dict,theta_rad:float)->dict:
    x,y,beta=_joint(model,theta_rad)
    g=model['geometry']
    displacement=g['displacement_sign']*g['horsehead_radius_m']*_angle_delta(beta,model['motion']['beta_bottom_rad'])
    return {'displacement_m':displacement,'beta_rad':beta,'J_m':_slope(model,theta_rad),'joint_m':(x,y)}


def _potential(model,theta):
    b,g=model['bodies'],model['geometry']
    return b['gravity_m_s2']*(-g['rotation_sign']*b['crank_first_moment_x_kg_m']*math.sin(theta)
                            +b['crank_first_moment_y_kg_m']*math.cos(theta))


def forward_state(model:dict,time_s:float)->dict:
    m,b,d=model['motion'],model['bodies'],model['drive']
    if any(b[k] != 0 for k in ('beam_inertia_kg_m2','beam_first_moment_x_kg_m',
                               'beam_first_moment_y_kg_m','pitman_mass_kg','pitman_inertia_kg_m2')):
        raise ValueError('this independent model does not implement beam/pitman mass')
    rate=math.tau/m['period_s']
    phase=rate*time_s
    theta=m['theta_start_rad']+phase+m['epsilon']*math.sin(phase)
    omega=rate*(1+m['epsilon']*math.cos(phase))
    alpha=-m['epsilon']*rate*rate*math.sin(phase)
    p=position(model,theta)
    v=p['J_m']*omega
    load=model['load']['mean_N']+model['load']['half_span_N']*math.tanh(v/model['load']['transition_speed_m_s'])
    h=.0002
    uprime=sum(w*_potential(model,theta+i*h) for i,w in ((-2,1),(-1,-8),(1,8),(2,-1)))/(12*h)
    inertia=b['crank_inertia_kg_m2']+d['upstream_inertia_kg_m2']
    friction=d['crank_friction_Nm']*math.copysign(1,omega)+d['viscous_friction_Nm_s']*omega
    power=(uprime+inertia*alpha+friction+load*p['J_m'])*omega+d['loss_W']
    if not all(math.isfinite(x) for x in (power,load,v,theta,alpha)):
        raise ValueError('nonfinite forward model')
    return {'theta_rad':theta,'omega_rad_s':omega,'alpha_rad_s2':alpha,
            'displacement_m':p['displacement_m'],'load_N':load,'power_W':power}


def window_power(model,time_s,*,panels=32):
    if type(panels) is not int or panels<2 or panels%2:
        raise ValueError('Simpson panels must be even')
    width=model['motion']['power_window_ns']/1e9
    values=[forward_state(model,time_s-width/2+i*width/panels)['power_W'] for i in range(panels+1)]
    return (values[0]+values[-1]+4*sum(values[1:-1:2])+2*sum(values[2:-1:2]))/(3*panels)


def turn_time(model):
    m=model['motion']
    target=m['theta_start_rad']+(m['theta_top_rad']-m['theta_start_rad'])%math.tau
    lo,hi=0.0,m['period_s']
    for _ in range(50):
        mid=(lo+hi)/2
        phase=math.tau*mid/m['period_s']
        if m['theta_start_rad']+phase+m['epsilon']*math.sin(phase) < target:
            lo=mid
        else:
            hi=mid
    return (lo+hi)/2
