"""Server-side operational reports computed from traceable source rows."""

from dataclasses import asdict, dataclass
from datetime import date, datetime, time, timedelta, timezone
import csv
import io
import json
import math
import re
from statistics import median


SHANGHAI = timezone(timedelta(hours=8))
UTC = timezone.utc
MAX_REPORT_RANGE = timedelta(days=366)
MAX_POWER_HOLD = timedelta(hours=6)
MAX_INFERRED_POWER_HOLD = timedelta(hours=2)
MIN_COMPARISON_COVERAGE = 0.95
SQLITE_MAX_INTEGER = 9_223_372_036_854_775_807
ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")
STATUS_PATTERN = re.compile(r"^[a-z][a-z_]{0,39}$")
REPORT_FILTERS = {
    "production": {"unit_id", "well_id", "from", "to"},
    "diagnosis": {"unit_id", "well_id", "from", "to", "status"},
    "data-quality": {"unit_id", "well_id", "from", "to", "status"},
    "strategy": {"unit_id", "well_id", "from", "to", "status"},
    "energy": {"unit_id", "well_id", "strategy_id", "from", "to"},
    "work-orders": {"unit_id", "well_id", "from", "to", "status", "assignee_id"},
}
EFFECTIVE_STATES = {"active", "effective", "paused", "rollback", "rolled_back"}


class ReportValidationError(ValueError):
    pass


class ReportNotFound(LookupError):
    pass


@dataclass(frozen=True)
class EnergyEffect:
    strategy_id: str
    strategy_version_id: str
    well_id: str
    well_name: str
    unit_id: str
    unit_name: str
    effective_at: str
    baseline_from: str
    baseline_to: str
    evaluation_from: str
    evaluation_to: str
    baseline_energy_kwh: float
    after_energy_kwh: float
    energy_saved_kwh: float | None
    saving_rate: float | None
    annualized_saving_kwh: float | None
    baseline_runtime_hours: float
    after_runtime_hours: float
    baseline_liquid_m3: float
    after_liquid_m3: float
    liquid_change_rate: float | None
    baseline_production_coverage_rate: float
    after_production_coverage_rate: float
    liquid_valid_for_comparison: bool
    liquid_validation_issues: tuple[str, ...]
    baseline_start_count: int
    after_start_count: int
    baseline_coverage_rate: float
    after_coverage_rate: float
    predicted_energy_kwh: float | None
    predicted_liquid_m3: float | None
    predicted_saving_rate: float | None
    valid_for_comparison: bool
    validation_issues: tuple[str, ...]
    source_tables: tuple[str, ...]


def _round(value, digits=6):
    return round(float(value or 0), digits)


