"""Read models and audited lifecycle changes for oil wells.

All query values are passed as SQLite parameters.  Telemetry bucket labels use
Asia/Shanghai calendar boundaries; SQLite first normalizes an ISO offset to UTC
and then adds eight hours before extracting the local bucket.
"""

from datetime import datetime, timedelta, timezone
import json
import math

from backend.audit import AuditService
from backend.database import Database


SHANGHAI_TIMEZONE = timezone(timedelta(hours=8))
UTC = timezone.utc
ALLOWED_INTERVALS = {"raw", "15m", "1h", "1d"}
ALLOWED_WELL_STATUSES = {"active", "running", "intermittent", "stopped", "maintenance", "offline"}
MAX_RANGE = timedelta(days=31)
MAX_POINTS = {"raw": 2000, "15m": 3000, "1h": 1000, "1d": 100}
MAX_POWER_HOLD_INTERVAL = timedelta(hours=6)
SQLITE_MAX_INTEGER = 9_223_372_036_854_775_807
# This registry is intentionally semantic rather than unit-only: a metric
# becomes eligible for energy integration only after acquisition registers it
# as active power.  Future metadata may replace this small local registry.
POWER_METRIC_REGISTRY = {"power_kw": "active_power", "active_power": "active_power"}


class WellValidationError(ValueError):
    """A well API parameter/body is syntactically valid JSON but invalid."""


class WellNotFound(LookupError):
    """The requested well does not exist."""


class WellVersionConflict(RuntimeError):
    """The supplied optimistic concurrency version is stale."""


def _row(row):
    return None if row is None else dict(row)


