"""Pure SI geometry for a conventional oscillating beam and circular horsehead.

O=(0,0), B=(bx,by), y is upward. Theta zero is crank UP; theta increases
in the actual rotation direction: d=+1 clockwise, -1 counterclockwise.
P=(d*r*sin(theta), r*cos(theta)). Branch is the oriented side of B->P on
which R lies. Neither encoder zero nor vertical crank position is a deadpoint.
Crank first moments are world-axis moments at theta=0, subsequently rotated
with the crank. Beam first moments use local +x along the rear arm, local +y
counterclockwise from it. Pitman COM fraction is measured from P toward R.

Numerics: every arithmetic operation carries an outward-rounded binary64
interval, including Decimal conversion, closure, derivatives and displacement
normalization. Transcendental enclosures assume the platform libm is accurate
within one ulp; they are software numerical estimates under that assumption,
NOT certified field measurement bounds. Trig argument intervals use the global
unit Lipschitz bound. No physical length, determinant or radicand is clamped.
The policy numerical_margin is the minimum allowed |sin(psi-beta)| over the
whole revolution (dimensionless); its reciprocal is the derivative condition.
The supplied numerical_factor_budget_m bounds errors in J and s independently.
"""

from dataclasses import dataclass, field
from decimal import Decimal
import math
from typing import ClassVar

from .parameter_contracts import ParameterSet


NUMERICAL_ASSUMPTIONS = (
    'IEEE-754 binary64 basic arithmetic with gradual underflow and outward nextafter rounding',
    'Conditional platform libm assumption: sin/cos on [-2*pi,2*pi], atan, atan2, acos, asin and sqrt within one ulp; not guaranteed by Python',
    'Numerical enclosures are conditional software estimates, not field measurement uncertainty or a libm certification',
)


class NumericalModelError(ValueError):
    """A parameter or numerical result cannot support the mechanical model."""


def _down(x):
    return math.nextafter(x, -math.inf)


def _up(x):
    return math.nextafter(x, math.inf)


@dataclass(frozen=True)
class _N:
    """Nominal value and interval for exact-input numerical error propagation."""
    v: float
    lo: float
    hi: float

    def __post_init__(self):
        if not all(math.isfinite(x) for x in (self.v, self.lo, self.hi)) or not self.lo <= self.v <= self.hi:
            raise NumericalModelError('nonfinite_or_unresolved_numerics')

    @classmethod
    def exact(cls, value):
        value = float(value)
        return cls(value, value, value)

    @classmethod
    def decimal(cls, value):
        if not isinstance(value, Decimal) or not value.is_finite():
            raise NumericalModelError('nonfinite_parameter')
        v = float(value)
        if not math.isfinite(v):
            raise NumericalModelError('parameter_outside_binary64')
        represented = Decimal.from_float(v)
        return cls(v, _down(v) if represented > value else v,
                   _up(v) if represented < value else v)

    @staticmethod
    def of(value):
        return value if isinstance(value, _N) else _N.exact(value)

    @property
    def error(self):
        return _up(max(self.v-self.lo, self.hi-self.v))

    def __add__(self, other):
        other = self.of(other)
        return _N(self.v+other.v, _down(self.lo+other.lo), _up(self.hi+other.hi))

    __radd__ = __add__

    def __neg__(self):
        return _N(-self.v, -self.hi, -self.lo)

    def __sub__(self, other):
        return self + -self.of(other)

    def __rsub__(self, other):
        return self.of(other) + -self

    def __mul__(self, other):
        other = self.of(other)
        ends = [a*b for a in (self.lo, self.hi) for b in (other.lo, other.hi)]
        return _N(self.v*other.v, _down(min(ends)), _up(max(ends)))

    __rmul__ = __mul__

    def __truediv__(self, other):
        other = self.of(other)
        if other.lo <= 0 <= other.hi:
            raise NumericalModelError('unresolved_denominator')
        ends = [a/b for a in (self.lo, self.hi) for b in (other.lo, other.hi)]
        return _N(self.v/other.v, _down(min(ends)), _up(max(ends)))

    def square(self):
        ends = (self.lo*self.lo, self.hi*self.hi)
        lower = 0.0 if self.lo <= 0 <= self.hi else _down(min(ends))
        return _N(self.v*self.v, lower, _up(max(ends)))

    def sqrt(self):
        if self.lo < 0:
            raise NumericalModelError('unresolved_square_root')
        lower = 0.0 if self.lo == 0 else _down(math.sqrt(self.lo))
        return _N(math.sqrt(self.v), lower, _up(math.sqrt(self.hi)))

    def acos(self):
        if not -1 < self.lo <= self.hi < 1:
            raise NumericalModelError('tangent_or_unresolved_circle_intersection')
        return _N(math.acos(self.v), _down(math.acos(self.hi)), _up(math.acos(self.lo)))

    def atan(self):
        return _N(math.atan(self.v), _down(math.atan(self.lo)), _up(math.atan(self.hi)))

    def trig(self, function):
        argument = self
        if abs(self.v) > math.pi:
            turns = round(self.v/math.tau)
            if abs(turns) > 2**52:
                raise NumericalModelError('angle_range_reduction_unresolved')
            tau = _N(math.tau, _down(math.tau), _up(math.tau))
            argument = self-turns*tau
        if max(abs(argument.lo), abs(argument.hi)) > math.tau:
            raise NumericalModelError('angle_range_reduction_unresolved')
        value = function(argument.v)
        error = argument.error
        # Intersect with the mathematical codomain, never change nominal geometry.
        return _N(value, max(-1.0, _down(_down(value)-error)),
                  min(1.0, _up(_up(value)+error)))


