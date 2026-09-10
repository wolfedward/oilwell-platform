"""Explainable hybrid diagnosis for surface dynagraph features.

Template distance provides a reproducible similarity measure while explicit
engineering rules provide operator-readable evidence.  The module is pure and
keeps the machine conclusion separate from any later manual review workflow.
"""

from dataclasses import dataclass, fields
import math
import re

from backend.algorithms.dynagraph import DynagraphFeatures


MODEL_VERSION = "diagnosis-1.0"
_CONDITION_PATTERN = re.compile(r"^[a-z][a-z0-9_]{1,63}$")
_FEATURE_NAMES = tuple(field.name for field in fields(DynagraphFeatures))
_FEATURE_SCALES = {
    "max_load_kn": 15.0,
    "min_load_kn": 15.0,
    "area_kn_m": 30.0,
    "fillage": .04,
    "upstroke_load_kn": 15.0,
    "downstroke_load_kn": 10.0,
    "load_difference_kn": 15.0,
    "top_edge_tilt_kn_per_m": 8.0,
    "bottom_edge_tilt_kn_per_m": 8.0,
    "closure_error": .05,
    "turning_points": 3.0,
}
_FEATURE_WEIGHTS = {
    "max_load_kn": .8,
    "min_load_kn": .5,
    "area_kn_m": 1.4,
    "fillage": 1.6,
    "upstroke_load_kn": .7,
    "downstroke_load_kn": .7,
    "load_difference_kn": 1.2,
    "top_edge_tilt_kn_per_m": .8,
    "bottom_edge_tilt_kn_per_m": .8,
    "closure_error": 1.2,
    "turning_points": .8,
}
_RECOMMENDATIONS = {
    "normal": "保持当前制度并持续观察功图趋势",
    "supply_shortage": "核查供液能力并评估延长间歇停机时间",
    "intermittent": "检查供液波动和启停周期，复测稳定周期功图",
    "pump_leak": "复核游动阀、固定阀密封并安排泵效测试",
    "empty_pump": "立即核查液面、抽空风险和停机保护条件",
}


class DiagnosisValidationError(ValueError):
    """Feature or template data is unsafe for diagnosis."""


@dataclass(frozen=True)
class Template:
    condition: str
    features: DynagraphFeatures
    sample_count: int = 1
    scale_overrides: tuple[tuple[str, float], ...] = ()
    weight_overrides: tuple[tuple[str, float], ...] = ()
    version: str = "1"


@dataclass(frozen=True)
class RuleHit:
    code: str
    condition: str
    description: str
    observed: float
    threshold: float
    severity: float


@dataclass(frozen=True)
class CandidateScore:
    condition: str
    distance: float
    template_score: float
    rule_score: float
    combined_score: float
    sample_count: int
    template_version: str


@dataclass(frozen=True)
class DiagnosisResult:
    condition: str
    confidence: float
    rule_hits: tuple[RuleHit, ...]
    all_rule_hits: tuple[RuleHit, ...]
    candidates: tuple[CandidateScore, ...]
    evidence: tuple[str, ...]
    selected_template_version: str
    recommendation: str
    rule_baseline_version: str
    metric_scales: tuple[tuple[str, float], ...]
    metric_weights: tuple[tuple[str, float], ...]
    model_version: str = MODEL_VERSION