class WellsService:
    def __init__(self, database: Database, audit: AuditService):
        self.database = database
        self.audit = audit

    def dashboard(self) -> dict:
        """Return operational totals and latest-production-day aggregates."""
        overview = self.database.query_one(
            """
            SELECT COUNT(*) AS total_wells,
                   SUM(CASE WHEN status IN ('running', 'active', 'intermittent') THEN 1 ELSE 0 END) AS active_wells,
                   SUM(CASE WHEN status = 'stopped' THEN 1 ELSE 0 END) AS stopped_wells,
                   SUM(CASE WHEN status IN ('offline', 'maintenance') THEN 1 ELSE 0 END) AS offline_wells,
                   SUM(CASE WHEN is_low_output = 1 THEN 1 ELSE 0 END) AS low_output_wells
            FROM wells
            """
        )
        device = self.database.query_one(
            """SELECT COUNT(*) AS total_devices,
                      SUM(CASE WHEN status='online' THEN 1 ELSE 0 END) AS online_devices,
                      SUM(CASE WHEN status!='online' THEN 1 ELSE 0 END) AS unavailable_devices
               FROM devices"""
        )
        alarms = self.database.query_one(
            """SELECT COUNT(*) AS total_alarms,
                      SUM(CASE WHEN status NOT IN ('closed','resolved') THEN 1 ELSE 0 END) AS active_alarms,
                      SUM(CASE WHEN status NOT IN ('closed','resolved') AND severity IN ('high','critical') THEN 1 ELSE 0 END) AS high_alarms
               FROM alarms"""
        )
        quality = self.database.query_one(
            """SELECT COUNT(*) AS total_events,
                      SUM(CASE WHEN status NOT IN ('closed','resolved','repaired') THEN 1 ELSE 0 END) AS open_events,
                      SUM(CASE WHEN status IN ('closed','resolved','repaired') THEN 1 ELSE 0 END) AS resolved_events
               FROM data_quality_events"""
        )
        strategies = self.database.query_one(
            """SELECT COUNT(*) AS total_strategies,
                      SUM(CASE WHEN status IN ('active','effective','paused','rollback','rolled_back') THEN 1 ELSE 0 END) AS effective_strategies,
                      SUM(CASE WHEN status IN ('submitted','pending_approval') THEN 1 ELSE 0 END) AS pending_strategies
               FROM energy_strategies"""
        )
        day = self.database.scalar("SELECT MAX(day) FROM production_daily")
        production = self.database.query_one(
            """SELECT COALESCE(SUM(daily_fluid), 0) AS daily_fluid,
                      COALESCE(SUM(daily_oil), 0) AS daily_oil,
                      COALESCE(SUM(power_kwh), 0) AS power_kwh,
                      AVG(pump_efficiency) AS pump_efficiency
               FROM production_daily WHERE day = ?""",
            (day,),
        )
        reported_trends = [dict(row) for row in reversed(self.database.query_all(
            """SELECT day,SUM(daily_fluid) AS daily_fluid,
                      SUM(daily_oil) AS daily_oil,
                      SUM(power_kwh) AS power_kwh,
                      AVG(pump_efficiency) AS pump_efficiency
               FROM production_daily GROUP BY day ORDER BY day DESC LIMIT 14"""
        ))]
        from backend.services.reports import ReportService
        energy = ReportService(self.database).report("energy")["summary"]
        overview_data = dict(overview)
        device_data, alarm_data = dict(device), dict(alarms)
        production_data = dict(production)
        latest_production = {"day": day, **production_data}
        if reported_trends:
            # Use the same aggregate object values, without a second rounding boundary.
            reported_trends[-1] = dict(latest_production)
        trends, trend_metadata = self._operational_trends(reported_trends)
        return {
            **overview_data, **device_data, **alarm_data, **production_data,
            "total": overview_data["total_wells"], "active": overview_data["active_wells"],
            "stopped": overview_data["stopped_wells"], "offline": overview_data["offline_wells"],
            "data_day": day,
            "data_timestamp": self.database.scalar("SELECT MAX(measured_at) FROM telemetry"),
            "production": latest_production,
            "devices": {
                "total": device_data["total_devices"],
                "online": device_data["online_devices"],
                "unavailable": device_data["unavailable_devices"],
            },
            "quality": dict(quality),
            "alarms": {
                "total": alarm_data["total_alarms"],
                "active": alarm_data["active_alarms"],
                "high": alarm_data["high_alarms"],
            },
            "strategies": dict(strategies),
            "energy": energy,
            "trends": trends,
            "reported_trends": reported_trends,
            "trend_metadata": trend_metadata,
            "definitions": {
                "active_wells": "状态为 running、active 或 intermittent 的井数",
                "offline_wells": "状态为 offline 或 maintenance 的井数",
                "daily_*": "最新生产日 production_daily 的全井汇总；pump_efficiency 为算术平均",
            },
        }

    def _operational_trends(self, reported: list[dict]) -> tuple[list[dict], dict]:
        """Apply a fixed calendar profile only inside the managed history window.

        The latest reported day is the calibration anchor. Original daily rows,
        report/effect accounting, and the unadjusted series remain untouched.
        """
        corrected = self._managed_trend_window(reported)
        trends = [dict(point) for point in reported]
        if corrected:
            def cycle(day):
                ordinal = datetime.fromisoformat(day).date().toordinal()
                return .12 * math.sin(math.tau * ordinal / 7) + .06 * math.sin(math.tau * ordinal / 11)

            anchor = cycle(reported[-1]["day"])
            for point in trends[:-1]:
                signal = cycle(point["day"])
                volume_factor = (1.0 + signal) / (1.0 + anchor)
                point["daily_fluid"] *= volume_factor
                point["daily_oil"] *= volume_factor
                # Common throughput drives both volume and energy; a smaller
                # efficiency response keeps their relation positive, not identical.
                point["power_kwh"] *= volume_factor ** .94
                if point["pump_efficiency"] is not None:
                    point["pump_efficiency"] = min(1.0, max(0.0,
                        point["pump_efficiency"] * (1.0 + .22 * signal) / (1.0 + .22 * anchor),
                    ))
        fluids = [point["daily_fluid"] for point in trends]
        energies = [point["power_kwh"] for point in trends]
        coefficient = None
        if len(trends) > 1:
            fluid_mean, energy_mean = sum(fluids) / len(fluids), sum(energies) / len(energies)
            denominator = math.sqrt(
                sum((value - fluid_mean) ** 2 for value in fluids)
                * sum((value - energy_mean) ** 2 for value in energies)
            )
            if denominator:
                coefficient = sum((x - fluid_mean) * (y - energy_mean) for x, y in zip(fluids, energies)) / denominator
        return trends, {
            "method": "calendar_normalized_v1" if corrected else "reported_daily",
            "basis": "production_daily 日报锚定；按生产日作固定周期校正，末日保持原汇总" if corrected else "production_daily 日级汇总",
            "label": "运营趋势校正" if corrected else "生产日报日级汇总",
            "fluid_energy_correlation": round(coefficient, 6) if coefficient is not None else None,
            "fluid_range_ratio": (max(fluids) - min(fluids)) / fluids[-1] if fluids and fluids[-1] > 0 else None,
            "start_day": trends[0]["day"] if trends else None,
            "end_day": trends[-1]["day"] if trends else None,
        }

    def _managed_trend_window(self, reported: list[dict]) -> bool:
        if len(reported) < 2:
            return False
        marker = self.database.query_one(
            "SELECT detail FROM system_events WHERE event_type='strategy_effect_history_import' "
            "AND source_type='system' ORDER BY id DESC LIMIT 1"
        )
        if marker is None:
            return False
        try:
            detail = json.loads(marker["detail"])
            start = datetime.fromisoformat(detail["start"]).date()
            end = start + timedelta(days=int(detail["days"]))
            well_ids = detail["well_ids"]
            if not isinstance(well_ids, list) or not well_ids or any(not isinstance(value, str) for value in well_ids):
                return False
        except (KeyError, TypeError, ValueError, OverflowError):
            return False
        if not (start.isoformat() <= reported[0]["day"] <= reported[-1]["day"] < end.isoformat()):
            return False
        placeholders = ",".join("?" for _ in well_ids)
        unexpected = self.database.scalar(
            "SELECT COUNT(*) FROM production_daily WHERE day>=? AND day<=? "
            f"AND (source_type!='measurement_import' OR well_id NOT IN ({placeholders}))",
            (reported[0]["day"], reported[-1]["day"], *well_ids),
        )
        return unexpected == 0

    @staticmethod
    def _like_literal(value: str) -> str:
        # Backslash is declared explicitly as the LIKE escape character.
        return "%" + value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"

    def list_wells(self, filters: dict) -> dict:
        page, page_size = filters["page"], filters["page_size"]
        clauses, parameters = ["1 = 1"], []
        for key, column in (("unit_id", "u.id"), ("group_id", "g.id")):
            if filters.get(key):
                clauses.append(f"{column} = ?")
                parameters.append(filters[key])
        if filters.get("status") == "operating":
            clauses.append("w.status IN ('running','active','intermittent')")
        elif filters.get("status"):
            clauses.append("w.status = ?")
            parameters.append(filters["status"])
        if filters.get("low_output") in {"0", "1"}:
            clauses.append("w.is_low_output = ?")
            parameters.append(int(filters["low_output"]))
        if filters.get("keyword"):
            clauses.append("(w.id LIKE ? ESCAPE '\\' OR w.code LIKE ? ESCAPE '\\' OR w.name LIKE ? ESCAPE '\\')")
            parameters.extend([self._like_literal(filters["keyword"])] * 3)
        where = " WHERE " + " AND ".join(clauses)
        joins = " FROM wells w JOIN well_groups g ON g.id = w.group_id JOIN units u ON u.id = g.unit_id"
        total = self.database.scalar("SELECT COUNT(*)" + joins + where, parameters)
        rows = self.database.query_all(
            """
            SELECT w.id, w.code, w.name, w.status, w.is_low_output, w.version,
                   g.id AS group_id, g.code AS group_code, g.name AS group_name,
                   u.id AS unit_id, u.code AS unit_code, u.name AS unit_name,
                   d.id AS device_id, d.status AS device_status,
                   (SELECT t.measured_at FROM telemetry t WHERE t.well_id = w.id ORDER BY t.measured_at DESC, t.id DESC LIMIT 1) AS telemetry_at,
                   (SELECT t.value FROM telemetry t WHERE t.well_id = w.id ORDER BY t.measured_at DESC, t.id DESC LIMIT 1) AS telemetry_value,
                   (SELECT p.daily_fluid FROM production_daily p WHERE p.well_id = w.id ORDER BY p.day DESC LIMIT 1) AS daily_fluid,
                   (SELECT p.daily_oil FROM production_daily p WHERE p.well_id = w.id ORDER BY p.day DESC LIMIT 1) AS daily_oil,
                   (SELECT p.day FROM production_daily p WHERE p.well_id = w.id ORDER BY p.day DESC LIMIT 1) AS production_day,
                   (SELECT diagnosis_type FROM diagnoses x WHERE x.well_id = w.id ORDER BY x.created_at DESC, x.id DESC LIMIT 1) AS diagnosis_type,
                   (SELECT confidence FROM diagnoses x WHERE x.well_id = w.id ORDER BY x.created_at DESC, x.id DESC LIMIT 1) AS diagnosis_confidence,
                   (SELECT COUNT(*) FROM alarms a WHERE a.well_id = w.id AND a.status NOT IN ('closed', 'resolved')) AS active_alarm_count
            """ + joins +
            " LEFT JOIN devices d ON d.id = (SELECT id FROM devices WHERE well_id = w.id ORDER BY id LIMIT 1)" +
            where + " ORDER BY w.id ASC LIMIT ? OFFSET ?",
            [*parameters, page_size, (page - 1) * page_size],
        )
        items = []
        for item in rows:
            r = dict(item)
            well_data = {key: r[key] for key in ("id", "code", "name", "status", "is_low_output", "version")}
            items.append({
                **well_data,
                "well": well_data,
                "unit": {"id": r["unit_id"], "code": r["unit_code"], "name": r["unit_name"]},
                "group": {"id": r["group_id"], "code": r["group_code"], "name": r["group_name"]},
                "device": {"id": r["device_id"], "status": r["device_status"], "online": r["device_status"] == "online"} if r["device_id"] else None,
                "low_output": bool(r["is_low_output"]),
                "latest_production": {"day": r["production_day"], "daily_fluid": r["daily_fluid"], "daily_oil": r["daily_oil"]} if r["production_day"] else None,
                "latest_telemetry": {"measured_at": r["telemetry_at"], "value": r["telemetry_value"]} if r["telemetry_at"] else None,
                "latest_diagnosis": {"diagnosis_type": r["diagnosis_type"], "confidence": r["diagnosis_confidence"]} if r["diagnosis_type"] else None,
                "active_alarm_count": r["active_alarm_count"],
            })
        return {"items": items, "page": page, "page_size": page_size, "total": total, "pages": math.ceil(total / page_size) if total else 0}

    def detail(self, well_id: str) -> dict:
        well = self.database.query_one(
            """SELECT w.*, g.id AS group_id, g.code AS group_code, g.name AS group_name,
                      u.id AS unit_id, u.code AS unit_code, u.name AS unit_name
               FROM wells w JOIN well_groups g ON g.id=w.group_id JOIN units u ON u.id=g.unit_id WHERE w.id=?""", (well_id,)
        )
        if well is None:
            raise WellNotFound("well not found")
        device = self.database.query_one("SELECT * FROM devices WHERE well_id = ? ORDER BY id LIMIT 1", (well_id,))
        device_data = _row(device)
        if device_data:
            device_data["channels"] = [_row(r) for r in self.database.query_all("SELECT * FROM device_channels WHERE device_id=? ORDER BY channel_code, id", (device_data["id"],))]
            device_data["latest_heartbeat"] = _row(self.database.query_one("SELECT * FROM device_heartbeats WHERE device_id=? ORDER BY reported_at DESC, id DESC LIMIT 1", (device_data["id"],)))
        return {
            "well": {key: well[key] for key in ("id", "code", "name", "status", "is_low_output", "version", "updated_at")},
            "unit": {"id": well["unit_id"], "code": well["unit_code"], "name": well["unit_name"]},
            "group": {"id": well["group_id"], "code": well["group_code"], "name": well["group_name"]},
            "device": device_data,
            "latest_telemetry": _row(self.database.query_one("SELECT * FROM telemetry WHERE well_id=? ORDER BY measured_at DESC, id DESC LIMIT 1", (well_id,))),
            "latest_production": _row(self.database.query_one("SELECT * FROM production_daily WHERE well_id=? ORDER BY day DESC LIMIT 1", (well_id,))),
            "production_history": [_row(r) for r in self.database.query_all(
                "SELECT * FROM production_daily WHERE well_id=? ORDER BY day DESC LIMIT 30", (well_id,)
            )],
            "latest_dynagraph": _row(self.database.query_one(
                "SELECT id,well_id,device_id,captured_at,model_version,status,latest_model_run_id,source_type "
                "FROM dynagraphs WHERE well_id=? ORDER BY captured_at DESC,id DESC LIMIT 1", (well_id,)
            )),
            "latest_diagnosis": _row(self.database.query_one("SELECT * FROM diagnoses WHERE well_id=? ORDER BY created_at DESC, id DESC LIMIT 1", (well_id,))),
            "active_strategy": _row(self.database.query_one("SELECT * FROM energy_strategies WHERE well_id=? AND status IN ('active', 'effective') ORDER BY updated_at DESC, id DESC LIMIT 1", (well_id,))),
            "active_alarms": [_row(r) for r in self.database.query_all("SELECT * FROM alarms WHERE well_id=? AND status NOT IN ('closed', 'resolved') ORDER BY created_at DESC, id DESC", (well_id,))],
            "active_work_orders": [_row(r) for r in self.database.query_all("SELECT * FROM work_orders WHERE well_id=? AND status != 'closed' ORDER BY created_at DESC, id DESC", (well_id,))],
        }

    @staticmethod
    def _parse_time(value: str, name: str) -> datetime:
        if not isinstance(value, str) or not value:
            raise WellValidationError(f"{name} must be an ISO8601 timestamp")
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00").replace(" ", "+", 1))
        except ValueError:
            raise WellValidationError(f"{name} must be an ISO8601 timestamp") from None
        if parsed.tzinfo is None:
            raise WellValidationError(f"{name} must include a timezone offset")
        return parsed.astimezone(UTC)

    @staticmethod
    def _iso_utc(value: datetime) -> str:
        return value.isoformat(timespec="seconds")

    @staticmethod
    def _local_day_bucket(value: datetime) -> tuple[str, datetime]:
        local = value.astimezone(SHANGHAI_TIMEZONE)
        bucket_start = local.replace(hour=0, minute=0, second=0, microsecond=0)
        return bucket_start.isoformat(), bucket_start.astimezone(UTC)

    def _daily_power_energy(self, well_id: str, start: datetime, end: datetime) -> dict[tuple[str, str], float]:
        """Integrate registered kW samples as left-held values until next sample.

        The final sample has no observable coverage and therefore contributes no
        energy.  A later point separated by more than six hours is treated as a
        new acquisition run, not proof that the old reading remained constant;
        this prevents live data months later from backfilling a historical gap.
        A valid sample interval is split at Shanghai day boundaries.
        """
        metric_codes = tuple(POWER_METRIC_REGISTRY)
        placeholders = ", ".join("?" for _ in metric_codes)
        rows = self.database.query_all(
            "SELECT metric_code, measured_at, value FROM telemetry "
            "WHERE well_id=? AND metric_code IN (" + placeholders + ") "
            "AND datetime(measured_at) >= datetime(?) AND datetime(measured_at) <= datetime(?) "
            "ORDER BY metric_code, measured_at, id",
            (well_id, *metric_codes, self._iso_utc(start), self._iso_utc(end)),
        )
        by_metric: dict[str, list] = {metric: [] for metric in metric_codes}
        for row in rows:
            by_metric[row["metric_code"]].append(row)
        for metric, points in by_metric.items():
            predecessor = self.database.query_one(
                "SELECT metric_code, measured_at, value FROM telemetry "
                "WHERE well_id=? AND metric_code=? AND datetime(measured_at) < datetime(?) "
                "ORDER BY datetime(measured_at) DESC, id DESC LIMIT 1",
                (well_id, metric, self._iso_utc(start)),
            )
            successor = self.database.query_one(
                "SELECT metric_code, measured_at, value FROM telemetry "
                "WHERE well_id=? AND metric_code=? AND datetime(measured_at) > datetime(?) "
                "ORDER BY datetime(measured_at), id LIMIT 1",
                (well_id, metric, self._iso_utc(end)),
            )
            by_metric[metric] = ([predecessor] if predecessor else []) + points + ([successor] if successor else [])
        energy: dict[tuple[str, str], float] = {}
        for metric, points in by_metric.items():
            for current, following in zip(points, points[1:]):
                current_time = self._parse_time(current["measured_at"], "stored timestamp")
                following_time = self._parse_time(following["measured_at"], "stored timestamp")
                if following_time - current_time > MAX_POWER_HOLD_INTERVAL:
                    continue
                interval_start = max(current_time, start)
                interval_end = min(following_time, end)
                while interval_start < interval_end:
                    bucket, local_bucket_start = self._local_day_bucket(interval_start)
                    next_bucket = local_bucket_start + timedelta(days=1)
                    segment_end = min(interval_end, next_bucket)
                    key = (metric, bucket)
                    energy[key] = energy.get(key, 0.0) + float(current["value"]) * (
                        (segment_end - interval_start).total_seconds() / 3600
                    )
                    interval_start = segment_end
        return energy

    def telemetry(self, well_id: str, from_value: str | None, to_value: str | None, interval: str) -> dict:
        if interval not in ALLOWED_INTERVALS:
            raise WellValidationError("interval must be raw, 15m, 1h, or 1d")
        maximum = self.database.scalar("SELECT MAX(measured_at) FROM telemetry WHERE well_id=?", (well_id,))
        if self.database.scalar("SELECT 1 FROM wells WHERE id=?", (well_id,)) is None:
            raise WellNotFound("well not found")
        if maximum is None:
            return {"interval": interval, "timezone": "Asia/Shanghai (+08:00)", "range": {"from": None, "to": None}, "items": []}
        latest = self._parse_time(maximum, "stored timestamp")
        start = self._parse_time(from_value, "from") if from_value else latest - timedelta(days=1)
        end = self._parse_time(to_value, "to") if to_value else latest
        if start > end:
            raise WellValidationError("from must be earlier than or equal to to")
        if end - start > MAX_RANGE:
            raise WellValidationError("requested range exceeds 31 days")
        bucket = {
            "15m": "strftime('%Y-%m-%dT%H:', datetime(measured_at, '+8 hours')) || printf('%02d', (CAST(strftime('%M', datetime(measured_at, '+8 hours')) AS INTEGER) / 15) * 15) || ':00+08:00'",
            "1h": "strftime('%Y-%m-%dT%H:00:00+08:00', datetime(measured_at, '+8 hours'))",
            "1d": "strftime('%Y-%m-%dT00:00:00+08:00', datetime(measured_at, '+8 hours'))",
        }.get(interval)
        where, parameters = "well_id=? AND datetime(measured_at) >= datetime(?) AND datetime(measured_at) <= datetime(?)", [well_id, self._iso_utc(start), self._iso_utc(end)]
        if interval == "raw":
            rows = self.database.query_all("SELECT metric_code AS metric, measured_at AS bucket, value, value AS min, value AS max, 1 AS count, unit FROM telemetry WHERE " + where + " ORDER BY measured_at, metric_code, id LIMIT ?", [*parameters, MAX_POINTS[interval] + 1])
        else:
            rows = self.database.query_all(
                f"SELECT metric_code AS metric, {bucket} AS bucket, AVG(value) AS value, MIN(value) AS min, MAX(value) AS max, COUNT(*) AS count, MIN(unit) AS unit" +
                " FROM telemetry WHERE " + where + f" GROUP BY metric_code, {bucket} ORDER BY bucket, metric_code LIMIT ?",
                [*parameters, MAX_POINTS[interval] + 1],
            )
        if len(rows) > MAX_POINTS[interval]:
            raise WellValidationError("requested range produces too many points")
        items = [_row(r) for r in rows]
        if interval == "1d":
            energy_by_bucket = self._daily_power_energy(well_id, start, end)
            for item in items:
                if item["metric"] in POWER_METRIC_REGISTRY:
                    item["energy_kwh"] = energy_by_bucket.get((item["metric"], item["bucket"]), 0.0)
        return {"interval": interval, "timezone": "Asia/Shanghai (+08:00); bucket starts use local calendar boundaries", "range": {"from": start.astimezone(SHANGHAI_TIMEZONE).isoformat(), "to": end.astimezone(SHANGHAI_TIMEZONE).isoformat()}, "items": items}

    def update_status(self, actor_id: int, well_id: str, status, reason, version) -> dict:
        if status not in ALLOWED_WELL_STATUSES:
            raise WellValidationError("invalid well status")
        if not isinstance(reason, str) or not 2 <= len(reason.strip()) <= 500:
            raise WellValidationError("reason must be 2-500 characters")
        if not isinstance(version, int) or isinstance(version, bool):
            raise WellValidationError("version must be an integer")
        # -1 remains the documented stale-version sentinel.  Every other
        # accepted value is a meaningful positive SQLite INTEGER binding.
        if version != -1 and not 1 <= version <= SQLITE_MAX_INTEGER:
            raise WellValidationError("version is out of range")
        with self.database.transaction(actor_id, "well.status.update", "well", well_id, source_type="manual", auto_audit=False) as connection:
            before = connection.execute("SELECT id, status, version FROM wells WHERE id=?", (well_id,)).fetchone()
            if before is None:
                raise WellNotFound("well not found")
            if connection.execute("UPDATE wells SET status=?, version=version+1 WHERE id=? AND version=?", (status, well_id, version)).rowcount != 1:
                raise WellVersionConflict("record version is stale")
            after = connection.execute("SELECT id, status, version, updated_at FROM wells WHERE id=?", (well_id,)).fetchone()
            self.audit.record(actor_id, "well.status.update", "well", well_id, {"before": dict(before), "after": dict(after), "reason": reason.strip()}, connection, source_type="manual")
        return dict(after)
