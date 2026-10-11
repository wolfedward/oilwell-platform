"""Bounded, descriptive motor-current spectrum and conservative sideband evidence."""

from datetime import datetime
import math
from statistics import median

from .contracts import StudyError


def _object(value, keys):
    if type(value) is not dict or set(value) != set(keys):
        raise StudyError("invalid_field", "missing or unknown spectrum field")
    return value


def _number(value, name, low, high, *, positive=False, optional=False):
    if optional and value is None:
        return None
    if type(value) not in (int, float) or value < low or value > high:
        raise StudyError("invalid_field", f"invalid {name}")
    # Range comparisons precede conversion: very large Python ints cannot overflow here.
    value = float(value)
    if not math.isfinite(value) or (positive and value <= 0):
        raise StudyError("invalid_field", f"invalid {name}")
    return value


def _text(value, name, optional=False):
    if optional and value is None:
        return None
    if type(value) is not str or not value.strip() or len(value) > 240:
        raise StudyError("invalid_field", f"invalid {name}")
    return value


def _boolean(value, name):
    if type(value) is not bool:
        raise StudyError("invalid_field", f"invalid {name}")
    return value


def _timestamp(value):
    if type(value) is not str or len(value) > 64:
        raise StudyError("invalid_field", "invalid started_at")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        raise StudyError("invalid_field", "invalid started_at") from None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise StudyError("invalid_field", "started_at requires timezone")


