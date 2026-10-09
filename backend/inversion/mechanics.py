"""Downstream rigid-body energy, with no rod/fluid or upstream inertia.

The component ledger assigns each physical component to exactly one aggregate;
the three approved aggregate groups are counted once each, never once per ledger
entry. Inertia about the crank/beam pivots already includes their parallel-axis
terms. Pitman inertia is about its COM, with translation added separately.

Numerical torque error combines interval errors in U', M and M' as
e_U + max(|alpha|)*e_M + 0.5*max(|omega|)^2*e_Mprime, using the approved motion
domain. This is a binary64/libm numerical estimate (see geometry), not a field
measurement bound. The separately supplied uncertainty budget gates this result.
"""

from dataclasses import dataclass
from typing import ClassVar

from .geometry import (BeamGeometry, NumericalModelError, NUMERICAL_ASSUMPTIONS,
                       _N, _dot, _squared_norm, _require_parameters)
from .parameter_contracts import ParameterSet


@dataclass(frozen=True)
class EnergyTerms:
    numerical_assumptions: ClassVar[tuple[str, ...]] = NUMERICAL_ASSUMPTIONS
    U_prime_Nm: float
    M_kg_m2: float
    M_prime_kg_m2: float
    numerical_torque_error_Nm: float


def energy_terms(geometry: BeamGeometry, theta_rad: float, parameters: ParameterSet) -> EnergyTerms:
    """Evaluate gravity and kinetic-energy coefficients with respect to theta."""
    _require_parameters(parameters)
    if not isinstance(geometry, BeamGeometry) or geometry.parameters != parameters:
        raise NumericalModelError('geometry_parameter_mismatch')
    b = parameters.bodies
    names = ('crank_inertia_kg_m2', 'crank_first_moment_x_kg_m', 'crank_first_moment_y_kg_m',
             'beam_inertia_kg_m2', 'beam_first_moment_x_kg_m', 'beam_first_moment_y_kg_m',
             'pitman_mass_kg', 'pitman_com_fraction', 'pitman_inertia_kg_m2', 'gravity_m_s2')
    ic, cx, cy, ib, bx, by, mass, lam, ip, gravity = (_N.decimal(getattr(b, name)) for name in names)
    if min(ic.lo, ib.lo, mass.lo, ip.lo) < 0 or gravity.lo <= 0 or not 0 <= lam.lo <= lam.hi <= 1:
        raise NumericalModelError('invalid_body_parameters')
    ids = [component.id for component in b.component_ledger]
    if not ids or len(ids) != len(set(ids)):
        raise NumericalModelError('duplicate_component_ownership')
    point = geometry.at(theta_rad)
    k = point._kinematics
    d, c = parameters.geometry.rotation_sign, geometry._values[3]
    # World y first moment: -d*cx*sin(theta)+cy*cos(theta).
    crank_height_prime = -d*cx*k.cos_theta-cy*k.sin_theta
    # Beam local x is rear arm; local y is its counterclockwise normal.
    beam_height_prime = (bx*k.arm[0]/c-by*k.arm[1]/c)*k.beta_prime
    up = gravity*(crank_height_prime+beam_height_prime+mass*k.C_prime[1])
    inertia = (ic+ib*k.beta_prime.square()+mass*_squared_norm(k.C_prime)
               + ip*k.psi_prime.square())
    inertia_prime = (2*ib*k.beta_prime*k.beta_second+2*mass*_dot(k.C_prime, k.C_second)
                     + 2*ip*k.psi_prime*k.psi_second)
    speed = _N.decimal(max(abs(parameters.domain.speed_rad_s.min), abs(parameters.domain.speed_rad_s.max)))
    acceleration = _N.decimal(max(abs(parameters.domain.acceleration_rad_s2.min),
                                   abs(parameters.domain.acceleration_rad_s2.max)))
    error = (_N.exact(up.error)+acceleration*inertia.error
             + 0.5*speed.square()*inertia_prime.error).hi
    budget = _N.decimal(parameters.uncertainty.numerical_budget_Nm)
    if error > budget.lo:
        raise NumericalModelError('energy_numerical_budget_exceeded')
    return EnergyTerms(up.v, inertia.v, inertia_prime.v, error)
