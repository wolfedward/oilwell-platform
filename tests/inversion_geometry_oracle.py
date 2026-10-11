"""Independent synthetic oracle: Cartesian circle intersections and energy.

No production inversion kernels are imported. Derivatives use centered finite
differences of positions/energies, not the production implicit equations.
"""

import math
from decimal import Decimal, localcontext

from tests.inversion_vectors import parameter_body


def synthetic_body():
    body = parameter_body()
    body['geometry'].update(pivot_x_m='3', pivot_y_m='2', crank_radius_m='0.5',
                            rear_arm_m='2', pitman_length_m='3', horsehead_radius_m='2')
    body['uncertainty'].update(numerical_budget_Nm='0.000001',
                               numerical_factor_budget_m='0.00000001')
    body['bodies']['component_ledger'] = [
        {'id': owner, 'name': owner, 'owner': owner}
        for owner in ('upstream', 'crank', 'beam', 'pitman')]
    return body


def _decimal_sincos(theta):
    pi = Decimal('3.1415926535897932384626433832795028841971693993751058209749445923')
    x = theta % (2*pi)
    if x > pi:
        x -= 2*pi
    sn, cs, st, ct = x, Decimal(1), x, Decimal(1)
    for n in range(1, 100):
        st *= -x*x/((2*n)*(2*n+1))
        ct *= -x*x/((2*n-1)*(2*n))
        sn += st
        cs += ct
        if max(abs(st), abs(ct)) < Decimal('1e-55'):
            return sn, cs
    raise AssertionError('oracle Taylor series failed to converge')


def _small_atan(x):
    # All uses compare neighbouring orientations (well inside |x|<0.01).
    if abs(x) >= Decimal('0.01'):
        raise AssertionError('oracle angular step too large')
    term, result = x, x
    for n in range(1, 30):
        term *= -x*x
        result += term/(2*n+1)
        if abs(term) < Decimal('1e-55'):
            return result
    raise AssertionError('oracle atan series failed to converge')


def position(body, theta, *, decimal=False):
    g = body['geometry']
    number = Decimal if decimal else float
    sqrt = (lambda x: x.sqrt()) if decimal else math.sqrt
    bx, by, r, c, ell = (number(g[k]) for k in (
        'pivot_x_m', 'pivot_y_m', 'crank_radius_m', 'rear_arm_m', 'pitman_length_m'))
    sn, cs = _decimal_sincos(theta) if decimal else (math.sin(theta), math.cos(theta))
    px, py = g['rotation_sign'] * r * sn, r * cs
    # Radical axis of the two circles, solved as a Cartesian quadratic.
    dx, dy = px - bx, py - by
    rhs = (c*c - ell*ell + dx*dx + dy*dy)/2
    if abs(dx) >= abs(dy):
        u, v = rhs/dx, -dy/dx
        aa, bb, cc = 1+v*v, 2*u*v, u*u-c*c
        ys = [(-bb + sign*sqrt(bb*bb-4*aa*cc))/(2*aa) for sign in (-1, 1)]
        candidates = [(u+v*y, y) for y in ys]
    else:
        u, v = rhs/dy, -dx/dy
        aa, bb, cc = 1+v*v, 2*u*v, u*u-c*c
        xs = [(-bb + sign*sqrt(bb*bb-4*aa*cc))/(2*aa) for sign in (-1, 1)]
        candidates = [(x, u+v*x) for x in xs]
    rx, ry = next((x+bx, y+by) for x, y in candidates
                  if g['branch']*(dx*y-dy*x) > 0)
    lam = number(body['bodies']['pitman_com_fraction'])
    return {'P': (px, py), 'R': (rx, ry),
            'C': (px+lam*(rx-px), py+lam*(ry-py)),
            'beta': math.atan2(ry-by, rx-bx), 'psi': math.atan2(ry-py, rx-px)}


def angle_difference(a, b):
    return math.atan2(math.sin(a-b), math.cos(a-b))


def derivatives(body, theta, h=1e-5):
    # 55 digits keep cancellation in the required h=1e-5 second difference
    # below the test tolerance. This does not reuse analytic kernel derivatives.
    with localcontext() as context:
        context.prec = 55
        h, theta = Decimal(str(h)), Decimal.from_float(theta)
        minus, center, plus = [position(body, theta+t, decimal=True) for t in (-h, 0, h)]
        pivot = tuple(Decimal(body['geometry'][key]) for key in ('pivot_x_m', 'pivot_y_m'))
        def vector(p, key):
            origin = pivot if key == 'beta' else p['P']
            return tuple(p['R'][i]-origin[i] for i in (0, 1))
        def relative(p, key):
            u, v = vector(center, key), vector(p, key)
            return _small_atan((u[0]*v[1]-u[1]*v[0])/(u[0]*v[0]+u[1]*v[1]))
        result = {}
        for key in ('beta', 'psi'):
            left, right = relative(minus, key), relative(plus, key)
            result[key+'_prime'] = float((right-left)/(2*h))
            result[key+'_second'] = float((right+left)/(h*h))
        for key in ('P', 'R', 'C'):
            result[key+'_prime'] = tuple(float((plus[key][i]-minus[key][i])/(2*h)) for i in (0, 1))
        return result