def _atan2(y, x):
    value = math.atan2(y.v, x.v)
    # Any point in the coordinate rectangle lies within this disk; tangents
    # from the origin give the enclosing angular deviation asin(error/radius).
    radius = math.hypot(x.v, y.v)
    error = _up(math.hypot(x.error, y.error))
    if radius == 0 or error >= _down(radius):
        raise NumericalModelError('unresolved_orientation')
    angle_error = _up(math.asin(_up(error/_down(radius))))
    return _N(value, _down(_down(value)-angle_error), _up(_up(value)+angle_error))


def _dot(a, b):
    return a[0]*b[0]+a[1]*b[1]


def _cross(a, b):
    return a[0]*b[1]-a[1]*b[0]


def _add(a, b):
    return a[0]+b[0], a[1]+b[1]


def _sub(a, b):
    return a[0]-b[0], a[1]-b[1]


def _scale(a, k):
    return a[0]*k, a[1]*k


def _squared_norm(a):
    return a[0].square()+a[1].square()


def _require_parameters(parameters):
    if (not isinstance(parameters, ParameterSet) or parameters.is_draft or parameters.missing
            or any(getattr(parameters, group) is None for group in (
                'geometry', 'bodies', 'drive', 'domain', 'uncertainty', 'policy', 'phase', 'provenance'))):
        raise NumericalModelError('incomplete_parameters')


@dataclass(frozen=True)
class _Kinematics:
    P: tuple[_N, _N]
    R: tuple[_N, _N]
    C: tuple[_N, _N]
    C_prime: tuple[_N, _N]
    C_second: tuple[_N, _N]
    arm: tuple[_N, _N]
    relative_beta: _N
    beta_prime: _N
    beta_second: _N
    psi_prime: _N
    psi_second: _N
    sin_theta: _N
    cos_theta: _N
    condition: float


@dataclass(frozen=True)
class GeometryPoint:
    s_m: float
    J_m: float
    beta_rad: float
    beta_prime: float
    beta_second: float
    P: tuple[float, float]
    R: tuple[float, float]
    C: tuple[float, float]
    psi_prime: float
    psi_second: float
    derivative_condition: float
    closure_residual_m: float
    numerical_factor_error_m: float
    numerical_displacement_error_m: float
    _kinematics: _Kinematics = field(repr=False, compare=False)


