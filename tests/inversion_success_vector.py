"""Offline-only synthetic WP05A numerical vector; never a device observation.

Test-only native vector; inversion_e2e_support archives its actual bytes and
binds independently reviewed evidence. This does not certify field accuracy.
"""

from fractions import Fraction
from hashlib import sha256
import math

from backend.inversion.comparison import compare
from backend.inversion.contracts import canonical_bytes
from backend.inversion.geometry import build_geometry
from backend.inversion.parameter_contracts import parse_parameter_set
from backend.inversion.series import CycleWindow, PredictionInputs, ReferenceInputs, Sample, SampleSeries, ChannelPin
from backend.inversion.solver import predict
from tests.inversion_geometry_oracle import derivatives, synthetic_body
from tests.inversion_vectors import CHANNEL, parameter_wire


COUNT = 129
RATE_HZ = 64
CYCLE_NS = 2_000_000_000
CALIBRATION_TIME_CORRECTION_NS = 17
POWER_WINDOW_NS = 1_000_000
# Model bottom-stroke crank orientation, rounded to a declared encoder phase.
CRANK_START_RAD = 4.686898939910908


def _continuous_forward_power_W(body, theta):
    """Synthetic continuous power from a declared load, never from references."""
    omega = math.pi
    synthetic_load_N = 100 + 10 * math.sin(theta)
    J = 2 * derivatives(body, theta)['beta_prime']
    # U'=-30*cos(theta); downstream M=2, upstream I=3, alpha=0.
    # Includes 4 Nm Coulomb and 0.5 Nm.s viscous friction plus 7 W loss.
    return (-30 * math.cos(theta) + 4 + .5 * omega
            + synthetic_load_N * J) * omega + 7


def _centered_mean_power_W(body, theta_center, panels):
    """Fixed composite Simpson mean on the declared 1 ms centered interval."""
    assert panels in (8, 16)
    width_s = POWER_WINDOW_NS / 1_000_000_000
    weighted = 0.0
    for k in range(panels + 1):
        offset_s = width_s * (k / panels - .5)
        weight = 1 if k in (0, panels) else 4 if k % 2 else 2
        weighted += weight * _continuous_forward_power_W(body, theta_center + math.pi * offset_s)
    return weighted / (3 * panels)


def parameters_for_vector():
    """Explicit software-test model, with hashes matching its amended body."""
    body = synthetic_body()
    body['phase']['unwrap_kind'] = 'unwrapped'
    body['phase']['zero_reading_rad'] = str(-CRANK_START_RAD)
    body['bodies'].update(
        crank_inertia_kg_m2='2', crank_first_moment_x_kg_m='3',
        beam_inertia_kg_m2='0', pitman_mass_kg='0', pitman_inertia_kg_m2='0',
        gravity_m_s2='10',
    )
    body['drive'].update(
        upstream_inertia_kg_m2='3', crank_friction_Nm='4',
        viscous_friction_Nm_s='0.5',
    )
    body['drive']['loss_grid'].update(
        power_W=['-1000', '1000'], motor_speed_rad_s=['1', '100'],
        loss_W=[['7', '7'], ['7', '7']],
        error_W=[['0.001', '0.001'], ['0.001', '0.001']],
    )
    body['domain'].update(
        power_W={'min': '-1000', 'max': '1000'},
        speed_rad_s={'min': '0.1', 'max': '10'},
        acceleration_rad_s2={'min': '-10', 'max': '10'},
        temperature_K={'min': '250', 'max': '350'},
        supply_voltage_V={'min': '300', 'max': '500'},
        supply_frequency_Hz={'min': '45', 'max': '55'},
        load_N={'min': '0', 'max': '1000'},
        window_ns={'min': '1', 'max': '10000000'},
        bandwidth_Hz={'min': '1', 'max': '1000'},
    )
    body['uncertainty'].update(
        torque_bound_Nm='0.1', factor_bound_m='0.02',
        speed_bound_rad_s='0', numerical_budget_Nm='0.0001',
        numerical_factor_budget_m='0.00000001',
    )
    policy = body['policy']['body']
    policy.update(
        motion_max_time_span_ns='1000000000',
        motion_max_angle_span_rad='0.5',
        motion_max_residual_rad='0.0001',
        motion_max_condition='10000', max_load_error_N='1000',
        min_coverage_fraction='0.8', reference_max_gap_ns='100000000',
        fill_max_time_span_ns='50000000', fill_max_angle_span_rad='0.2',
    )
    body['policy']['content_sha256'] = sha256(canonical_bytes(policy)).hexdigest()
    body['provenance']['coverage']['policy_sha256'] = body['policy']['content_sha256']
    body['provenance']['coverage']['domain_sha256'] = sha256(canonical_bytes(body['domain'])).hexdigest()
    body['provenance']['coverage']['uncertainty_sha256'] = sha256(canonical_bytes(body['uncertainty'])).hexdigest()
    return parse_parameter_set(parameter_wire(body))