def potential(body, theta):
    b, g = body['bodies'], body['geometry']
    p = position(body, theta)
    # Independently rotate mass first moments in world coordinates.
    turn = -g['rotation_sign']*theta
    crank_y = (float(b['crank_first_moment_x_kg_m'])*math.sin(turn)
               + float(b['crank_first_moment_y_kg_m'])*math.cos(turn))
    beam_y = (float(b['beam_first_moment_x_kg_m'])*math.sin(p['beta'])
              + float(b['beam_first_moment_y_kg_m'])*math.cos(p['beta']))
    return float(b['gravity_m_s2'])*(crank_y+beam_y+float(b['pitman_mass_kg'])*p['C'][1])


def inertia(body, theta):
    b = body['bodies']
    # Five-point derivatives reduce the nested-difference error in M'.
    h = 0.0002
    p = {i: position(body, theta+i*h) for i in (-2, -1, 0, 1, 2)}
    angular = {key: sum(weight*angle_difference(p[i][key], p[0][key])
                        for i, weight in ((-2, 1), (-1, -8), (1, 8), (2, -1)))/(12*h)
               for key in ('beta', 'psi')}
    velocity = [sum(weight*p[i]['C'][axis] for i, weight in
                    ((-2, 1), (-1, -8), (1, 8), (2, -1)))/(12*h) for axis in (0, 1)]
    return (float(b['crank_inertia_kg_m2'])
            + float(b['beam_inertia_kg_m2'])*angular['beta']**2
            + float(b['pitman_mass_kg'])*sum(v*v for v in velocity)
            + float(b['pitman_inertia_kg_m2'])*angular['psi']**2)


def energy_derivatives(body, theta):
    h = 0.0002
    def first(function):
        return sum(weight*function(body, theta+i*h) for i, weight in
                   ((-2, 1), (-1, -8), (1, 8), (2, -1)))/(12*h)
    return first(potential), inertia(body, theta), first(inertia)


def high_precision_energy(body, theta):
    """Decimal position/energy differences for numerical-enclosure checks.

    This verifies the synthetic software cases, not arbitrary platform libm.
    All first derivatives use h=1e-12; the nested M' difference retains over
    15 digits beyond binary64 in a 65-digit Decimal working context.
    """
    with localcontext() as context:
        context.prec = 65
        theta, h = Decimal.from_float(theta), Decimal('1e-12')
        b = {k: Decimal(v) for k, v in body['bodies'].items()
             if k not in ('component_ledger', 'evidence_refs')}
        g = body['geometry']
        pivot = tuple(Decimal(g[k]) for k in ('pivot_x_m', 'pivot_y_m'))
        def height(t):
            p = position(body, t, decimal=True)
            sn, cs = _decimal_sincos(t)
            crank = -g['rotation_sign']*b['crank_first_moment_x_kg_m']*sn+b['crank_first_moment_y_kg_m']*cs
            beam = (b['beam_first_moment_x_kg_m']*(p['R'][1]-pivot[1])
                    + b['beam_first_moment_y_kg_m']*(p['R'][0]-pivot[0]))/Decimal(g['rear_arm_m'])
            return b['gravity_m_s2']*(crank+beam+b['pitman_mass_kg']*p['C'][1])
        def kinetic(t):
            left, right = position(body, t-h, decimal=True), position(body, t+h, decimal=True)
            def angular(key):
                origin1 = pivot if key == 'beta' else left['P']
                origin2 = pivot if key == 'beta' else right['P']
                u = tuple(left['R'][i]-origin1[i] for i in (0, 1))
                v = tuple(right['R'][i]-origin2[i] for i in (0, 1))
                return _small_atan((u[0]*v[1]-u[1]*v[0])/(u[0]*v[0]+u[1]*v[1]))/(2*h)
            velocities = [(right['C'][i]-left['C'][i])/(2*h) for i in (0, 1)]
            return (b['crank_inertia_kg_m2']+b['beam_inertia_kg_m2']*angular('beta')**2
                    + b['pitman_inertia_kg_m2']*angular('psi')**2
                    + b['pitman_mass_kg']*sum(v*v for v in velocities))
        return tuple(float(value) for value in (
            (height(theta+h)-height(theta-h))/(2*h), kinetic(theta),
            (kinetic(theta+h)-kinetic(theta-h))/(2*h)))


def deadpoints(body):
    # Independent bounded search of oracle derivatives; no measured samples.
    def derivative(theta):
        return derivatives(body, theta)['beta_prime']
    roots = []
    for i in range(360):
        lo, hi = i*math.tau/360, (i+1)*math.tau/360
        left, right = derivative(lo), derivative(hi)
        if left*right >= 0:
            continue
        for _ in range(48):
            middle = (lo+hi)/2
            if left*derivative(middle) <= 0:
                hi = middle
            else:
                lo, left = middle, derivative(middle)
        roots.append((lo+hi)/2)
    return roots