def _fft(values):
    """In-place iterative radix-2 complex FFT, then retain the real-input half."""
    a = [complex(value) for value in values]
    n = len(a)
    j = 0
    for i in range(1, n):
        bit = n >> 1
        while j & bit:
            j ^= bit
            bit >>= 1
        j ^= bit
        if i < j:
            a[i], a[j] = a[j], a[i]
    size = 2
    while size <= n:
        half = size // 2
        rotation = complex(math.cos(-2 * math.pi / size), math.sin(-2 * math.pi / size))
        for start in range(0, n, size):
            twiddle = 1 + 0j
            for offset in range(half):
                top = a[start + offset]
                bottom = twiddle * a[start + offset + half]
                a[start + offset] = top + bottom
                a[start + offset + half] = top - bottom
                twiddle *= rotation
        size <<= 1
    return a[: n // 2 + 1]


def _reason(reasons, code, message):
    reasons.append({"code": code, "message": message})


def analyze_spectrum(payload):
    _object(payload, ("schema", "capture", "motor", "policy"))
    if payload["schema"] != "motor.spectrum/1":
        raise StudyError("unsupported_schema", "unsupported motor spectrum schema")
    capture = _object(payload["capture"], ("capture_id", "well_id", "device_id", "started_at", "sample_rate_hz", "samples_A", "quality", "clipped", "provenance"))
    for key in ("capture_id", "well_id", "device_id"):
        _text(capture[key], key)
    _timestamp(capture["started_at"])
    fs = _number(capture["sample_rate_hz"], "sample_rate_hz", 1, 100000)
    samples = capture["samples_A"]
    if type(samples) is not list or len(samples) < 256 or len(samples) > 65536 or len(samples) & (len(samples) - 1):
        raise StudyError("invalid_field", "samples_A must have power-of-two length 256..65536")
    n = len(samples)
    if n / fs > 60:
        raise StudyError("invalid_field", "capture duration exceeds 60 seconds")
    samples = [_number(x, "samples_A", -100000, 100000) for x in samples]
    quality, clipped = capture["quality"], capture["clipped"]
    if type(quality) is not list or len(quality) != n or any(type(x) is not str or x not in ("valid", "invalid", "unknown") for x in quality):
        raise StudyError("invalid_field", "quality must match samples")
    if type(clipped) is not list or len(clipped) != n or any(type(x) is not bool for x in clipped):
        raise StudyError("invalid_field", "clipped must match samples")
    provenance = _object(capture["provenance"], ("sample_kind", "uniform_sampling_confirmed", "clock_error_ppm", "calibration_reference", "anti_alias_reference", "anti_alias_passband_hz", "manifest_sha256"))
    if provenance["sample_kind"] not in ("instantaneous", "rms", "unknown") or type(provenance["sample_kind"]) is not str:
        raise StudyError("invalid_field", "invalid sample_kind")
    _boolean(provenance["uniform_sampling_confirmed"], "uniform_sampling_confirmed")
    clock_error = _number(provenance["clock_error_ppm"], "clock_error_ppm", 0, 100000, optional=True)
    _text(provenance["calibration_reference"], "calibration_reference", optional=True)
    _text(provenance["anti_alias_reference"], "anti_alias_reference", optional=True)
    passband = _number(provenance["anti_alias_passband_hz"], "anti_alias_passband_hz", 0, fs / 2, positive=True, optional=True)
    if passband == fs / 2:
        raise StudyError("invalid_field", "anti_alias_passband_hz must be below Nyquist")
    manifest = provenance["manifest_sha256"]
    if manifest is not None and (type(manifest) is not str or len(manifest) != 64 or any(c not in "0123456789abcdef" for c in manifest)):
        raise StudyError("invalid_field", "invalid manifest_sha256")
    motor = _object(payload["motor"], ("type", "supply", "pole_count", "speed_rpm", "speed_uncertainty_rpm", "supply_frequency_hz", "frequency_uncertainty_hz", "steady_state_declared", "periodic_load_excluded", "basis_reference"))
    if type(motor["type"]) is not str or motor["type"] not in ("squirrel_cage_induction", "other", "unknown"):
        raise StudyError("invalid_field", "invalid motor type")
    if type(motor["supply"]) is not str or motor["supply"] not in ("line_fed", "inverter_fed", "unknown"):
        raise StudyError("invalid_field", "invalid supply")
    poles = motor["pole_count"]
    if poles is not None and (type(poles) is not int or poles < 2 or poles > 64 or poles % 2):
        raise StudyError("invalid_field", "invalid pole_count")
    speed = _number(motor["speed_rpm"], "speed_rpm", 0, 120000, optional=True)
    speed_error = _number(motor["speed_uncertainty_rpm"], "speed_uncertainty_rpm", 0, 120000, optional=True)
    frequency = _number(motor["supply_frequency_hz"], "supply_frequency_hz", 0, 1000, positive=True, optional=True)
    frequency_error = _number(motor["frequency_uncertainty_hz"], "frequency_uncertainty_hz", 0, 1000, optional=True)
    _boolean(motor["steady_state_declared"], "steady_state_declared")
    _boolean(motor["periodic_load_excluded"], "periodic_load_excluded")
    _text(motor["basis_reference"], "basis_reference", optional=True)
    policy = _object(payload["policy"], ("maximum_clock_error_ppm", "maximum_rms_variation_ratio", "minimum_fundamental_A", "minimum_sideband_dbc", "minimum_sideband_to_noise_db"))
    max_clock = _number(policy["maximum_clock_error_ppm"], "maximum_clock_error_ppm", 0, 100000, positive=True)
    max_variation = _number(policy["maximum_rms_variation_ratio"], "maximum_rms_variation_ratio", 0, 1)
    min_main = _number(policy["minimum_fundamental_A"], "minimum_fundamental_A", 0, 100000, positive=True)
    min_dbc = _number(policy["minimum_sideband_dbc"], "minimum_sideband_dbc", -160, 0)
    min_snr = _number(policy["minimum_sideband_to_noise_db"], "minimum_sideband_to_noise_db", 0, 100)

    dc = math.fsum(samples) / n
    centered = [x - dc for x in samples]
    ac_rms = math.sqrt(math.fsum(x * x for x in centered) / n)
    rms_windows = [math.sqrt(math.fsum(x * x for x in centered[i:i + n // 8]) / (n // 8)) for i in range(0, n, n // 8)]
    mean_window = math.fsum(rms_windows) / 8
    variation = (max(rms_windows) - min(rms_windows)) / mean_window if mean_window else 0.0
    windowed = [centered[i] * (0.5 - 0.5 * math.cos(2 * math.pi * i / n)) for i in range(n)]
    spectrum_half = _fft(windowed)
    window_sum = n / 2  # exact sum for a periodic Hann window of power-of-two N
    amplitudes = [abs(value) * (1 if i in (0, n // 2) else 2) / window_sum for i, value in enumerate(spectrum_half)]
    df = fs / n
    frequencies = [i * df for i in range(n // 2 + 1)]
    reasons = []
    if any(x != "valid" for x in quality):
        _reason(reasons, "sample_quality", "at least one sample has invalid or unknown quality")
    if any(clipped):
        _reason(reasons, "clipped_samples", "at least one sample is clipped")
    if provenance["sample_kind"] != "instantaneous":
        _reason(reasons, "not_instantaneous", "instantaneous current samples required")
    if not provenance["uniform_sampling_confirmed"]:
        _reason(reasons, "sampling_unverified", "uniform sampling not declared")
    if clock_error is None or clock_error > max_clock:
        _reason(reasons, "clock_unverified", "declared clock uncertainty missing or above policy")
    if provenance["calibration_reference"] is None:
        _reason(reasons, "calibration_unverified", "declared calibration reference missing")
    if provenance["anti_alias_reference"] is None or passband is None:
        _reason(reasons, "anti_alias_unverified", "declared anti-alias reference/passband missing")
    if motor["type"] != "squirrel_cage_induction":
        _reason(reasons, "unsupported_motor", "squirrel-cage induction motor required")
    if motor["supply"] != "line_fed":
        _reason(reasons, "unsupported_supply", "line-fed supply required for v1")
    if not motor["steady_state_declared"] or variation > max_variation:
        _reason(reasons, "nonsteady", "steady operation not supported by declaration/windows")
    if None in (poles, speed, speed_error, frequency, frequency_error):
        _reason(reasons, "missing_motor_basis", "speed, poles, frequency, and uncertainty required")
    if motor["basis_reference"] is None:
        _reason(reasons, "missing_motor_basis_reference", "motor basis reference missing")
    confounders = []
    if not motor["periodic_load_excluded"] or motor["basis_reference"] is None:
        confounders.append("periodic_load_not_excluded")

    features = None
    if None not in (poles, speed, speed_error, frequency, frequency_error):
        sync = 120 * frequency / poles
        if sync == 0:
            raise StudyError("numeric_overflow", "synchronous speed underflow")
        slip = (sync - speed) / sync
        minus = -frequency + speed * poles / 60
        plus = 3 * frequency - speed * poles / 60
        if not all(math.isfinite(value) for value in (sync, slip, minus, plus)):
            raise StudyError("numeric_overflow", "derived motor frequency overflow")
        features = {"synchronous_speed_rpm": sync, "slip": slip, "nominal_sideband_hz": [minus, plus]}
        if not 0 < slip < 0.5:
            _reason(reasons, "invalid_slip", "supported motoring slip must be in (0, 0.5)")
        else:
            clock_fraction = (clock_error or 0) / 1e6
            def interval(center, error):
                spread = error + abs(center) * clock_fraction + 2 * df
                return [center - spread, center + spread]
            main_range = interval(frequency, frequency_error)
            minus_range = interval(minus, frequency_error + speed_error * poles / 60)
            plus_range = interval(plus, 3 * frequency_error + speed_error * poles / 60)
            intervals = [minus_range, main_range, plus_range]
            features["search_intervals_hz"] = {"lower": minus_range, "fundamental": main_range, "upper": plus_range}
            upper_bound = min(fs / 2, passband) if passband is not None else fs / 2
            if any(lo <= 0 or hi >= upper_bound for lo, hi in intervals) or not (minus_range[1] < main_range[0] and main_range[1] < plus_range[0]):
                _reason(reasons, "unresolvable_sidebands", "expanded Hann/uncertainty intervals overlap or exceed measured passband")
            else:
                selected = [[i for i, hz in enumerate(frequencies) if lo <= hz <= hi] for lo, hi in intervals]
                if any(not indices for indices in selected):
                    _reason(reasons, "missing_search_bins", "expanded interval contains no observed FFT bin")
                else:
                    peaks = [max((amplitudes[i], i) for i in indices) for indices in selected]
                    main = peaks[1][0]
                    features["peak_frequency_hz"] = {name: frequencies[peak[1]] for name, peak in zip(("lower", "fundamental", "upper"), peaks)}
                    features["peak_amplitude_A"] = {name: peak[0] for name, peak in zip(("lower", "fundamental", "upper"), peaks)}
                    if main < min_main:
                        _reason(reasons, "weak_fundamental", "fundamental below explicit minimum")
                    excluded = set().union(*map(set, selected))
                    side_data = {}
                    for name, peak in (("lower", peaks[0]), ("upper", peaks[2])):
                        vicinity = [amplitudes[i] for i in range(max(1, peak[1] - 10), min(n // 2, peak[1] + 10) + 1) if i not in excluded]
                        noise = median(vicinity) if len(vicinity) >= 3 else None
                        dbc = 20 * math.log10(peak[0] / main) if peak[0] > 0 and main > 0 else None
                        snr = 20 * math.log10(peak[0] / noise) if noise is not None and noise > 0 and peak[0] > 0 else None
                        side_data[name] = {"dbc": dbc, "noise_A": noise, "signal_to_noise_db": snr}
                        if noise is None or noise == 0:
                            _reason(reasons, "noise_floor_unavailable", f"{name} local noise floor unavailable")
                    features["sidebands"] = side_data
    support = "not_applicable" if motor["supply"] == "inverter_fed" else ("spectrum_only" if reasons else "sideband_features_supported")
    finding = "indeterminate"
    if support == "sideband_features_supported":
        finding = "sideband_features_present" if all(features["sidebands"][side]["dbc"] is not None and features["sidebands"][side]["dbc"] >= min_dbc and features["sidebands"][side]["signal_to_noise_db"] is not None and features["sidebands"][side]["signal_to_noise_db"] >= min_snr for side in ("lower", "upper")) else "no_resolved_sideband"
    if support != "sideband_features_supported":
        features = None  # unsupported inputs cannot emit quantitative broken-bar features
    return {
        "status": "completed", "algorithm_version": "motor_spectrum/1", "support": support,
        "finding": finding, "reasons": reasons,
        "spectrum": {"sample_count": n, "duration_s": n / fs, "sample_rate_hz": fs,
                     "window": "periodic_hann", "removed_dc_A": dc, "ac_rms_A": ac_rms,
                     "rms_variation_ratio": variation, "bin_spacing_hz": df,
                     "frequency_hz": frequencies, "amplitude_A_peak": amplitudes,
                     "amplitude_basis": "declared_calibration_not_verified_traceability"},
        "motor_features": features, "confounders": confounders,
        "fault_confirmation": False, "field_validation": False, "control_eligible": False,
    }
