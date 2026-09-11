"""Deterministic visual cards generated through the inverse torque model.

Profile names describe synthetic shapes, not machine diagnosis labels. In
particular, rod_break does not add a condition to the diagnostic code set.
"""

from dataclasses import asdict
import hashlib
import math
import re

from backend.algorithms.dynagraph import MechanismParams


PROFILES = ("normal", "supply_shortage", "intermittent", "pump_leak", "empty_pump", "rod_break")


def profile_condition(device_id: str) -> str:
    """Allocate consecutive device numbers evenly, preserving namespace suffixes."""
    suffix = re.search(r"(\d+)$", device_id)
    index = int(suffix[1]) - 1 if suffix else int.from_bytes(
        hashlib.sha256(device_id.encode("utf-8")).digest()[:4], "big",
    )
    return PROFILES[index % len(PROFILES)]


def build_profile_card(
    condition: str, mechanism: dict, *, variation: float = 0.0, point_count: int = 120,
) -> dict:
    """Return one electric cycle whose torque recovers the selected load shape.

    Current is an RMS magnitude; a negative active power sample represents
    regeneration (negative signed power factor), with |pf| fixed at 0.88.
    """
    if condition not in PROFILES:
        raise ValueError("unknown visual profile")
    if isinstance(point_count, bool) or not isinstance(point_count, int) or not 8 <= point_count <= 512:
        raise ValueError("point_count must be an integer from 8 to 512")
    if not math.isfinite(variation):
        raise ValueError("variation must be finite")
    variation = max(-1.0, min(1.0, variation))
    params = MechanismParams(**{
        "motor_efficiency": .91, "gear_ratio": 30.0,
        "transmission_efficiency": .86, "balance_torque_nm": 180.0,
        "balance_phase_deg": 8.0, "static_rod_load_kn": 36.0,
        "full_load_span_kn": 25.0, **mechanism,
        "torque_factor_m": mechanism["stroke_m"] / 2.0,
    })
    radius = params.stroke_m / 2.0
    omega_motor = math.tau * params.strokes_per_minute / 60.0 * params.gear_ratio
    balance_phase = math.radians(params.balance_phase_deg)
    samples = []
    for index in range(point_count):
        theta = math.tau * index / point_count
        u = (1.0 - math.cos(theta)) / 2.0
        upper, lower = 1.0 + .025 * u, .025 + .015 * u
        if condition == "supply_shortage":
            lower += .78 * (1.0 + math.tanh((u - .52) / .075)) / 2.0
        elif condition == "intermittent":
            upper = .60 + .31 * u - .30 * math.exp(-((u - .43) / .13) ** 2)
            lower = .08 + .36 * (1.0 + math.tanh((u - .72) / .10)) / 2.0
        elif condition == "pump_leak":
            upper, lower = .91 - .40 * u, .10 + .12 * u
        elif condition == "empty_pump":
            upper, lower = .14 + .03 * math.sin(math.pi * u), .04 + .02 * u
        elif condition == "rod_break":
            upper, lower = -.08 + .025 * u, -.11 + .015 * u
        # Smooth load transfer at both reversals while retaining separate branches.
        upstroke_weight = (1.0 + math.tanh(math.sin(theta) / .065)) / 2.0
        fraction = lower + (upper - lower) * upstroke_weight
        harmonic = variation * .08 * (math.sin(3 * theta) + .35 * math.sin(5 * theta))
        target_load_kn = params.static_rod_load_kn + params.full_load_span_kn * fraction + harmonic
        net_torque_nm = (target_load_kn - params.static_rod_load_kn) * 1000.0 * radius * math.sin(theta)
        balance_torque_nm = params.balance_torque_nm * math.sin(theta + balance_phase)
        power_kw = (net_torque_nm + balance_torque_nm) * omega_motor / (
            1000.0 * params.gear_ratio * params.transmission_efficiency * params.motor_efficiency
        )
        voltage_v = 380.0
        current_a = abs(power_kw) * 1000.0 / (math.sqrt(3.0) * voltage_v * .88)
        samples.append({
            "phase": 360.0 * index / point_count,
            "current": round(current_a, 6), "voltage": voltage_v,
            "power": round(power_kw, 6),
        })
    return {"kind": "electric_card", "electric_samples": samples,
            "mechanism": asdict(params), "profile": condition}
