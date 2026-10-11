"""Nonnegative dissipative loss on the signed electric-power calibration grid."""
from bisect import bisect_right
from dataclasses import dataclass
from fractions import Fraction
import math

from .geometry import _require_parameters
from .parameter_contracts import ParameterSet


def _fraction(value):
    try:
        return Fraction(value)
    except (ValueError, OverflowError, TypeError) as error:
        raise ValueError('nonfinite_solver_input') from error


def _finite(value):
    result = float(value)
    if not math.isfinite(result):
        raise ValueError('nonfinite_solver_result')
    return result


def _upper(value):
    """Round an exact nonnegative bound outward, retaining exact zero."""
    result = _finite(value)
    if Fraction(result) < value:
        result = math.nextafter(result, math.inf)
    return _finite(result)


@dataclass(frozen=True)
class LossPoint:
    loss_W: float
    error_W: float
    numerical_error_W: float


def lookup_loss(parameters: ParameterSet, power_W: float,
                motor_speed_rad_s: float) -> LossPoint:
    """Bilinear interpolation, with exact rational arithmetic then one rounding.

    error_W is the interpolated calibration envelope, already covered by the
    approved torque envelope. numerical_error_W covers only output rounding.
    Both axes include their endpoints; extrapolation is never performed.
    """
    _require_parameters(parameters)
    grid = parameters.drive.loss_grid
    power, speed = _fraction(power_W), _fraction(motor_speed_rad_s)
    xs = tuple(map(_fraction, grid.power_W))
    ys = tuple(map(_fraction, grid.motor_speed_rad_s))
    if not xs[0] <= power <= xs[-1] or not ys[0] <= speed <= ys[-1]:
        raise ValueError('loss_outside_domain')
    i = min(bisect_right(xs, power)-1, len(xs)-2)
    j = min(bisect_right(ys, speed)-1, len(ys)-2)
    x = (power-xs[i])/(xs[i+1]-xs[i])
    y = (speed-ys[j])/(ys[j+1]-ys[j])
    def interpolate(matrix):
        return ((1-x)*((1-y)*_fraction(matrix[i][j])+y*_fraction(matrix[i][j+1]))
                + x*((1-y)*_fraction(matrix[i+1][j])+y*_fraction(matrix[i+1][j+1])))
    loss, error = interpolate(grid.loss_W), interpolate(grid.error_W)
    if loss < 0 or error < 0:
        raise ValueError('negative_loss_envelope')
    rounded = _finite(loss)
    return LossPoint(rounded, _upper(error), _upper(abs(loss-Fraction(rounded))))