def synthetic_native_cycle():
    """Return exact native indices/times and all four *raw i32* channels.

    Calibrations: power W = raw/100+1; angle rad = raw/1e6;
    load kN = raw/1000 (hence SI load N = raw); displacement m = raw/1e6.
    Every channel shares one 64 Hz clock and +17 ns time correction.
    Power is the 16-panel Simpson average of a continuous synthetic forward
    function over each centered 1 ms window, followed by native quantization.
    Eight-panel means must quantize identically; their maximum numeric
    difference is returned, not asserted as a rigorous quadrature bound.
    Reference channels derive from the same synthetic mechanical construction;
    they are not independent measurements or field evidence.
    """
    parameters = parameters_for_vector()
    body = synthetic_body()
    body['geometry'].update(
        pivot_x_m='3', pivot_y_m='2', crank_radius_m='0.5',
        rear_arm_m='2', pitman_length_m='3', horsehead_radius_m='2',
    )
    geometry = build_geometry(parameters)
    assert abs(geometry.bottom_theta_rad - CRANK_START_RAD) < 1e-12
    indices = tuple(range(COUNT))
    times_ns = tuple(Fraction(CALIBRATION_TIME_CORRECTION_NS)
                     + Fraction(i * 1_000_000_000, RATE_HZ) for i in indices)
    power_raw, angle_raw, load_raw, displacement_raw = [], [], [], []
    quadrature_differences_W = []
    for i in indices:
        theta = CRANK_START_RAD + math.tau * i / (COUNT - 1)
        synthetic_load_N = 100 + 10 * math.sin(theta)
        coarse_mean_W = _centered_mean_power_W(body, theta, 8)
        power_W = _centered_mean_power_W(body, theta, 16)
        quadrature_differences_W.append(abs(power_W - coarse_mean_W))
        assert round((coarse_mean_W - 1) * 100) == round((power_W - 1) * 100)
        power_raw.append(round((power_W - 1) * 100))
        angle_raw.append(round((theta - CRANK_START_RAD) * 1_000_000))
        load_raw.append(round(synthetic_load_N))
        displacement_raw.append(round(geometry.at(theta).s_m * 1_000_000))
    return dict(
        source_kind='synthetic', indices=indices, times_ns=times_ns,
        cycle_start_ns=times_ns[0], cycle_turn_ns=times_ns[COUNT // 2],
        cycle_end_ns=times_ns[-1], sample_rate_hz=RATE_HZ,
        calibration_time_correction_ns=CALIBRATION_TIME_CORRECTION_NS,
        power_window_ns=POWER_WINDOW_NS,
        quadrature_panels=16,
        max_8_to_16_panel_mean_difference_W=max(quadrature_differences_W),
        cycle_event_indices=(0, COUNT // 2, COUNT - 1),
        raw_encoding='i32le', quality_flags=(0,) * COUNT,
        calibration_si=dict(active_power=(Fraction(1, 100), Fraction(1)),
                            crank_angle=(Fraction(1, 1_000_000), Fraction(0)),
                            load=(Fraction(1), Fraction(0)),
                            displacement=(Fraction(1, 1_000_000), Fraction(0))),
        wire_units=dict(active_power='W', crank_angle='rad', load='kN', displacement='m'),
        signal_kinds=dict(active_power='window_mean', crank_angle='instantaneous',
                          load='instantaneous', displacement='instantaneous'),
        active_power_raw=tuple(power_raw), crank_angle_raw=tuple(angle_raw),
        load_raw=tuple(load_raw), displacement_raw=tuple(displacement_raw),
    )


def pure_numerical_check():
    """Run existing pure kernels with reference channels excluded from predict."""
    native = synthetic_native_cycle()
    parameters = parameters_for_vector()
    source = ChannelPin.from_wire(CHANNEL | {'last_index': COUNT - 1})
    def series(role, raw, scale, offset=0):
        return SampleSeries(role, tuple(Sample(t, i, Fraction(v) * scale + offset, 0)
                                        for i, t, v in zip(native['indices'], native['times_ns'], raw)), source)
    power = series('active_power', native['active_power_raw'], Fraction(1, 100), 1)
    angle = series('crank_angle', native['crank_angle_raw'], Fraction(1, 1_000_000))
    load = series('load', native['load_raw'], Fraction(1))
    displacement = series('displacement', native['displacement_raw'], Fraction(1, 1_000_000))
    cycle = CycleWindow(native['cycle_start_ns'], native['cycle_end_ns'], 'synthetic-cycle')
    prediction = predict(PredictionInputs(power, angle, cycle, POWER_WINDOW_NS, Fraction(0)),
                         parameters, parameters.policy)
    comparison = compare(prediction, ReferenceInputs(load, displacement), parameters.policy)
    return native, parameters, prediction, comparison