@dataclass(frozen=True)
class BeamGeometry:
    numerical_assumptions: ClassVar[tuple[str, ...]] = NUMERICAL_ASSUMPTIONS
    parameters: ParameterSet = field(repr=False)
    stroke_m: float
    bottom_theta_rad: float
    top_theta_rad: float
    minimum_branch_sine: float
    _values: tuple[_N, ...] = field(repr=False)
    _reference_angle: _N = field(repr=False)
    _bottom_beta: _N = field(repr=False)

    def _state(self, theta):
        bx, by, r, c, ell, a, lam = self._values
        specification = self.parameters.geometry
        d, branch = specification.rotation_sign, specification.branch
        sn, cs = theta.trig(math.sin), theta.trig(math.cos)
        P = (d*r*sn, r*cs)
        P1, P2 = (d*r*cs, -r*sn), (-P[0], -P[1])
        q = (P[0]-bx, P[1]-by)
        distance = _squared_norm(q).sqrt()
        cos_gamma = (distance.square()+c.square()-ell.square())/(2*distance*c)
        sin_gamma = (1-cos_gamma.square()).sqrt()
        arm = ((q[0]*cos_gamma-branch*q[1]*sin_gamma)*c/distance,
               (q[1]*cos_gamma+branch*q[0]*sin_gamma)*c/distance)
        R = (bx+arm[0], by+arm[1])
        w, tangent = _sub(R, P), (-arm[1], arm[0])
        orientation = _cross(q, arm)*branch
        if orientation.lo <= 0:
            raise NumericalModelError('branch_unresolved')
        denominator = _dot(w, tangent)
        sine = denominator/(c*ell)
        min_sine = min(abs(sine.lo), abs(sine.hi)) if sine.lo*sine.hi > 0 else 0
        if min_sine <= float(self.parameters.policy.numerical_margin):
            raise NumericalModelError('ill_conditioned_assembly')
        beta1 = _dot(w, P1)/denominator
        R1 = _scale(tangent, beta1)
        w1 = _sub(R1, P1)
        beta2 = (_dot(w, P2)-_squared_norm(w1)+_dot(w, arm)*beta1.square())/denominator
        R2 = _sub(_scale(tangent, beta2), _scale(arm, beta1.square()))
        w2 = _sub(R2, P2)
        C = _add(P, _scale(w, lam))
        C1, C2 = _add(P1, _scale(w1, lam)), _add(P2, _scale(w2, lam))
        psi1, psi2 = _cross(w, w1)/ell.square(), _cross(w, w2)/ell.square()
        # Since |B|>r, q stays in the open half-plane around -B. atan of
        # cross/dot is continuous even when the world atan2 branch cut is crossed.
        base = (-bx, -by)
        projection = _dot(base, q)
        if projection.lo <= 0:
            raise NumericalModelError('nonrocking_assembly')
        relative_beta = (_cross(base, q)/projection).atan()+branch*cos_gamma.acos()
        return _Kinematics(P, R, C, C1, C2, arm, relative_beta, beta1, beta2,
                           psi1, psi2, sn, cs, 1/min_sine)

    def at(self, theta_rad: float) -> GeometryPoint:
        """Evaluate any finite angle without history-dependent branch switching.

        A zero J is a normal load deadpoint and is returned unchanged. A singular
        loop, unresolved branch, or exceeded numerical budget raises ValueError.
        """
        theta = _N.exact(theta_rad)
        state = self._state(theta)
        a, sign = self._values[5], self.parameters.geometry.displacement_sign
        s = sign*a*(state.relative_beta-self._bottom_beta)
        J = sign*a*state.beta_prime
        beta = self._reference_angle+state.relative_beta
        budget = _N.decimal(self.parameters.uncertainty.numerical_factor_budget_m)
        if max(s.error, J.error) > budget.lo:
            raise NumericalModelError('geometry_numerical_budget_exceeded')
        point = lambda vector: tuple(value.v for value in vector)
        P, R, C = point(state.P), point(state.R), point(state.C)
        bx, by, _, c, ell, _, _ = self._values
        residual = max(abs(math.dist(R, P)-ell.v), abs(math.dist(R, (bx.v, by.v))-c.v))
        return GeometryPoint(s.v, J.v, beta.v, state.beta_prime.v, state.beta_second.v,
                             P, R, C, state.psi_prime.v, state.psi_second.v,
                             state.condition, residual, J.error, s.error, state)