def _finite_number(value, name: str, minimum: float, maximum: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise DiagnosisValidationError(f"{name} must be finite numeric data")
    try:
        result = float(value)
    except (OverflowError, TypeError, ValueError) as error:
        raise DiagnosisValidationError(f"{name} must be finite numeric data") from error
    if not math.isfinite(result):
        raise DiagnosisValidationError(f"{name} must be finite numeric data")
    if not minimum <= result <= maximum:
        raise DiagnosisValidationError(f"{name} is outside the engineering range")
    return result


def _validate_features(value: DynagraphFeatures) -> None:
    if not isinstance(value, DynagraphFeatures):
        raise DiagnosisValidationError("features must be DynagraphFeatures")
    ranges = {
        "max_load_kn": (-10_000.0, 10_000.0),
        "min_load_kn": (-10_000.0, 10_000.0),
        "area_kn_m": (0.0, 100_000.0),
        "fillage": (0.0, 1.0),
        "upstroke_load_kn": (-10_000.0, 10_000.0),
        "downstroke_load_kn": (-10_000.0, 10_000.0),
        "load_difference_kn": (-20_000.0, 20_000.0),
        "top_edge_tilt_kn_per_m": (-10_000.0, 10_000.0),
        "bottom_edge_tilt_kn_per_m": (-10_000.0, 10_000.0),
        "closure_error": (0.0, 100.0),
        "turning_points": (0.0, 10_000.0),
    }
    for name, (minimum, maximum) in ranges.items():
        _finite_number(getattr(value, name), name, minimum, maximum)
    if value.max_load_kn < value.min_load_kn:
        raise DiagnosisValidationError("max_load_kn cannot be lower than min_load_kn")
    if isinstance(value.turning_points, bool) or not isinstance(value.turning_points, int):
        raise DiagnosisValidationError("turning_points must be an integer")


def _validated_overrides(
    pairs: tuple[tuple[str, float], ...], name: str, allow_zero: bool,
) -> dict[str, float]:
    if not isinstance(pairs, tuple):
        raise DiagnosisValidationError(f"{name} must be a tuple")
    result = {}
    for pair in pairs:
        if not isinstance(pair, tuple) or len(pair) != 2 or pair[0] not in _FEATURE_NAMES:
            raise DiagnosisValidationError(f"{name} contains an unknown feature")
        if pair[0] in result:
            raise DiagnosisValidationError(f"{name} contains a duplicate feature")
        minimum = 0.0 if allow_zero else 1e-12
        result[pair[0]] = _finite_number(pair[1], f"{name}.{pair[0]}", minimum, 1_000_000.0)
    return result


def _metric_config(template: Template) -> tuple[dict[str, float], dict[str, float]]:
    scale_overrides = _validated_overrides(
        template.scale_overrides, "scale_overrides", allow_zero=False,
    )
    weight_overrides = _validated_overrides(
        template.weight_overrides, "weight_overrides", allow_zero=True,
    )
    for name, value in scale_overrides.items():
        if not _FEATURE_SCALES[name] * .25 <= value <= _FEATURE_SCALES[name] * 4.0:
            raise DiagnosisValidationError(
                f"scale_overrides.{name} exceeds the allowed default-relative range"
            )
    for name, value in weight_overrides.items():
        if value > _FEATURE_WEIGHTS[name] * 4.0:
            raise DiagnosisValidationError(
                f"weight_overrides.{name} exceeds the allowed default-relative range"
            )
    scales = {**_FEATURE_SCALES, **scale_overrides}
    weights = {**_FEATURE_WEIGHTS, **weight_overrides}
    active_weights = [value for value in weights.values() if value > 0.0]
    total_weight = sum(active_weights)
    if len(active_weights) < 7 or total_weight < sum(_FEATURE_WEIGHTS.values()) * .60:
        raise DiagnosisValidationError("metric configuration retains too few effective features")
    if max(active_weights) / total_weight > .30:
        raise DiagnosisValidationError("one feature cannot dominate the diagnosis metric")
    return scales, weights


def _validate_templates(templates: list[Template]) -> tuple[dict[str, float], dict[str, float]]:
    if not isinstance(templates, list) or len(templates) < 2:
        raise DiagnosisValidationError("at least two templates are required")
    conditions = set()
    for template in templates:
        if not isinstance(template, Template):
            raise DiagnosisValidationError("templates must contain Template values")
        if not isinstance(template.condition, str) or not _CONDITION_PATTERN.fullmatch(template.condition):
            raise DiagnosisValidationError("template condition must be a stable snake-case code")
        if template.condition in conditions:
            raise DiagnosisValidationError("template conditions must be unique")
        conditions.add(template.condition)
        _validate_features(template.features)
        if isinstance(template.sample_count, bool) or not isinstance(template.sample_count, int) or template.sample_count < 1:
            raise DiagnosisValidationError("template sample_count must be a positive integer")
        if not isinstance(template.version, str) or not template.version.strip():
            raise DiagnosisValidationError("template version is required")
        _metric_config(template)
    if "normal" not in conditions:
        raise DiagnosisValidationError("a unique normal template is required as the rule baseline")
    normal = next(template for template in templates if template.condition == "normal")
    if (
        normal.features.area_kn_m <= 1e-9
        or abs(normal.features.load_difference_kn) <= 1e-9
        or normal.features.fillage <= 1e-9
        or normal.features.max_load_kn - normal.features.min_load_kn <= 1e-9
    ):
        raise DiagnosisValidationError("normal template features must be non-degenerate")
    reference_scales, reference_weights = _metric_config(templates[0])
    for template in templates[1:]:
        scales, weights = _metric_config(template)
        if scales != reference_scales or weights != reference_weights:
            raise DiagnosisValidationError(
                "all templates must use one shared metric configuration"
            )
    return reference_scales, reference_weights


def _clamp01(value: float) -> float:
    return max(0.0, min(1.0, value))


def _support_below(value: float, threshold: float, transition: float) -> float:
    return _clamp01((threshold + transition - value) / (2.0 * transition))


def _support_above(value: float, threshold: float, transition: float) -> float:
    return _clamp01((value - threshold + transition) / (2.0 * transition))


def _support_band(value: float, lower: float, upper: float, transition: float) -> float:
    return min(
        _support_above(value, lower, transition),
        _support_below(value, upper, transition),
    )


def _rule_hits(value: DynagraphFeatures, normal: DynagraphFeatures) -> list[RuleHit]:
    area_ratio = value.area_kn_m / max(normal.area_kn_m, 1e-12)
    load_ratio = value.load_difference_kn / max(abs(normal.load_difference_kn), 1e-12)
    fillage_ratio = value.fillage / max(normal.fillage, 1e-12)
    top_tilt_delta = value.top_edge_tilt_kn_per_m - normal.top_edge_tilt_kn_per_m
    bottom_tilt_delta = value.bottom_edge_tilt_kn_per_m - normal.bottom_edge_tilt_kn_per_m
    hits = []

    def add(code, condition, description, observed, threshold, severity):
        bounded = _clamp01(severity)
        if bounded >= .01:
            hits.append(RuleHit(code, condition, description, observed, threshold, bounded))

    add("curve_closure", "intermittent", "曲线首尾闭合误差偏大", value.closure_error, .06, _support_above(value.closure_error, .06, .02))
    add("turning_points", "intermittent", "载荷曲线转折次数偏多", float(value.turning_points), 8.0, _support_above(float(value.turning_points), 8.0, 2.0))

    add("area_loss", "empty_pump", "功图面积相对正常模板显著缩小", area_ratio, .72, _support_below(area_ratio, .72, .05))
    add("load_drop", "empty_pump", "上下冲程载荷差显著下降", load_ratio, .72, .9 * _support_below(load_ratio, .72, .05))

    supply_area = _support_band(area_ratio, .74, .90, .04)
    supply_load = _support_band(load_ratio, .72, .85, .04)
    supply_tilt = _support_above(top_tilt_delta, -.18, .06)
    add("area_loss", "supply_shortage", "功图面积相对正常模板下降", area_ratio, .90, .8 * min(supply_area, supply_load, supply_tilt))
    add("load_drop", "supply_shortage", "有效载荷差下降", load_ratio, .85, .8 * min(supply_area, supply_load, supply_tilt))

    pump_tilt = _support_below(top_tilt_delta, -.15, .06)
    pump_area = _support_below(area_ratio, .92, .05)
    pump_fillage = _support_below(fillage_ratio, .94, .04)
    pump_bottom = _support_below(bottom_tilt_delta, -.20, .08)
    add("top_edge_tilt", "pump_leak", "上载荷边相对正常模板下倾", top_tilt_delta, -.15, min(pump_tilt, pump_area))
    add("low_fillage", "pump_leak", "归一化功图充满系数下降", fillage_ratio, .94, .8 * min(pump_fillage, pump_tilt, pump_area))
    add("bottom_impact", "pump_leak", "下冲程边缘变化相对正常模板加剧", bottom_tilt_delta, -.20, .8 * min(pump_bottom, pump_tilt))

    normal_support = min(
        _support_above(fillage_ratio, .95, .03),
        _support_above(area_ratio, .90, .05),
        _support_above(load_ratio, .90, .05),
        _support_below(value.closure_error, .03, .02),
        _support_below(float(value.turning_points), 8.0, 2.0),
    )
    add("card_stable", "normal", "面积、充满系数和闭合度处于正常模板区间", fillage_ratio, .95, normal_support)
    return hits


def _distance(
    value: DynagraphFeatures, template: Template,
    scales: dict[str, float], weights: dict[str, float],
) -> float:
    total_weight = sum(weights.values())
    if total_weight <= 0.0:
        raise DiagnosisValidationError("template weights must contain a positive value")
    squared = sum(
        weights[name] * ((float(getattr(value, name)) - float(getattr(template.features, name))) / scales[name]) ** 2
        for name in _FEATURE_NAMES
    )
    return math.sqrt(squared / total_weight)


def diagnose(features: DynagraphFeatures, templates: list[Template]) -> DiagnosisResult:
    """Rank condition templates and return traceable rule evidence."""
    _validate_features(features)
    scales, weights = _validate_templates(templates)
    normal_template = next(template for template in templates if template.condition == "normal")
    hits = _rule_hits(features, normal_template.features)
    by_condition = {}
    for hit in hits:
        by_condition.setdefault(hit.condition, []).append(hit)

    candidates = []
    for template in templates:
        distance = _distance(features, template, scales, weights)
        template_score = math.exp(-distance)
        supporting = by_condition.get(template.condition, [])
        rule_score = min(1.0, sum(hit.severity for hit in supporting) / 2.5)
        combined = .90 * template_score + .10 * rule_score
        candidates.append(CandidateScore(
            condition=template.condition,
            distance=distance,
            template_score=template_score,
            rule_score=rule_score,
            combined_score=combined,
            sample_count=template.sample_count,
            template_version=template.version,
        ))
    candidates.sort(key=lambda item: (-item.combined_score, item.distance, item.condition))
    selected = candidates[0]
    selected_hits = tuple(by_condition.get(selected.condition, ()))
    runner_up = candidates[1].combined_score
    separation = max(0.0, selected.combined_score - runner_up)
    confidence = min(.99, max(.0, selected.combined_score * (.92 + min(.08, separation))))
    evidence = (
        f"模板归一化距离 {selected.distance:.4f}",
        f"模板相似度 {selected.template_score:.4f}",
        f"规则支持度 {selected.rule_score:.4f}",
        f"候选领先度 {separation:.4f}",
    )
    return DiagnosisResult(
        condition=selected.condition,
        confidence=confidence,
        rule_hits=selected_hits,
        all_rule_hits=tuple(hits),
        candidates=tuple(candidates),
        evidence=evidence,
        selected_template_version=next(
            template.version for template in templates
            if template.condition == selected.condition
        ),
        recommendation=_RECOMMENDATIONS.get(
            selected.condition, "请结合候选排序、规则证据和现场复测结果处置",
        ),
        rule_baseline_version=normal_template.version,
        metric_scales=tuple((name, scales[name]) for name in _FEATURE_NAMES),
        metric_weights=tuple((name, weights[name]) for name in _FEATURE_NAMES),
    )
