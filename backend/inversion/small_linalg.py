"""Seven-by-four Householder QR on target-relative, scaled time.

The reported condition is ||A||_inf ||A+||_inf of A=[1,x,x²,x³],
x=t/max(abs(t)); A+ is obtained by triangular solves against Q.T. It is a
full rectangular matrix condition estimate, not a ratio of R's diagonals.
No normal equations are formed. Callers subtract exact epochs BEFORE entry.

Numerical speed error is a software estimate under binary64 stable-QR
backward-error assumptions, not a certified physical derivative bound. We use
gamma_k=k*u/(1-k*u), u=2**-53, with k the arithmetic-operation budget of this
fixed QR/solve pipeline. The LAPACK least-squares estimate includes both
matrix/RHS perturbations and residual-dependent condition-squared sensitivity:
gamma_k * (2*kappa_2/cos(phi) + tan(phi)*kappa_2**2). We upper-estimate kappa_2
using ||A||_F ||A+||_F and refuse unresolved perturbations; we do not claim a
rigorous enclosure for this implementation. Reference:
https://www.netlib.org/lapack/lug/node83.html
Unresolved conditioning, underflowed scales, and nonfinite results are refused.
The separate, independently evidenced physical speed bound remains mandatory.
"""

from dataclasses import dataclass
import math


@dataclass(frozen=True)
class CubicFit:
    coefficients: tuple[float, float, float, float]
    residual_rad: float
    condition: float
    time_scale_s: float
    speed_sensitivity_per_s: float
    numerical_speed_error_rad_s: float


def _solve(r, rhs):
    result = [0.0]*4
    for row in range(3, -1, -1):
        if r[row][row] == 0:
            raise ValueError('rank_deficient_motion_fit')
        result[row] = (rhs[row] - math.fsum(r[row][j]*result[j] for j in range(row+1, 4))) / r[row][row]
    return result


def cubic_fit(times_s: tuple[float, ...], angles_rad: tuple[float, ...]) -> CubicFit:
    """Fit seven observations; coefficients refer to t=0 in caller's seconds."""
    if (len(times_s) != 7 or len(angles_rad) != 7
            or not all(math.isfinite(x) for x in times_s + angles_rad)
            or any(a >= b for a, b in zip(times_s, times_s[1:]))):
        raise ValueError('invalid_motion_fit_samples')
    scale = max(map(abs, times_s))
    scale_cubed = scale*scale*scale
    if scale == 0 or scale_cubed == 0 or not math.isfinite(scale_cubed):
        raise ValueError('unresolved_motion_time_scale')
    x = tuple(t/scale for t in times_s)
    a = [[1.0, t, t*t, t*t*t] for t in x]
    r = [row[:] for row in a]
    qt = [[float(i == j) for j in range(7)] for i in range(7)]
    # Keep offsets out of derivative solves. Motion removes exact offsets too.
    offset = angles_rad[0]
    y = [value-offset for value in angles_rad]
    rhs = y[:]
    operations = 7*5  # Time division, power columns and angular subtraction.
    for k in range(4):
        v = [r[i][k] for i in range(k, 7)]
        length = len(v)
        v[0] += math.copysign(math.hypot(*v), v[0])
        norm = math.hypot(*v)
        if norm == 0 or not math.isfinite(norm):
            raise ValueError('rank_deficient_motion_fit')
        v = [value/norm for value in v]
        operations += 2*(2*length+1) + 1 + length
        for matrix, columns in ((r, range(k, 4)), (qt, range(7))):
            for j in columns:
                projection = 2*math.fsum(v[i]*matrix[k+i][j] for i in range(length))
                for i in range(length):
                    matrix[k+i][j] -= projection*v[i]
                operations += 4*length+1
        projection = 2*math.fsum(v[i]*rhs[k+i] for i in range(length))
        for i in range(length):
            rhs[k+i] -= projection*v[i]
        operations += 4*length+1
    scaled = _solve(r, rhs)
    inverse_columns = [_solve(r, [qt[i][j] for i in range(4)]) for j in range(7)]
    inverse = [[inverse_columns[j][i] for j in range(7)] for i in range(4)]
    # Eight back-solves: 6 products + 6 sums + 4 subtractions + 4 divisions.
    operations += 8*20 + 7*8 + 4*7 + 7*4
    condition = max(sum(map(abs, row)) for row in a)*max(sum(map(abs, row)) for row in inverse)
    sensitivity = sum(map(abs, inverse[1]))/scale
    residuals = [math.fsum(c*term for c, term in zip(scaled, row))-value
                 for row, value in zip(a, y)]
    residual = max(map(abs, residuals))
    condition_two = math.hypot(*(v for row in a for v in row))*math.hypot(*(v for row in inverse for v in row))
    gamma = operations*2.0**-53 / (1-operations*2.0**-53)
    unresolved = gamma*condition_two
    if not math.isfinite(unresolved) or unresolved >= 1:
        raise ValueError('unresolved_motion_condition')
    bnorm = math.hypot(*y)
    sin_phi = math.hypot(*residuals)/bnorm if bnorm else 0.0
    if sin_phi >= 1:
        raise ValueError('unresolved_motion_residual')
    cos_phi = math.sqrt((1-sin_phi)*(1+sin_phi))
    relative_error = gamma*(2*condition_two/cos_phi + sin_phi/cos_phi*condition_two**2)
    if not math.isfinite(relative_error) or relative_error >= 1:
        raise ValueError('unresolved_motion_error')
    # Entire coefficient-vector error also bounds its slope component. Convert
    # the relative estimate from true to computed norm via 1-relative_error.
    numerical = relative_error/(1-relative_error)*math.hypot(*scaled)/scale
    coefficients = (scaled[0]+offset, scaled[1]/scale, scaled[2]/(scale*scale), scaled[3]/scale_cubed)
    if not all(math.isfinite(v) for v in (*coefficients, condition, residual, sensitivity, numerical)):
        raise ValueError('nonfinite_motion_fit')
    return CubicFit(coefficients, residual, condition, scale, sensitivity, numerical)
