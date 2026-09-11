"""Deterministic visual cards generated through the inverse torque model.

Profile names describe synthetic shapes, not machine diagnosis labels. In
particular, rod_break does not add a condition to the diagnostic code set.
"""

from dataclasses import asdict
import hashlib
import math
import re

from backend.algorithms.dynagraph import MechanismParams


PROFILE_DEFINITIONS = {
    "normal": {"label": "泵工作正常", "diagnosis_code": "normal"},
    "traveling_valve_leak": {"label": "游动凡尔漏失", "diagnosis_code": "pump_leak"},
    "standing_valve_leak": {"label": "固定凡尔漏失", "diagnosis_code": "pump_leak"},
    "double_valve_leak": {"label": "双凡尔漏失", "diagnosis_code": "pump_leak"},
    "rod_break": {"label": "抽油杆断脱", "diagnosis_code": "empty_pump"},
    "tubing_leak": {"label": "油管漏失", "diagnosis_code": "pump_leak"},
    "supply_shortage": {"label": "供液不足", "diagnosis_code": "supply_shortage"},
    "gas_interference": {"label": "气体影响", "diagnosis_code": "intermittent"},
    "gas_lock": {"label": "气锁", "diagnosis_code": "empty_pump"},
    "discharge_blockage": {"label": "出油管线部分堵塞", "diagnosis_code": "intermittent"},
    "filter_blockage": {"label": "滤沙器/附件堵塞", "diagnosis_code": "supply_shortage"},
    "high_water_cut": {"label": "高含水", "diagnosis_code": "normal"},
    "tight_packing": {"label": "盘根盒过紧", "diagnosis_code": "intermittent"},
    "sand_obstruction": {"label": "砂阻", "diagnosis_code": "intermittent"},
    "partial_plunger_exit": {"label": "活塞部分脱出工作筒", "diagnosis_code": "pump_leak"},
    "upstroke_hang": {"label": "上挂", "diagnosis_code": "intermittent"},
    "downstroke_hang": {"label": "下挂", "diagnosis_code": "intermittent"},
    "stuck_plunger": {"label": "活塞被卡", "diagnosis_code": "intermittent"},
    "loose_liner": {"label": "衬套松动", "diagnosis_code": "intermittent"},
    "flowing_well": {"label": "连抽带喷", "diagnosis_code": "intermittent"},
}
PROFILES = tuple(PROFILE_DEFINITIONS)
PROFILE_ALIASES = {"pump_leak": "traveling_valve_leak", "empty_pump": "gas_lock",
                   "intermittent": "gas_interference"}


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
    original_condition = condition
    condition = PROFILE_ALIASES.get(condition, condition)
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
    samples, sensor_samples = [], []
    for index in range(point_count):
        theta = math.tau * index / point_count
        u = (1.0 - math.cos(theta)) / 2.0
        upper, lower = 1.0 + .025 * u, .025 + .015 * u
        if condition == "supply_shortage":
            lower += .78 * (1.0 + math.tanh((u - .52) / .075)) / 2.0
        elif condition == "gas_interference":
            upper = .60 + .31 * u - .30 * math.exp(-((u - .43) / .13) ** 2)
            lower = .08 + .36 * (1.0 + math.tanh((u - .72) / .10)) / 2.0
        elif condition == "traveling_valve_leak":
            upper, lower = .91 - .40 * u, .10 + .12 * u
        elif condition == "standing_valve_leak":
            upper, lower = .60 + .40 * u, .08 + .42 * u * u
        elif condition == "double_valve_leak":
            upper, lower = .72 - .31 * u, .16 + .13 * u
        elif condition == "tubing_leak":
            upper, lower = .68 + .05 * u, .12 + .08 * math.sin(math.pi * u)
        elif condition == "gas_lock":
            upper, lower = .14 + .03 * math.sin(math.pi * u), .04 + .02 * u
        elif condition == "rod_break":
            upper, lower = -.08 + .025 * u, -.11 + .015 * u
        elif condition == "discharge_blockage":
            upper += .38 * u + .16 * math.exp(-((u - .72) / .13) ** 2)
        elif condition == "filter_blockage":
            upper = .83 - .26 * math.exp(-((u - .28) / .12) ** 2)
            lower += .62 * (1 + math.tanh((u - .35) / .1)) / 2
        elif condition == "high_water_cut":
            upper, lower = 1.14 + .09 * u, .05 + .04 * u
        elif condition == "tight_packing":
            upper += .18 + .07 * math.sin(math.pi * u)
            lower -= .15 + .04 * math.sin(math.pi * u)
        elif condition == "sand_obstruction":
            upper += .17 * math.sin(7 * math.pi * u) + .12 * u
            lower += .12 * math.sin(9 * math.pi * u)
        elif condition == "partial_plunger_exit":
            upper -= .72 * (1 + math.tanh((u - .79) / .035)) / 2
            lower += .14 * u
        elif condition == "upstroke_hang":
            upper += .65 * math.exp(-((u - .86) / .07) ** 2)
        elif condition == "downstroke_hang":
            lower -= .60 * math.exp(-((u - .14) / .07) ** 2)
        elif condition == "stuck_plunger":
            upper, lower = .35 + 1.10 * u, .27 - .90 * u
        elif condition == "loose_liner":
            upper = .79 + .21 * math.sin(4 * math.pi * u)
            lower = .19 + .16 * math.sin(4 * math.pi * u + .7)
        elif condition == "flowing_well":
            upper, lower = .42 + .18 * u, -.05 + .12 * math.sin(math.pi * u)
        # Smooth load transfer at both reversals while retaining separate branches.
        upstroke_weight = (1.0 + math.tanh(math.sin(theta) / .065)) / 2.0
        fraction = lower + (upper - lower) * upstroke_weight
        harmonic = variation * .08 * (math.sin(3 * theta) + .35 * math.sin(5 * theta))
        target_load_kn = params.static_rod_load_kn + params.full_load_span_kn * fraction + harmonic
        sensor_samples.append({
            "phase": 360.0 * index / point_count,
            "displacement": round(params.stroke_m * u * 1.003 + .004 + .002 * math.sin(2 * theta), 6),
            "load": round(target_load_kn * 1.006 + .12 + .07 * math.sin(3 * theta + .2), 6),
        })
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
            "mechanism": asdict(params), "profile": original_condition,
            "profile_code": condition, "profile_label": PROFILE_DEFINITIONS[condition]["label"],
            "sensor_samples": sensor_samples}