class ReportService:
    """Compute every report on demand; stored report payloads are never trusted."""

    def __init__(self, database, clock=None):
        self.database = database
        self.clock = clock or (lambda: datetime.now(SHANGHAI))

    def report(self, report_type: str, filters: dict | None = None) -> dict:
        normalized = self._filters(report_type, filters or {})
        builders = {
            "production": self._production,
            "diagnosis": self._diagnosis,
            "data-quality": self._data_quality,
            "strategy": self._strategy,
            "energy": self._energy,
            "work-orders": self._work_orders,
        }
        result = builders[report_type](normalized)
        return {
            "report_type": report_type,
            "generated_at": self._now().isoformat(timespec="milliseconds"),
            "timezone": "Asia/Shanghai (+08:00)",
            "filters": self._public_filters(normalized),
            **result,
        }

    def energy_effect(
        self, strategy_id: str, from_time: datetime, to_time: datetime
    ) -> EnergyEffect:
        self._identifier(strategy_id, "strategy_id")
        start = self._aware_datetime(from_time, "from_time")
        end = self._aware_datetime(to_time, "to_time")
        self._range(start, end)
        strategy = self._strategy_row(strategy_id)
        if strategy["status"] not in EFFECTIVE_STATES or not strategy["effective_at"]:
            raise ReportValidationError("strategy has not reached an effective state")
        effective = self._parse_timestamp(strategy["effective_at"], "effective_at")
        if start < effective:
            raise ReportValidationError("evaluation window must start at or after effective_at")
        period_end, _ = self._effective_period_end(strategy)
        if period_end is not None and end > period_end:
            raise ReportValidationError("evaluation window exceeds the strategy effective period")
        duration = end - start
        baseline_end = effective
        baseline_start = baseline_end - duration
        before_power = self._power_stats(strategy["well_id"], baseline_start, baseline_end)
        after_power = self._power_stats(strategy["well_id"], start, end)
        before_production = self._production_stats(
            strategy["well_id"], baseline_start, baseline_end
        )
        after_production = self._production_stats(strategy["well_id"], start, end)
        baseline_energy = before_power["energy_kwh"]
        after_energy = after_power["energy_kwh"]
        window_hours = duration.total_seconds() / 3600
        baseline_coverage = min(1.0, before_power["coverage_hours"] / window_hours)
        after_coverage = min(1.0, after_power["coverage_hours"] / window_hours)
        issues = []
        if baseline_coverage < MIN_COMPARISON_COVERAGE:
            issues.append("baseline_power_coverage_below_95_percent")
        if after_coverage < MIN_COMPARISON_COVERAGE:
            issues.append("evaluation_power_coverage_below_95_percent")
        if baseline_energy <= 0:
            issues.append("baseline_energy_is_zero")
        valid = not issues
        saved = baseline_energy - after_energy if valid else None
        saving_rate = saved / baseline_energy if valid else None
        baseline_liquid = before_production["liquid_m3"]
        after_liquid = after_production["liquid_m3"]
        baseline_production_coverage = min(
            1.0, before_production["coverage_hours"] / window_hours
        )
        after_production_coverage = min(
            1.0, after_production["coverage_hours"] / window_hours
        )
        liquid_issues = []
        if baseline_production_coverage < MIN_COMPARISON_COVERAGE:
            liquid_issues.append("baseline_production_coverage_below_95_percent")
        if after_production_coverage < MIN_COMPARISON_COVERAGE:
            liquid_issues.append("evaluation_production_coverage_below_95_percent")
        if baseline_liquid <= 0:
            liquid_issues.append("baseline_liquid_is_zero")
        liquid_valid = not liquid_issues
        liquid_change = (
            (after_liquid - baseline_liquid) / baseline_liquid
            if liquid_valid else None
        )
        days = duration.total_seconds() / 86400
        annualized = saved / days * 365 if valid and days > 0 else None
        predicted = self._selected_candidate(strategy["configuration"])
        return EnergyEffect(
            strategy_id=strategy["id"],
            strategy_version_id=strategy["current_version_id"],
            well_id=strategy["well_id"],
            well_name=strategy["well_name"],
            unit_id=strategy["unit_id"],
            unit_name=strategy["unit_name"],
            effective_at=effective.astimezone(SHANGHAI).isoformat(),
            baseline_from=baseline_start.astimezone(SHANGHAI).isoformat(),
            baseline_to=baseline_end.astimezone(SHANGHAI).isoformat(),
            evaluation_from=start.astimezone(SHANGHAI).isoformat(),
            evaluation_to=end.astimezone(SHANGHAI).isoformat(),
            baseline_energy_kwh=_round(baseline_energy),
            after_energy_kwh=_round(after_energy),
            energy_saved_kwh=None if saved is None else _round(saved),
            saving_rate=None if saving_rate is None else _round(saving_rate),
            annualized_saving_kwh=None if annualized is None else _round(annualized),
            baseline_runtime_hours=_round(before_power["runtime_hours"]),
            after_runtime_hours=_round(after_power["runtime_hours"]),
            baseline_liquid_m3=_round(baseline_liquid),
            after_liquid_m3=_round(after_liquid),
            liquid_change_rate=None if liquid_change is None else _round(liquid_change),
            baseline_production_coverage_rate=_round(baseline_production_coverage),
            after_production_coverage_rate=_round(after_production_coverage),
            liquid_valid_for_comparison=liquid_valid,
            liquid_validation_issues=tuple(liquid_issues),
            baseline_start_count=before_power["start_count"],
            after_start_count=after_power["start_count"],
            baseline_coverage_rate=_round(baseline_coverage),
            after_coverage_rate=_round(after_coverage),
            predicted_energy_kwh=self._number(predicted.get("predicted_energy_kwh")),
            predicted_liquid_m3=self._number(predicted.get("predicted_liquid_m3")),
            predicted_saving_rate=self._number(predicted.get("saving_rate")),
            valid_for_comparison=valid,
            validation_issues=tuple(issues),
            source_tables=(
                "telemetry", "production_daily", "energy_strategies", "strategy_versions",
                "device_commands",
            ),
        )

    def csv(self, report_type: str, filters: dict | None = None) -> bytes:
        report = self.report(report_type, filters)
        output = io.StringIO(newline="")
        writer = csv.writer(output, lineterminator="\r\n")
        writer.writerow(("report_type", report_type))
        writer.writerow(("generated_at", report["generated_at"]))
        writer.writerow(("timezone", report["timezone"]))
        writer.writerow(("source_tables", "|".join(report["source_tables"])))
        for key, value in report["filters"].items():
            writer.writerow((f"filter.{key}", self._csv_value(value)))
        for key, value in report["summary"].items():
            writer.writerow((f"summary.{key}", self._csv_value(value)))
        writer.writerow(())
        items = report["items"]
        if items:
            fieldnames = []
            for item in items:
                for key in item:
                    if key not in fieldnames:
                        fieldnames.append(key)
            dictionary = csv.DictWriter(
                output, fieldnames=fieldnames, extrasaction="ignore", lineterminator="\r\n"
            )
            dictionary.writeheader()
            for item in items:
                dictionary.writerow({key: self._csv_value(item.get(key)) for key in fieldnames})
        return ("\ufeff" + output.getvalue()).encode("utf-8")

    def _production(self, filters):
        clauses, values = self._scope(filters, "p.day", daily=True)
        rows = self.database.query_all(
            """SELECT p.day,u.id AS unit_id,u.name AS unit_name,g.id AS group_id,
                      w.id AS well_id,w.name AS well_name,p.daily_oil,p.daily_fluid,
                      p.power_kwh,p.pump_efficiency,p.runtime_hours,p.status
               FROM production_daily AS p
               JOIN wells AS w ON w.id=p.well_id
               JOIN well_groups AS g ON g.id=w.group_id
               JOIN units AS u ON u.id=g.unit_id"""
            + self._where(clauses)
            + " ORDER BY p.day,u.id,g.id,w.id",
            values,
        )
        items = [dict(row) for row in rows]
        return {
            "source_tables": ["production_daily"],
            "summary": {
                "record_count": len(items),
                "well_count": len({item["well_id"] for item in items}),
                "daily_oil_m3": _round(sum(item["daily_oil"] for item in items), 3),
                "daily_fluid_m3": _round(sum(item["daily_fluid"] for item in items), 3),
                "energy_kwh": _round(sum(item["power_kwh"] for item in items), 3),
                "runtime_hours": _round(sum(item["runtime_hours"] for item in items), 3),
                "average_pump_efficiency": self._average(
                    item["pump_efficiency"] for item in items
                ),
            },
            "items": items,
        }

    def _diagnosis(self, filters):
        clauses, values = self._scope(filters, "d.created_at")
        if filters.get("status"):
            clauses.append("d.status=?")
            values.append(filters["status"])
        rows = self.database.query_all(
            """SELECT d.id,d.created_at,u.id AS unit_id,u.name AS unit_name,
                      w.id AS well_id,w.name AS well_name,d.diagnosis_type,
                      d.confidence,d.status,d.recommendation,d.model_run_id,
                      (SELECT COUNT(*) FROM diagnosis_reviews r WHERE r.diagnosis_id=d.id)
                          AS review_count
               FROM diagnoses AS d
               JOIN wells AS w ON w.id=d.well_id
               JOIN well_groups AS g ON g.id=w.group_id
               JOIN units AS u ON u.id=g.unit_id"""
            + self._where(clauses)
            + " ORDER BY d.created_at DESC,d.id",
            values,
        )
        items = [dict(row) for row in rows]
        return {
            "source_tables": ["diagnoses", "diagnosis_reviews"],
            "summary": {
                "diagnosis_count": len(items),
                "well_count": len({item["well_id"] for item in items}),
                "reviewed_count": sum(item["status"] == "reviewed" for item in items),
                "average_confidence": self._average(item["confidence"] for item in items),
                "counts_by_type": self._counts(items, "diagnosis_type"),
            },
            "items": items,
        }

    def _data_quality(self, filters):
        clauses, values = self._scope(filters, "q.created_at")
        if filters.get("status"):
            clauses.append("q.status=?")
            values.append(filters["status"])
        rows = self.database.query_all(
            """SELECT q.id,q.created_at,q.resolved_at,u.id AS unit_id,u.name AS unit_name,
                      w.id AS well_id,w.name AS well_name,q.device_id,q.event_type,q.kind,
                      q.detail,q.status,q.raw_message_id,q.missing_from,q.missing_to
               FROM data_quality_events AS q
               LEFT JOIN wells AS w ON w.id=q.well_id
               LEFT JOIN well_groups AS g ON g.id=w.group_id
               LEFT JOIN units AS u ON u.id=g.unit_id"""
            + self._where(clauses)
            + " ORDER BY q.created_at DESC,q.id DESC",
            values,
        )
        items = [dict(row) for row in rows]
        return {
            "source_tables": ["data_quality_events", "raw_messages"],
            "summary": {
                "event_count": len(items),
                "open_count": sum(item["status"] == "open" for item in items),
                "closed_count": sum(item["status"] in {"closed", "resolved"} for item in items),
                "counts_by_type": self._counts(items, "event_type"),
            },
            "items": items,
        }

    def _strategy(self, filters):
        clauses, values = self._scope(filters, "s.created_at")
        if filters.get("status"):
            clauses.append("s.status=?")
            values.append(filters["status"])
        rows = self.database.query_all(
            """SELECT s.id,s.created_at,s.updated_at,s.effective_at,
                      u.id AS unit_id,u.name AS unit_name,w.id AS well_id,w.name AS well_name,
                      s.name,s.status,s.version,s.current_version_id,v.version_number,
                      v.configuration,s.submitted_at,s.decision_at,s.approval_comment,
                      (SELECT c.id FROM device_commands c WHERE c.strategy_id=s.id
                       ORDER BY c.created_at DESC,c.id DESC LIMIT 1) AS latest_command_id,
                      (SELECT c.status FROM device_commands c WHERE c.strategy_id=s.id
                       ORDER BY c.created_at DESC,c.id DESC LIMIT 1) AS latest_command_status
               FROM energy_strategies AS s
               JOIN wells AS w ON w.id=s.well_id
               JOIN well_groups AS g ON g.id=w.group_id
               JOIN units AS u ON u.id=g.unit_id
               LEFT JOIN strategy_versions AS v ON v.id=s.current_version_id"""
            + self._where(clauses)
            + " ORDER BY s.created_at DESC,s.id",
            values,
        )
        items = []
        for row in rows:
            item = dict(row)
            configuration = self._json_object(item.pop("configuration"))
            candidate = self._selected_candidate(configuration)
            item.update({
                "on_minutes": candidate.get("on_minutes"),
                "off_minutes": candidate.get("off_minutes"),
                "predicted_liquid_m3": candidate.get("predicted_liquid_m3"),
                "predicted_energy_kwh": candidate.get("predicted_energy_kwh"),
                "predicted_saving_rate": candidate.get("saving_rate"),
            })
            items.append(item)
        return {
            "source_tables": [
                "energy_strategies", "strategy_versions", "approvals", "device_commands"
            ],
            "summary": {
                "strategy_count": len(items),
                "effective_count": sum(item["status"] in EFFECTIVE_STATES for item in items),
                "counts_by_status": self._counts(items, "status"),
                "predicted_energy_kwh": _round(sum(
                    item["predicted_energy_kwh"] or 0 for item in items
                )),
            },
            "items": items,
        }

    def _energy(self, filters):
        clauses, values = self._scope(
            {key: value for key, value in filters.items() if key not in {"from", "to", "_from", "_to", "_to_date_only"}},
            "s.effective_at",
        )
        clauses.extend((
            "s.effective_at IS NOT NULL",
            "s.status IN ('active','effective','paused','rollback','rolled_back')",
        ))
        if filters.get("strategy_id"):
            clauses.append("s.id=?")
            values.append(filters["strategy_id"])
        strategies = self.database.query_all(
            """SELECT s.id,s.effective_at,s.well_id
               FROM energy_strategies AS s
               JOIN wells AS w ON w.id=s.well_id
               JOIN well_groups AS g ON g.id=w.group_id
               JOIN units AS u ON u.id=g.unit_id"""
            + self._where(clauses)
            + " ORDER BY s.well_id,datetime(s.effective_at),s.id",
            values,
        )
        if filters.get("strategy_id") and not strategies:
            if self.database.scalar(
                "SELECT 1 FROM energy_strategies WHERE id=?", (filters["strategy_id"],)
            ) is None:
                raise ReportNotFound("strategy not found")
            raise ReportValidationError("strategy has not reached an effective state")
        items = []
        for strategy in strategies:
            effective = self._parse_timestamp(strategy["effective_at"], "effective_at")
            latest = self._continuous_power_end(strategy["well_id"], effective)
            if latest is None:
                continue
            requested_start = filters.get("_from") or effective
            requested_end = filters.get("_to") or latest
            start = max(requested_start, effective)
            end = min(requested_end, latest) if filters.get("_to") is None else requested_end
            period_end, _ = self._effective_period_end(strategy)
            if period_end is not None:
                end = min(end, period_end)
            if start >= end:
                continue
            items.append(asdict(self.energy_effect(strategy["id"], start, end)))
        comparable = [item for item in items if item["valid_for_comparison"]]
        liquid_comparable = [item for item in items if item["liquid_valid_for_comparison"]]
        baseline = sum(item["baseline_energy_kwh"] for item in comparable)
        after = sum(item["after_energy_kwh"] for item in comparable)
        saved = baseline - after
        return {
            "source_tables": [
                "telemetry", "production_daily", "energy_strategies",
                "strategy_versions", "device_commands",
            ],
            "summary": {
                "strategy_count": len(items),
                "comparable_strategy_count": len(comparable),
                "insufficient_coverage_count": len(items) - len(comparable),
                "production_comparable_strategy_count": len(liquid_comparable),
                "insufficient_production_coverage_count": len(items) - len(liquid_comparable),
                "baseline_energy_kwh": _round(baseline),
                "after_energy_kwh": _round(after),
                "energy_saved_kwh": _round(saved) if comparable else None,
                "saving_rate": _round(saved / baseline) if baseline > 0 else None,
                "baseline_liquid_m3": _round(sum(item["baseline_liquid_m3"] for item in liquid_comparable)),
                "after_liquid_m3": _round(sum(item["after_liquid_m3"] for item in liquid_comparable)),
            },
            "items": items,
        }

    def _continuous_power_end(self, well_id, start):
        """End the default effect window at the first acquisition gap.

        A later live sample must not make a historical hourly run appear to
        cover the intervening weeks.  The returned end is an observed sample
        boundary, matching the left-held integration contract.
        """
        rows = self.database.query_all(
            """SELECT measured_at FROM telemetry
               WHERE well_id=? AND status='valid'
                 AND metric_code IN ('power_kw','active_power')
                 AND datetime(measured_at)>=datetime(?)
               ORDER BY datetime(measured_at),id""",
            (well_id, start.isoformat()),
        )
        previous = None
        for row in rows:
            current = self._parse_timestamp(row["measured_at"], "measured_at")
            if previous is not None and current - previous > MAX_POWER_HOLD:
                break
            previous = current
        return previous

    def _effective_period_end(self, strategy):
        """Return the first command-backed boundary after this activation."""
        effective = self._parse_timestamp(strategy["effective_at"], "effective_at")
        rollback_text = self.database.scalar(
            """SELECT MIN(completed_at) FROM device_commands
               WHERE strategy_id=? AND command_type='rollback_cycle'
                 AND status='success' AND completed_at IS NOT NULL
                 AND datetime(completed_at)>datetime(?)""",
            (strategy["id"], effective.isoformat()),
        )
        next_text = self.database.scalar(
            """SELECT effective_at FROM energy_strategies
               WHERE well_id=? AND id<>? AND effective_at IS NOT NULL
                 AND (datetime(effective_at)>datetime(?) OR
                      (datetime(effective_at)=datetime(?) AND id>?))
               ORDER BY datetime(effective_at),id LIMIT 1""",
            (
                strategy["well_id"], strategy["id"], effective.isoformat(),
                effective.isoformat(), strategy["id"],
            ),
        )
        candidates = []
        if rollback_text:
            candidates.append((self._parse_timestamp(rollback_text, "completed_at"), "rollback"))
        if next_text:
            candidates.append((self._parse_timestamp(next_text, "effective_at"), "next_strategy"))
        return min(candidates, key=lambda item: item[0]) if candidates else (None, None)

    def _work_orders(self, filters):
        clauses, values = self._scope(filters, "o.created_at")
        if filters.get("status"):
            clauses.append("o.status=?")
            values.append(filters["status"])
        if filters.get("assignee_id") is not None:
            clauses.append("o.assignee_id=?")
            values.append(filters["assignee_id"])
        rows = self.database.query_all(
            """SELECT o.id,o.created_at,o.updated_at,o.due_at,o.accepted_at,o.completed_at,
                      o.closed_at,u.id AS unit_id,u.name AS unit_name,w.id AS well_id,
                      w.name AS well_name,o.alarm_id,o.title,o.kind,o.assignee_id,
                      assignee.display_name AS assignee_name,o.closed_by,o.status,
                      (SELECT COUNT(*) FROM work_order_logs l WHERE l.work_order_id=o.id)
                          AS log_count
               FROM work_orders AS o
               LEFT JOIN wells AS w ON w.id=o.well_id
               LEFT JOIN well_groups AS g ON g.id=w.group_id
               LEFT JOIN units AS u ON u.id=g.unit_id
               LEFT JOIN users AS assignee ON assignee.id=o.assignee_id"""
            + self._where(clauses)
            + " ORDER BY o.created_at DESC,o.id",
            values,
        )
        items = []
        for row in rows:
            item = dict(row)
            item["overdue"] = self._overdue(item)
            item["cycle_hours"] = self._cycle_hours(item)
            items.append(item)
        closed = [item for item in items if item["status"] == "closed" and item["closed_at"]]
        timely = [item for item in closed if not item["overdue"]]
        return {
            "source_tables": ["work_orders", "work_order_logs", "alarms"],
            "summary": {
                "work_order_count": len(items),
                "closed_count": len(closed),
                "open_count": len(items) - len(closed),
                "overdue_count": sum(item["overdue"] for item in items),
                "timely_close_rate": _round(len(timely) / len(closed)) if closed else None,
                "average_cycle_hours": self._average(
                    item["cycle_hours"] for item in closed if item["cycle_hours"] is not None
                ),
                "counts_by_status": self._counts(items, "status"),
            },
            "items": items,
        }

    def _power_stats(self, well_id, start, end):
        metric = self.database.scalar(
            """SELECT metric_code FROM telemetry
               WHERE well_id=? AND status='valid' AND metric_code IN ('power_kw','active_power')
               ORDER BY CASE metric_code WHEN 'power_kw' THEN 0 ELSE 1 END LIMIT 1""",
            (well_id,),
        )
        if metric is None:
            return {"energy_kwh": 0.0, "runtime_hours": 0.0,
                    "coverage_hours": 0.0, "start_count": 0}
        start_text, end_text = start.isoformat(), end.isoformat()
        points = self.database.query_all(
            """SELECT measured_at,value FROM telemetry
               WHERE well_id=? AND metric_code=? AND status='valid'
                 AND datetime(measured_at)>=datetime(?) AND datetime(measured_at)<datetime(?)
               ORDER BY datetime(measured_at),id""",
            (well_id, metric, start_text, end_text),
        )
        predecessor = self.database.query_one(
            """SELECT measured_at,value FROM telemetry
               WHERE well_id=? AND metric_code=? AND status='valid' AND datetime(measured_at)<datetime(?)
               ORDER BY datetime(measured_at) DESC,id DESC LIMIT 1""",
            (well_id, metric, start_text),
        )
        successor = self.database.query_one(
            """SELECT measured_at,value FROM telemetry
               WHERE well_id=? AND metric_code=? AND status='valid' AND datetime(measured_at)>=datetime(?)
               ORDER BY datetime(measured_at),id LIMIT 1""",
            (well_id, metric, end_text),
        )
        sequence = ([predecessor] if predecessor else []) + list(points) + ([successor] if successor else [])
        sequence_times = [
            self._parse_timestamp(point["measured_at"], "measured_at")
            for point in sequence
        ]
        observed_intervals = [
            (following - current).total_seconds()
            for current, following in zip(sequence_times, sequence_times[1:])
            if timedelta(0) < following - current <= MAX_POWER_HOLD
        ]
        hold = timedelta(seconds=min(
            MAX_INFERRED_POWER_HOLD.total_seconds(),
            median(observed_intervals) * 1.5 if observed_intervals else 0,
        ))
        energy = runtime = coverage = 0.0
        starts = 0
        previous_value = float(predecessor["value"]) if predecessor else None
        previous_time = self._parse_timestamp(predecessor["measured_at"], "measured_at") if predecessor else None
        for point in points:
            point_time = self._parse_timestamp(point["measured_at"], "measured_at")
            value = float(point["value"])
            if value > 0 and (
                previous_value is None or previous_value <= 0
                or (previous_time is not None and point_time - previous_time > MAX_POWER_HOLD)
            ):
                starts += 1
            previous_value, previous_time = value, point_time
        for current, following in zip(sequence, sequence[1:]):
            current_time = self._parse_timestamp(current["measured_at"], "measured_at")
            following_time = self._parse_timestamp(following["measured_at"], "measured_at")
            if following_time <= current_time or following_time - current_time > hold:
                continue
            interval_start, interval_end = max(start, current_time), min(end, following_time)
            if interval_start >= interval_end:
                continue
            hours = (interval_end - interval_start).total_seconds() / 3600
            value = max(0.0, float(current["value"]))
            energy += value * hours
            coverage += hours
            if value > 0:
                runtime += hours
        return {"energy_kwh": energy, "runtime_hours": runtime,
                "coverage_hours": coverage, "start_count": starts}

    def _production_stats(self, well_id, start, end):
        first_day = start.astimezone(SHANGHAI).date()
        last_day = (end.astimezone(SHANGHAI) - timedelta(microseconds=1)).date()
        rows = self.database.query_all(
            """SELECT day,daily_fluid,runtime_hours FROM production_daily
               WHERE well_id=? AND status='valid' AND day BETWEEN ? AND ? ORDER BY day""",
            (well_id, first_day.isoformat(), last_day.isoformat()),
        )
        liquid = runtime = coverage = 0.0
        for row in rows:
            day = date.fromisoformat(row["day"])
            day_start = datetime.combine(day, time.min, SHANGHAI).astimezone(UTC)
            day_end = day_start + timedelta(days=1)
            overlap = max(0.0, (min(end, day_end) - max(start, day_start)).total_seconds())
            fraction = overlap / 86400
            liquid += float(row["daily_fluid"]) * fraction
            runtime += float(row["runtime_hours"]) * fraction
            coverage += overlap / 3600
        return {"liquid_m3": liquid, "runtime_hours": runtime, "coverage_hours": coverage}

    def _strategy_row(self, strategy_id):
        row = self.database.query_one(
            """SELECT s.*,v.configuration,w.name AS well_name,u.id AS unit_id,u.name AS unit_name
               FROM energy_strategies AS s
               JOIN wells AS w ON w.id=s.well_id
               JOIN well_groups AS g ON g.id=w.group_id
               JOIN units AS u ON u.id=g.unit_id
               LEFT JOIN strategy_versions AS v ON v.id=s.current_version_id
               WHERE s.id=?""",
            (strategy_id,),
        )
        if row is None:
            raise ReportNotFound("strategy not found")
        return row

    def _scope(self, filters, time_column, daily=False):
        clauses, values = [], []
        if filters.get("unit_id"):
            clauses.append("u.id=?")
            values.append(filters["unit_id"])
        if filters.get("well_id"):
            clauses.append("w.id=?")
            values.append(filters["well_id"])
        if filters.get("_from"):
            if daily:
                clauses.append(f"{time_column}>=?")
                values.append(filters["_from"].astimezone(SHANGHAI).date().isoformat())
            else:
                clauses.append(f"datetime({time_column})>=datetime(?)")
                values.append(filters["_from"].isoformat())
        if filters.get("_to"):
            if daily:
                clauses.append(f"{time_column}<=?")
                end = filters["_to"]
                if filters.get("_to_date_only"):
                    end -= timedelta(microseconds=1)
                values.append(end.astimezone(SHANGHAI).date().isoformat())
            else:
                operator = "<" if filters.get("_to_date_only") else "<="
                clauses.append(f"datetime({time_column}){operator}datetime(?)")
                values.append(filters["_to"].isoformat())
        return clauses, values

    def _filters(self, report_type, filters):
        if report_type not in REPORT_FILTERS:
            raise ReportNotFound("report type not found")
        if not isinstance(filters, dict):
            raise ReportValidationError("filters must be an object")
        if set(filters) - REPORT_FILTERS[report_type]:
            raise ReportValidationError("unknown report filter")
        result = dict(filters)
        for key in ("unit_id", "well_id", "strategy_id"):
            if key in result:
                self._identifier(result[key], key)
        if "status" in result:
            if not isinstance(result["status"], str) or not STATUS_PATTERN.fullmatch(result["status"]):
                raise ReportValidationError("invalid status")
        if "assignee_id" in result:
            try:
                assignee = int(result["assignee_id"])
            except (TypeError, ValueError):
                raise ReportValidationError("assignee_id must be a positive integer") from None
            if (
                isinstance(result["assignee_id"], bool) or assignee < 1
                or assignee > SQLITE_MAX_INTEGER
                or str(assignee) != str(result["assignee_id"])
            ):
                raise ReportValidationError("assignee_id must be a positive integer")
            result["assignee_id"] = assignee
        start = self._parse_filter(result.get("from"), "from", False)
        end, end_date_only = self._parse_filter(result.get("to"), "to", True)
        if start:
            result["_from"] = start
        if end:
            result["_to"] = end
            result["_to_date_only"] = end_date_only
        if start and end:
            self._range(start, end)
        return result

    @staticmethod
    def _parse_filter(value, name, end_of_date):
        if value is None:
            return (None, False) if end_of_date else None
        if not isinstance(value, str) or not value:
            raise ReportValidationError(f"{name} must be an ISO8601 date or timestamp")
        date_only = bool(re.fullmatch(r"\d{4}-\d{2}-\d{2}", value))
        try:
            if date_only:
                parsed = datetime.combine(date.fromisoformat(value), time.min, SHANGHAI)
                if end_of_date:
                    parsed += timedelta(days=1)
            else:
                parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
                if parsed.tzinfo is None:
                    raise ValueError
            normalized = parsed.astimezone(UTC)
        except (ValueError, OverflowError):
            raise ReportValidationError(f"{name} must be an ISO8601 date or timezone timestamp") from None
        return (normalized, date_only) if end_of_date else normalized

    @staticmethod
    def _public_filters(filters):
        return {key: value for key, value in filters.items() if not key.startswith("_")}

    @staticmethod
    def _identifier(value, name):
        if not isinstance(value, str) or not ID_PATTERN.fullmatch(value):
            raise ReportValidationError(f"invalid {name}")

    @staticmethod
    def _parse_timestamp(value, name):
        if not isinstance(value, str):
            raise ReportValidationError(f"invalid {name}")
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            raise ReportValidationError(f"invalid {name}") from None
        if parsed.tzinfo is None:
            raise ReportValidationError(f"invalid {name}")
        return parsed.astimezone(UTC)

    @staticmethod
    def _aware_datetime(value, name):
        if not isinstance(value, datetime) or value.tzinfo is None:
            raise ReportValidationError(f"{name} must be a timezone-aware datetime")
        return value.astimezone(UTC)

    @staticmethod
    def _range(start, end):
        if start >= end:
            raise ReportValidationError("from must be earlier than to")
        if end - start > MAX_REPORT_RANGE:
            raise ReportValidationError("report range exceeds 366 days")

    def _now(self):
        value = self.clock()
        if not isinstance(value, datetime) or value.tzinfo is None:
            raise ReportValidationError("report clock must return a timezone-aware datetime")
        return value.astimezone(SHANGHAI)

    @staticmethod
    def _selected_candidate(raw):
        configuration = raw if isinstance(raw, dict) else ReportService._json_object(raw)
        candidate = configuration.get("selected_candidate", {})
        return candidate if isinstance(candidate, dict) else {}

    @staticmethod
    def _json_object(raw):
        if not isinstance(raw, str):
            return {}
        try:
            value = json.loads(raw)
        except json.JSONDecodeError:
            return {}
        return value if isinstance(value, dict) else {}

    @staticmethod
    def _counts(items, key):
        result = {}
        for item in items:
            value = item.get(key) or "unknown"
            result[value] = result.get(value, 0) + 1
        return dict(sorted(result.items()))

    @staticmethod
    def _average(values):
        numbers = [float(value) for value in values if value is not None]
        return _round(sum(numbers) / len(numbers)) if numbers else None

    @staticmethod
    def _number(value):
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
            return None
        return _round(value)

    def _overdue(self, item):
        if not item.get("due_at"):
            return item.get("status") == "overdue"
        due = self._parse_timestamp(item["due_at"], "due_at")
        closed = self._parse_timestamp(item["closed_at"], "closed_at") if item.get("closed_at") else None
        return item.get("status") == "overdue" or (closed or self._now().astimezone(UTC)) > due

    @staticmethod
    def _cycle_hours(item):
        if not item.get("closed_at") or not item.get("created_at"):
            return None
        try:
            created = datetime.fromisoformat(item["created_at"].replace("Z", "+00:00"))
            closed = datetime.fromisoformat(item["closed_at"].replace("Z", "+00:00"))
        except ValueError:
            return None
        if created.tzinfo is None or closed.tzinfo is None or closed < created:
            return None
        return _round((closed - created).total_seconds() / 3600)

    @staticmethod
    def _where(clauses):
        return " WHERE " + " AND ".join(clauses) if clauses else ""

    @staticmethod
    def _csv_value(value):
        if isinstance(value, (dict, list, tuple)):
            value = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        if value is None:
            return ""
        if isinstance(value, bool):
            return "true" if value else "false"
        if isinstance(value, str) and value.startswith(("=", "+", "-", "@", "\t", "\r", "\n")):
            return "'" + value
        return value
