"""Versioned adaptive pumping strategies with auditable human approval."""

from dataclasses import asdict
import json
import math
import re
import sqlite3
import uuid

from backend.algorithms.energy import (
    EnergyValidationError,
    RecoveryModel,
    RecoveryPoint,
    StrategyConstraints,
    fit_recovery_curve,
    optimize_cycle,
)
from backend.state_machine import InvalidState, transition


ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")
CONSTRAINT_KEYS = {
    "min_on_minutes", "max_on_minutes", "min_off_minutes", "max_off_minutes",
    "min_liquid_m3_day", "max_starts_per_day", "motor_kw",
}
WORKFLOW = {
    "draft": {"pending_approval"},
    "pending_approval": {"approved", "rejected"},
    "approved": {"pending_dispatch"},
    "pending_dispatch": {"effective", "failed"},
    "effective": {"paused", "rolled_back"},
    "paused": {"pending_dispatch", "rolled_back"},
}


class StrategyValidationError(ValueError):
    pass


class StrategyNotFound(RuntimeError):
    pass


class StrategyVersionConflict(RuntimeError):
    pass


def _json(value) -> str:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False,
    )


def _plain(value):
    if hasattr(value, "__dataclass_fields__"):
        return _plain(asdict(value))
    if isinstance(value, dict):
        return {key: _plain(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_plain(item) for item in value]
    return value


class StrategyService:
    """Own optimization, immutable versions and separation-of-duties approval."""

    def __init__(self, database, audit):
        self.database = database
        self.audit = audit

    def optimize(self, well_id: str, constraint_values: dict, actor_id: int) -> dict:
        self._id(well_id, "well id")
        constraints, motor_kw, normalized = self._constraints(constraint_values)
        well = self.database.query_one(
            "SELECT id,name,is_low_output,status FROM wells WHERE id=?", (well_id,),
        )
        if well is None:
            raise StrategyNotFound()
        if not well["is_low_output"]:
            raise StrategyValidationError("adaptive pumping is restricted to low-output wells")
        recovery = self.database.query_one(
            """SELECT id,tested_at,result_data FROM recovery_tests
               WHERE well_id=? AND status='completed'
               ORDER BY tested_at DESC,id DESC LIMIT 1""",
            (well_id,),
        )
        if recovery is None:
            raise StrategyValidationError("a completed recovery test is required")
        points, recovery_payload = self._recovery_points(recovery["result_data"])
        try:
            model = fit_recovery_curve(points)
            candidates = optimize_cycle(model, constraints, motor_kw)
        except EnergyValidationError as error:
            raise StrategyValidationError(str(error)) from error
        feasible = [candidate for candidate in candidates if candidate.feasible]
        if not feasible:
            raise StrategyValidationError("no safe cycle satisfies the production constraints")
        configuration = {
            "schema_version": 1,
            "recovery_test_id": recovery["id"],
            "recovery_tested_at": recovery["tested_at"],
            "recovery_observations": recovery_payload["observations"],
            "recovery_units": recovery_payload["units"],
            "recovery_model": _plain(model),
            "constraints": normalized,
            "selected_candidate": _plain(feasible[0]),
            "ranked_candidates": [_plain(candidate) for candidate in feasible[:10]],
        }
        strategy_id = "STR-" + uuid.uuid4().hex.upper()
        strategy_version_id = "SV-" + uuid.uuid4().hex.upper()
        with self.database.transaction(
            actor_id, "strategy.optimize", "energy_strategy", strategy_id,
            source_type="algorithm", auto_audit=False,
        ) as connection:
            connection.execute(
                """INSERT INTO energy_strategies(
                       id,well_id,name,version,created_by,status,actor_id,source_type
                   ) VALUES(?,?,?,1,?,'draft',?,'algorithm')""",
                (strategy_id, well_id, f"{well['name']} 自适应间抽节能策略", actor_id, actor_id),
            )
            connection.execute(
                """INSERT INTO strategy_versions(
                       id,strategy_id,version_number,configuration,created_by,status,
                       actor_id,source_type
                   ) VALUES(?,?,1,?,?,'draft',?,'algorithm')""",
                (strategy_version_id, strategy_id, _json(configuration), actor_id, actor_id),
            )
            connection.execute(
                "UPDATE energy_strategies SET current_version_id=? WHERE id=?",
                (strategy_version_id, strategy_id),
            )
            self.audit.record(
                actor_id, "strategy.optimize", "energy_strategy", strategy_id,
                {"before": {}, "after": {
                    "well_id": well_id, "version": 1,
                    "strategy_version_id": strategy_version_id,
                    "recovery_test_id": recovery["id"],
                    "selected_candidate": configuration["selected_candidate"],
                }}, connection, source_type="algorithm",
            )
        return self.detail(strategy_id)

    def update(
        self, strategy_id: str, changes: dict, version: int, actor_id: int,
    ) -> dict:
        self._id(strategy_id, "strategy id")
        expected_version = self._version(version)
        if not isinstance(changes, dict) or not changes or set(changes) - {"on_minutes", "off_minutes"}:
            raise StrategyValidationError("only on_minutes and off_minutes may be changed")
        row = self._row(strategy_id)
        if row["version"] != expected_version:
            raise StrategyVersionConflict()
        if row["status"] != "draft":
            raise InvalidState("only draft strategies can be revised")
        configuration = self._configuration(row["configuration"])
        selected = configuration.get("selected_candidate")
        if not isinstance(selected, dict):
            raise StrategyValidationError("strategy configuration is not editable")
        on_minutes = changes.get("on_minutes", selected.get("on_minutes"))
        off_minutes = changes.get("off_minutes", selected.get("off_minutes"))
        if isinstance(on_minutes, bool) or not isinstance(on_minutes, int):
            raise StrategyValidationError("on_minutes must be an integer")
        if isinstance(off_minutes, bool) or not isinstance(off_minutes, int):
            raise StrategyValidationError("off_minutes must be an integer")
        model, constraints, motor_kw = self._objects_from_configuration(configuration)
        try:
            candidates = optimize_cycle(model, constraints, motor_kw)
        except EnergyValidationError as error:
            raise StrategyValidationError(str(error)) from error
        chosen = next((candidate for candidate in candidates
                       if candidate.on_minutes == on_minutes and candidate.off_minutes == off_minutes), None)
        if chosen is None:
            raise StrategyValidationError("cycle must use a supported ten/fifteen-minute interval")
        if not chosen.feasible:
            raise StrategyValidationError("cycle violates production or start-count constraints")
        new_configuration = json.loads(_json(configuration))
        new_configuration["selected_candidate"] = _plain(chosen)
        next_version = expected_version + 1
        strategy_version_id = "SV-" + uuid.uuid4().hex.upper()
        with self.database.transaction(
            actor_id, "strategy.update", "energy_strategy", strategy_id,
            source_type="manual", auto_audit=False,
        ) as connection:
            current = connection.execute(
                "SELECT version,status FROM energy_strategies WHERE id=?", (strategy_id,),
            ).fetchone()
            if current is None:
                raise StrategyNotFound()
            if current["version"] != expected_version:
                raise StrategyVersionConflict()
            if current["status"] != "draft":
                raise InvalidState("only draft strategies can be revised")
            try:
                connection.execute(
                    """INSERT INTO strategy_versions(
                           id,strategy_id,version_number,configuration,created_by,status,
                           actor_id,source_type
                       ) VALUES(?,?,?,?,?,'draft',?,'manual')""",
                    (strategy_version_id, strategy_id, next_version,
                     _json(new_configuration), actor_id, actor_id),
                )
            except sqlite3.IntegrityError as error:
                if "strategy_versions.strategy_id, strategy_versions.version_number" in str(error):
                    raise StrategyVersionConflict() from None
                raise
            cursor = connection.execute(
                """UPDATE energy_strategies SET current_version_id=?,version=version+1,
                       actor_id=? WHERE id=? AND version=? AND status='draft'""",
                (strategy_version_id, actor_id, strategy_id, expected_version),
            )
            if cursor.rowcount != 1:
                raise StrategyVersionConflict()
            self.audit.record(
                actor_id, "strategy.update", "energy_strategy", strategy_id,
                {"before": {"version": expected_version, "selected_candidate": selected},
                 "after": {"version": next_version, "strategy_version_id": strategy_version_id,
                           "selected_candidate": new_configuration["selected_candidate"]}},
                connection,
            )
        return self.detail(strategy_id)

    def submit(self, strategy_id: str, version: int, actor_id: int) -> dict:
        self._id(strategy_id, "strategy id")
        expected_version = self._version(version)
        row = self._row(strategy_id)
        if row["version"] != expected_version:
            raise StrategyVersionConflict()
        transition(row["status"], "pending_approval", WORKFLOW)
        configuration = self._configuration(row["configuration"])
        self._assert_safe_configuration(configuration)
        approval_id = "APR-" + uuid.uuid4().hex.upper()
        next_version = expected_version + 1
        with self.database.transaction(
            actor_id, "strategy.submit", "energy_strategy", strategy_id,
            source_type="manual", auto_audit=False,
        ) as connection:
            cursor = connection.execute(
                """UPDATE energy_strategies SET status='pending_approval',
                       submitted_by=?,submitted_at=strftime('%Y-%m-%dT%H:%M:%f+08:00','now','+8 hours'),
                       version=version+1,actor_id=?
                   WHERE id=? AND version=? AND status='draft'""",
                (actor_id, actor_id, strategy_id, expected_version),
            )
            if cursor.rowcount != 1:
                raise StrategyVersionConflict()
            connection.execute(
                """UPDATE strategy_versions SET status='submitted',submitted_at=
                       strftime('%Y-%m-%dT%H:%M:%f+08:00','now','+8 hours'),actor_id=?
                   WHERE id=? AND status='draft'""",
                (actor_id, row["current_version_id"]),
            )
            connection.execute(
                """INSERT INTO approvals(
                       id,object_type,object_id,object_version,decision,status,source_type
                   ) VALUES(?,'energy_strategy',?,?,NULL,'pending','manual')""",
                (approval_id, strategy_id, next_version),
            )
            self.audit.record(
                actor_id, "strategy.submit", "energy_strategy", strategy_id,
                {"before": {"status": "draft", "version": expected_version},
                 "after": {"status": "pending_approval", "version": next_version,
                           "approval_id": approval_id}}, connection,
            )
        return self.detail(strategy_id)

    def decide(
        self, strategy_id: str, decision: str, comment: str,
        actor_id: int, version: int,
    ) -> dict:
        self._id(strategy_id, "strategy id")
        expected_version = self._version(version)
        if decision not in {"approve", "reject"}:
            raise StrategyValidationError("decision must be approve or reject")
        if not isinstance(comment, str) or not 2 <= len(comment.strip()) <= 500:
            raise StrategyValidationError("approval comment must contain 2-500 characters")
        comment = comment.strip()
        row = self._row(strategy_id)
        if row["version"] != expected_version:
            raise StrategyVersionConflict()
        target = "approved" if decision == "approve" else "rejected"
        transition(row["status"], target, WORKFLOW)
        if actor_id in {row["created_by"], row["submitted_by"]}:
            raise StrategyValidationError("strategy creator or submitter cannot approve it")
        self._assert_safe_configuration(self._configuration(row["configuration"]))
        next_version = expected_version + 1
        with self.database.transaction(
            actor_id, f"strategy.{decision}", "energy_strategy", strategy_id,
            source_type="manual", auto_audit=False,
        ) as connection:
            cursor = connection.execute(
                """UPDATE energy_strategies SET status=?,decision_by=?,decision_at=
                       strftime('%Y-%m-%dT%H:%M:%f+08:00','now','+8 hours'),
                       approval_comment=?,version=version+1,actor_id=?
                   WHERE id=? AND version=? AND status='pending_approval'""",
                (target, actor_id, comment, actor_id, strategy_id, expected_version),
            )
            if cursor.rowcount != 1:
                raise StrategyVersionConflict()
            approval = connection.execute(
                """SELECT id FROM approvals WHERE object_type='energy_strategy'
                   AND object_id=? AND status='pending'
                   ORDER BY created_at DESC,id DESC LIMIT 1""",
                (strategy_id,),
            ).fetchone()
            if approval is None:
                raise StrategyValidationError("pending approval record is missing")
            connection.execute(
                """UPDATE approvals SET decision=?,comment=?,decided_at=
                       strftime('%Y-%m-%dT%H:%M:%f+08:00','now','+8 hours'),
                       status='decided',actor_id=? WHERE id=? AND status='pending'""",
                (target, comment, actor_id, approval["id"]),
            )
            self.audit.record(
                actor_id, f"strategy.{decision}", "energy_strategy", strategy_id,
                {"before": {"status": "pending_approval", "version": expected_version},
                 "after": {"status": target, "version": next_version,
                           "comment": comment, "approval_id": approval["id"]}},
                connection,
            )
        return self.detail(strategy_id)

    def copy(self, strategy_id: str, actor_id: int) -> dict:
        self._id(strategy_id, "strategy id")
        source = self._row(strategy_id)
        if source["status"] not in {"approved", "rejected", "effective", "paused", "rolled_back"}:
            raise InvalidState("only decided or previously effective strategies can be copied")
        configuration = self._configuration(source["configuration"])
        new_id = "STR-" + uuid.uuid4().hex.upper()
        version_id = "SV-" + uuid.uuid4().hex.upper()
        with self.database.transaction(
            actor_id, "strategy.copy", "energy_strategy", new_id,
            source_type="manual", auto_audit=False,
        ) as connection:
            connection.execute(
                """INSERT INTO energy_strategies(
                       id,well_id,parent_strategy_id,name,version,
                       created_by,status,actor_id,source_type
                   ) VALUES(?,?,?,?,1,?,'draft',?,'manual')""",
                (new_id, source["well_id"], strategy_id,
                 f"{source['name']} - 修订", actor_id, actor_id),
            )
            connection.execute(
                """INSERT INTO strategy_versions(
                       id,strategy_id,version_number,configuration,created_by,status,
                       actor_id,source_type
                   ) VALUES(?,?,1,?,?,'draft',?,'manual')""",
                (version_id, new_id, _json(configuration), actor_id, actor_id),
            )
            connection.execute(
                "UPDATE energy_strategies SET current_version_id=? WHERE id=?",
                (version_id, new_id),
            )
            self.audit.record(
                actor_id, "strategy.copy", "energy_strategy", new_id,
                {"before": {}, "after": {"parent_strategy_id": strategy_id,
                                           "strategy_version_id": version_id}},
                connection,
            )
        return self.detail(new_id)

    def detail(self, strategy_id: str) -> dict:
        self._id(strategy_id, "strategy id")
        row = self._row(strategy_id)
        result = self._public(row)
        result["approvals"] = [dict(item) for item in self.database.query_all(
            """SELECT id,object_version,decision,comment,decided_at,status,actor_id,created_at
               FROM approvals WHERE object_type='energy_strategy' AND object_id=?
               ORDER BY created_at DESC,id DESC""",
            (strategy_id,),
        )]
        result["versions"] = [dict(item) for item in self.database.query_all(
            """SELECT id,version_number,status,created_by,submitted_at,created_at,updated_at
               FROM strategy_versions WHERE strategy_id=? ORDER BY version_number DESC""",
            (strategy_id,),
        )]
        return result

    def list_strategies(self, filters: dict) -> dict:
        if not isinstance(filters, dict):
            raise StrategyValidationError("filters must be an object")
        page = filters.get("page", 1)
        page_size = filters.get("page_size", 20)
        if isinstance(page, bool) or not isinstance(page, int) or page < 1:
            raise StrategyValidationError("page is out of range")
        if isinstance(page_size, bool) or not isinstance(page_size, int) or not 1 <= page_size <= 100:
            raise StrategyValidationError("page_size is out of range")
        clauses, parameters = [], []
        if "well_id" in filters and filters["well_id"] not in {None, ""}:
            self._id(filters["well_id"], "well id")
            clauses.append("strategy.well_id=?")
            parameters.append(filters["well_id"])
        if "status" in filters and filters["status"] not in {None, ""}:
            status = filters["status"]
            known = set(WORKFLOW) | {target for targets in WORKFLOW.values() for target in targets}
            if status not in known:
                raise StrategyValidationError("invalid strategy status")
            clauses.append("strategy.status=?")
            parameters.append(status)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        total = self.database.scalar(
            "SELECT COUNT(*) FROM energy_strategies AS strategy" + where, parameters,
        )
        rows = self.database.query_all(
            """SELECT strategy.*,well.name AS well_name,version.configuration,
                      version.version_number AS configuration_version
               FROM energy_strategies AS strategy
               JOIN wells AS well ON well.id=strategy.well_id
               LEFT JOIN strategy_versions AS version ON version.id=strategy.current_version_id"""
            + where + " ORDER BY strategy.updated_at DESC,strategy.id DESC LIMIT ? OFFSET ?",
            [*parameters, page_size, (page - 1) * page_size],
        )
        return {
            "items": [self._public(row) for row in rows],
            "page": page, "page_size": page_size, "total": total,
        }

    def _row(self, strategy_id: str):
        row = self.database.query_one(
            """SELECT strategy.*,well.name AS well_name,version.configuration,
                      version.version_number AS configuration_version
               FROM energy_strategies AS strategy
               JOIN wells AS well ON well.id=strategy.well_id
               LEFT JOIN strategy_versions AS version ON version.id=strategy.current_version_id
               WHERE strategy.id=?""",
            (strategy_id,),
        )
        if row is None:
            raise StrategyNotFound()
        return row

    @staticmethod
    def _public(row) -> dict:
        keys = (
            "id", "well_id", "well_name", "parent_strategy_id", "current_version_id",
            "name", "version", "configuration_version", "created_by", "submitted_by",
            "decision_by", "submitted_at", "decision_at", "approval_comment", "status",
            "source_type", "created_at", "updated_at",
        )
        result = {key: row[key] for key in keys}
        result["configuration"] = StrategyService._configuration(row["configuration"])
        return result

    @staticmethod
    def _configuration(raw):
        if not isinstance(raw, str):
            return {}
        try:
            value = json.loads(raw)
        except json.JSONDecodeError:
            return {}
        return value if isinstance(value, dict) else {}

    @staticmethod
    def _constraints(values):
        if not isinstance(values, dict) or set(values) != CONSTRAINT_KEYS:
            raise StrategyValidationError("constraints must contain the seven engineering limits")
        integer_keys = {
            "min_on_minutes", "max_on_minutes", "min_off_minutes", "max_off_minutes",
            "max_starts_per_day",
        }
        normalized = {}
        for key in integer_keys:
            value = values[key]
            if isinstance(value, bool) or not isinstance(value, int):
                raise StrategyValidationError(f"{key} must be an integer")
            normalized[key] = value
        for key in {"min_liquid_m3_day", "motor_kw"}:
            value = values[key]
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise StrategyValidationError(f"{key} must be numeric")
            value = float(value)
            if not math.isfinite(value):
                raise StrategyValidationError(f"{key} must be finite")
            normalized[key] = value
        if not (20 <= normalized["min_on_minutes"] <= normalized["max_on_minutes"] <= 240):
            raise StrategyValidationError("on-time limits must stay within 20-240 minutes")
        if not (15 <= normalized["min_off_minutes"] <= normalized["max_off_minutes"] <= 360):
            raise StrategyValidationError("off-time limits must stay within 15-360 minutes")
        if not 1 <= normalized["max_starts_per_day"] <= 24:
            raise StrategyValidationError("max_starts_per_day must stay within 1-24")
        if not .01 <= normalized["min_liquid_m3_day"] <= 1000:
            raise StrategyValidationError("minimum liquid target is outside the engineering range")
        if not .1 <= normalized["motor_kw"] <= 200:
            raise StrategyValidationError("motor_kw is outside the configured equipment range")
        constraints = StrategyConstraints(
            normalized["min_on_minutes"], normalized["max_on_minutes"],
            normalized["min_off_minutes"], normalized["max_off_minutes"],
            normalized["min_liquid_m3_day"], normalized["max_starts_per_day"],
        )
        return constraints, normalized["motor_kw"], normalized

    @staticmethod
    def _recovery_points(raw):
        try:
            payload = json.loads(raw)
        except (TypeError, json.JSONDecodeError):
            raise StrategyValidationError("recovery result is not valid JSON") from None
        if not isinstance(payload, dict) or not isinstance(payload.get("observations"), list):
            raise StrategyValidationError("recovery observations are missing")
        if payload.get("units") != {"time": "min", "liquid_rate": "m³/d"}:
            raise StrategyValidationError("recovery units must be minutes and cubic metres per day")
        points = []
        for item in payload["observations"]:
            if not isinstance(item, dict) or set(item) != {"shut_in_minutes", "liquid_rate_m3_day"}:
                raise StrategyValidationError("recovery observation shape is invalid")
            points.append(RecoveryPoint(item["shut_in_minutes"], item["liquid_rate_m3_day"]))
        return points, payload

    @staticmethod
    def _objects_from_configuration(configuration):
        try:
            model_data = configuration["recovery_model"]
            constraint_data = configuration["constraints"]
            model = RecoveryModel(
                capacity=model_data["capacity"], tau_minutes=model_data["tau_minutes"],
                baseline=model_data["baseline"], r_squared=model_data["r_squared"],
                observation_count=model_data["observation_count"],
                formula=model_data["formula"],
                units=tuple(tuple(item) for item in model_data["units"]),
                model_version=model_data["model_version"],
            )
            constraints = StrategyConstraints(
                constraint_data["min_on_minutes"], constraint_data["max_on_minutes"],
                constraint_data["min_off_minutes"], constraint_data["max_off_minutes"],
                constraint_data["min_liquid_m3_day"], constraint_data["max_starts_per_day"],
            )
            motor_kw = constraint_data["motor_kw"]
        except (KeyError, TypeError):
            raise StrategyValidationError("strategy configuration is incomplete") from None
        return model, constraints, motor_kw

    @classmethod
    def _assert_safe_configuration(cls, configuration):
        model, constraints, motor_kw = cls._objects_from_configuration(configuration)
        _, _, normalized = cls._constraints({
            "min_on_minutes": constraints.min_on_minutes,
            "max_on_minutes": constraints.max_on_minutes,
            "min_off_minutes": constraints.min_off_minutes,
            "max_off_minutes": constraints.max_off_minutes,
            "min_liquid_m3_day": constraints.min_liquid_m3_day,
            "max_starts_per_day": constraints.max_starts_per_day,
            "motor_kw": motor_kw,
        })
        selected = configuration.get("selected_candidate")
        if not isinstance(selected, dict):
            raise StrategyValidationError("selected cycle is missing")
        try:
            candidates = optimize_cycle(model, constraints, normalized["motor_kw"])
        except EnergyValidationError as error:
            raise StrategyValidationError(str(error)) from error
        exact = next((candidate for candidate in candidates
                      if candidate.on_minutes == selected.get("on_minutes")
                      and candidate.off_minutes == selected.get("off_minutes")), None)
        if exact is None or not exact.feasible:
            raise StrategyValidationError("selected cycle is outside approved safety limits")

    @staticmethod
    def _id(value, label):
        if not isinstance(value, str) or not ID_PATTERN.fullmatch(value):
            raise StrategyValidationError(f"invalid {label}")

    @staticmethod
    def _version(value):
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            raise StrategyValidationError("version must be a positive integer")
        return value