def build_geometry(parameters: ParameterSet) -> BeamGeometry:
    """Validate the entire revolution, then derive the two physical deadpoints.

    Extrema are enumerated algebraically: at beta'=0 the pitman and crank are
    collinear, R=(r +/- l)u(theta). Each signed radius supplies two candidates,
    so the search has a fixed upper bound of four evaluations, no angular grid,
    and no reference curve. Root intervals propagate into the zero displacement.
    A candidate must enclose beta'=0 or satisfy the fixed 1e-12 rad/rad residual
    tolerance. Both roots must belong to the configured branch.
    """
    _require_parameters(parameters)
    g, bodies = parameters.geometry, parameters.bodies
    values = tuple(_N.decimal(value) for value in (
        g.pivot_x_m, g.pivot_y_m, g.crank_radius_m, g.rear_arm_m,
        g.pitman_length_m, g.horsehead_radius_m, bodies.pitman_com_fraction))
    bx, by, r, c, ell, a, lam = values
    if (min(r.lo, c.lo, ell.lo, a.lo) <= 0 or not 0 <= lam.lo <= lam.hi <= 1
            or any(type(sign) is not int or sign not in (-1, 1)
                   for sign in (g.branch, g.rotation_sign, g.displacement_sign))):
        raise NumericalModelError('invalid_geometry_parameters')
    margin = _N.decimal(parameters.policy.numerical_margin)
    if not 0 <= margin.lo <= margin.hi < 1:
        raise NumericalModelError('invalid_numerical_margin')
    distance = (bx.square()+by.square()).sqrt()
    if distance.lo <= r.hi:
        raise NumericalModelError('nonrocking_nonperiodic_assembly')
    nearest, farthest = distance-r, distance+r
    difference = c-ell
    inner = max(abs(difference.lo), abs(difference.hi))
    if nearest.lo <= inner or farthest.hi >= (c+ell).lo:
        raise NumericalModelError('full_rotation_circles_tangent_separated_or_contained')
    # Triangle angle cosine is monotonic in |B-P|²; its largest absolute value
    # occurs at one of the two distance endpoints. This checks the entire cycle.
    sines = []
    for endpoint in (nearest, farthest):
        cosine = (c.square()+ell.square()-endpoint.square())/(2*c*ell)
        sine = (1-cosine.square()).sqrt()
        sines.append(sine.lo)
    minimum_sine = min(sines)
    if minimum_sine <= margin.hi:
        raise NumericalModelError('ill_conditioned_full_rotation')
    reference = _atan2(-by, -bx)
    temporary = BeamGeometry(parameters, 0, 0, 0, minimum_sine, values, reference, _N.exact(0))
    origin_phase = _atan2(g.rotation_sign*bx, by)
    tau = _N(math.tau, _down(math.tau), _up(math.tau))
    candidates = []
    for radius in (r+ell, r-ell):
        cosine = (distance.square()+radius.square()-c.square())/(2*radius*distance)
        if cosine.lo > 1 or cosine.hi < -1:
            continue
        offset = cosine.acos()
        for sign in (-1, 1):
            theta = origin_phase+sign*offset
            if theta.v < 0:
                theta = theta+tau
            elif theta.v >= math.tau:
                theta = theta-tau
            sn, cs = theta.trig(math.sin), theta.trig(math.cos)
            direction = (g.rotation_sign*sn, cs)
            P = _scale(direction, r)
            R = _scale(direction, radius)
            orientation = _cross(_sub(P, (bx, by)), _sub(R, (bx, by)))*g.branch
            if orientation.hi < 0:
                continue
            if orientation.lo <= 0:
                raise NumericalModelError('extremum_branch_unresolved')
            state = temporary._state(theta)
            if abs(state.beta_prime.v) > max(1e-12, state.beta_prime.error):
                raise NumericalModelError('extremum_not_resolved')
            candidates.append((theta, state.relative_beta))
    if len(candidates) != 2:
        raise NumericalModelError('two_rocker_extrema_required')
    candidates.sort(key=lambda item: g.displacement_sign*item[1].v)
    bottom, top = candidates
    stroke = g.displacement_sign*a*(top[1]-bottom[1])
    if stroke.lo <= 0:
        raise NumericalModelError('zero_or_unresolved_stroke')
    geometry = BeamGeometry(parameters, stroke.v, bottom[0].v, top[0].v,
                            minimum_sine, values, reference, bottom[1])
    for angle in (0, bottom[0].v, top[0].v):
        geometry.at(angle)
    return geometry
