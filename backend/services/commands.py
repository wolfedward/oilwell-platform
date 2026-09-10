"""Signed command delivery to virtual DSP+MCU terminals."""

from datetime import datetime, timedelta
import json
import re
import secrets
import sqlite3
import uuid

from backend.services.strategies import StrategyService, StrategyValidationError, WORKFLOW
from backend.services.workflows import WorkflowService
from backend.simulator import (
    SimulatorValidationError,
    TerminalUnavailable,
    VirtualTerminal,
    sign_control_command,
)
from backend.state_machine import transition


ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")
TERMINAL_COMMAND_STATES = {"success", "failed", "expired", "cancelled"}


class CommandValidationError(ValueError):
    pass


class CommandNotFound(RuntimeError):
    pass


def _json(value) -> str:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False,
    )


class CommandService:
    """Queue commands, persist staged receipts and commit terminal state safely."""

    def __init__(self, database, audit, registry):
        self.database = database
        self.audit = audit
        self.registry = registry
        self.workflows = WorkflowService(database, audit, clock=registry.now)

    def dispatch_strategy(
        self, strategy_id: str, actor_id: int, now: datetime | None = None,
    ) -> dict:
        now = self._time(now)
        strategy = self._strategy(strategy_id)
        idempotency_key = f"dispatch:{strategy_id}:{strategy['current_version_id']}"
        existing = self.database.query_one(
            "SELECT id FROM device_commands WHERE idempotency_key=?", (idempotency_key,),
        )
        if existing is not None:
            return self.get(existing["id"])
        transition(strategy["status"], "pending_dispatch", WORKFLOW)
        configuration = self._safe_configuration(strategy["configuration"])
        return self._enqueue(
            strategy, strategy["current_version_id"], configuration,
            "set_intermittent_cycle", idempotency_key, None, actor_id, now,
            move_to_pending=True,
        )

    def rollback(
        self, strategy_id: str, actor_id: int, reason: str,
        now: datetime | None = None,
    ) -> dict:
        now = self._time(now)
        if not isinstance(reason, str) or not 5 <= len(reason.strip()) <= 500:
            raise CommandValidationError("rollback reason must contain 5-500 characters")
        reason = reason.strip()
        strategy = self._strategy(strategy_id)
        existing = self.database.query_one(
            """SELECT id FROM device_commands
               WHERE strategy_id=? AND command_type='rollback_cycle'
               ORDER BY created_at DESC,id DESC LIMIT 1""",
            (strategy_id,),
        )
        if existing is not None:
            return self.get(existing["id"])
        if strategy["status"] != "effective":
            raise CommandValidationError("only an effective strategy can be rolled back")
        parent_id = strategy["parent_strategy_id"]
        if not parent_id:
            raise CommandValidationError("strategy has no previous approved cycle")
        parent = self._strategy(parent_id)
        if parent["well_id"] != strategy["well_id"] or parent["status"] != "effective":
            raise CommandValidationError("previous effective strategy is unavailable")
        configuration = self._safe_configuration(parent["configuration"])
        idempotency_key = f"rollback:{strategy_id}:{parent['current_version_id']}"
        existing = self.database.query_one(
            "SELECT id FROM device_commands WHERE idempotency_key=?", (idempotency_key,),
        )
        if existing is not None:
            return self.get(existing["id"])
        return self._enqueue(
            strategy, parent["current_version_id"], configuration,
            "rollback_cycle", idempotency_key, reason, actor_id, now,
            move_to_pending=False,
        )

    def process(self, now: datetime | None = None) -> dict:
        now = self._time(now)
        rows = self.database.query_all(
            """SELECT id FROM device_commands
               WHERE status='queued' AND (next_attempt_at IS NULL OR next_attempt_at<=?)
               ORDER BY created_at,id""",
            (now.isoformat(),),
        )
        summary = {"processed": 0, "succeeded": 0, "deferred": 0, "failed": 0}
        for row in rows:
            outcome = self._process_one(row["id"], now)
            if outcome == "skipped":
                continue
            summary["processed"] += 1
            summary[outcome] += 1
        return summary

    def get(self, command_id: str) -> dict:
        self._id(command_id, "command id")
        row = self.database.query_one(
            """SELECT command.*,device.well_id
               FROM device_commands AS command
               JOIN devices AS device ON device.id=command.device_id
               WHERE command.id=?""",
            (command_id,),
        )
        if row is None:
            raise CommandNotFound()
        result = self._public(row)
        result["receipts"] = [dict(item) for item in self.database.query_all(
            """SELECT id,attempt_number,state,receipt_at,result_detail,status,created_at
               FROM command_attempts WHERE command_id=? ORDER BY id""",
            (command_id,),
        )]
        for receipt in result["receipts"]:
            receipt["detail"] = self._loads(receipt.pop("result_detail"))
        return result

    def list_commands(self, filters: dict) -> dict:
        if not isinstance(filters, dict):
            raise CommandValidationError("filters must be an object")
        page, page_size = filters.get("page", 1), filters.get("page_size", 20)
        if isinstance(page, bool) or not isinstance(page, int) or not 1 <= page <= 1_000_000:
            raise CommandValidationError("page is out of range")
        if isinstance(page_size, bool) or not isinstance(page_size, int) or not 1 <= page_size <= 100:
            raise CommandValidationError("page_size is out of range")
        clauses, values = [], []
        for key, column in (
            ("device_id", "command.device_id"), ("strategy_id", "command.strategy_id"),
        ):
            if filters.get(key):
                self._id(filters[key], key.replace("_", " "))
                clauses.append(f"{column}=?")
                values.append(filters[key])
        if filters.get("state"):
            state = filters["state"]
            if state not in {"queued", *TERMINAL_COMMAND_STATES, "pending", "sent", "succeeded", "rolled_back"}:
                raise CommandValidationError("invalid command state")
            clauses.append("command.status=?")
            values.append(state)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        total = self.database.scalar(
            "SELECT COUNT(*) FROM device_commands AS command" + where, values,
        )
        rows = self.database.query_all(
            """SELECT command.*,device.well_id
               FROM device_commands AS command
               JOIN devices AS device ON device.id=command.device_id"""
            + where + " ORDER BY command.created_at DESC,command.id DESC LIMIT ? OFFSET ?",
            [*values, page_size, (page - 1) * page_size],
        )
        return {
            "items": [self._public(row) for row in rows],
            "page": page, "page_size": page_size, "total": total,
        }

    def _enqueue(
        self, strategy, target_version_id, configuration, command_type,
        idempotency_key, reason, actor_id, now, *, move_to_pending,
    ):
        device = self.database.query_one(
            "SELECT id FROM devices WHERE well_id=? ORDER BY id LIMIT 1",
            (strategy["well_id"],),
        )
        if device is None or self.registry.get(device["id"]) is None:
            raise CommandValidationError("well has no registered control terminal")
        selected = configuration["selected_candidate"]
        command_id = "CMD-" + uuid.uuid4().hex.upper()
        expires = now + timedelta(minutes=15)
        unsigned = {
            "schema_version": 1,
            "command_id": command_id,
            "device_id": device["id"],
            "strategy_id": strategy["id"],
            "strategy_version_id": target_version_id,
            "command_type": command_type,
            "on_minutes": selected["on_minutes"],
            "off_minutes": selected["off_minutes"],
            "issued_at": now.isoformat(),
            "expires_at": expires.isoformat(),
            "nonce": secrets.token_hex(16),
            "idempotency_key": idempotency_key,
            "reason": reason,
        }
        signature = sign_control_command(unsigned, self.registry.command_signing_key)
        payload = {**unsigned, "signature": signature}
        try:
            with self.database.transaction(
                actor_id, "command.dispatch", "device_command", command_id,
                source_type="manual", auto_audit=False,
            ) as connection:
                existing = connection.execute(
                    "SELECT id FROM device_commands WHERE idempotency_key=?",
                    (idempotency_key,),
                ).fetchone()
                if existing is not None:
                    return_id = existing["id"]
                else:
                    current = connection.execute(
                        "SELECT status,current_version_id FROM energy_strategies WHERE id=?",
                        (strategy["id"],),
                    ).fetchone()
                    expected_status = "approved" if move_to_pending else "effective"
                    if current is None or current["status"] != expected_status:
                        raise CommandValidationError("strategy state changed before dispatch")
                    if move_to_pending and current["current_version_id"] != target_version_id:
                        raise CommandValidationError("strategy version changed before dispatch")
                    connection.execute(
                        """INSERT INTO device_commands(
                               id,device_id,strategy_id,strategy_version_id,command_type,
                               payload,signature,idempotency_key,issued_at,expires_at,
                               next_attempt_at,attempt_count,max_attempts,status,actor_id,source_type
                           ) VALUES(?,?,?,?,?,?,?,?,?,?,?,0,3,'queued',?,'system')""",
                        (command_id, device["id"], strategy["id"], target_version_id,
                         command_type, _json(payload), signature, idempotency_key,
                         now.isoformat(), expires.isoformat(), now.isoformat(), actor_id),
                    )
                    if move_to_pending:
                        cursor = connection.execute(
                            """UPDATE energy_strategies SET status='pending_dispatch',
                                   version=version+1,actor_id=?
                               WHERE id=? AND status='approved' AND current_version_id=?""",
                            (actor_id, strategy["id"], target_version_id),
                        )
                        if cursor.rowcount != 1:
                            raise CommandValidationError("strategy state changed before dispatch")
                    self.audit.record(
                        actor_id, "command.dispatch", "device_command", command_id,
                        {"before": {}, "after": {
                            "strategy_id": strategy["id"],
                            "strategy_version_id": target_version_id,
                            "device_id": device["id"], "command_type": command_type,
                            "state": "queued", "expires_at": expires.isoformat(),
                            "idempotency_key": idempotency_key,
                        }}, connection,
                    )
                    return_id = command_id
        except sqlite3.IntegrityError as error:
            if "idempotency" not in str(error).lower():
                raise
            existing = self.database.query_one(
                "SELECT id FROM device_commands WHERE idempotency_key=?", (idempotency_key,),
            )
            if existing is None:
                raise
            return_id = existing["id"]
        return self.get(return_id)

    def _process_one(self, command_id: str, now: datetime) -> str:
        row = self._command_row(command_id)
        if row is None or row["status"] != "queued":
            return "skipped"
        try:
            expires = self._time(datetime.fromisoformat(row["expires_at"]))
        except (TypeError, ValueError):
            return self._finish_failure(row, now, "invalid_expiry", "failed")
        if now >= expires:
            return self._finish_failure(row, now, "command_expired", "expired")
        terminal = self.registry.get(row["device_id"])
        if terminal is None:
            return self._defer(row, now, "terminal_not_registered")
        envelope = self._loads(row["payload"])
        try:
            self._validate_envelope_binding(row, envelope)
        except CommandValidationError as error:
            return self._reject(row, now, str(error))
        with self.registry.scenario_lock:
            try:
                planned = terminal.preview_control_command(
                    envelope, now, self.registry.command_signing_key,
                )
            except TerminalUnavailable as error:
                return self._defer(row, now, str(error))
            except SimulatorValidationError as error:
                return self._reject(row, now, str(error))
            snapshot = terminal.snapshot_control_state()
            try:
                terminal.commit_control_command(planned)
            except Exception:
                terminal.restore_control_state(snapshot)
                return self._finish_failure(
                    row, now, "terminal_commit_failed", "failed",
                )
            try:
                outcome = self._succeed(row, now, planned)
            except BaseException:
                terminal.restore_control_state(snapshot)
                raise
            if outcome != "succeeded":
                terminal.restore_control_state(snapshot)
            return outcome

    def _defer(self, row, now, reason):
        attempt_number = row["attempt_count"] + 1
        exhausted = attempt_number >= row["max_attempts"]
        with self.database.transaction(
            row["actor_id"], "command.failed" if exhausted else "command.deferred",
            "device_command", row["id"], source_type="system", auto_audit=False,
        ) as connection:
            current = connection.execute(
                "SELECT status,attempt_count FROM device_commands WHERE id=?", (row["id"],),
            ).fetchone()
            if current is None or current["status"] != "queued" or current["attempt_count"] != row["attempt_count"]:
                return "skipped"
            self._receipt(connection, row["id"], attempt_number, "deferred", now, {"reason": reason})
            if exhausted:
                connection.execute(
                    """UPDATE device_commands SET status='failed',attempt_count=?,
                           completed_at=?,failure_reason=?,next_attempt_at=NULL WHERE id=?""",
                    (attempt_number, now.isoformat(), reason, row["id"]),
                )
                alarm_id = self._fail_strategy_and_alarm(connection, row, reason)
            else:
                next_attempt = now + timedelta(minutes=1)
                connection.execute(
                    """UPDATE device_commands SET attempt_count=?,next_attempt_at=?,
                           failure_reason=? WHERE id=?""",
                    (attempt_number, next_attempt.isoformat(), reason, row["id"]),
                )
                alarm_id = None
            self.audit.record(
                row["actor_id"], "command.failed" if exhausted else "command.deferred",
                "device_command", row["id"],
                {"before": {"state": "queued", "attempt_count": row["attempt_count"]},
                 "after": {"state": "failed" if exhausted else "queued",
                           "attempt_count": attempt_number, "reason": reason,
                           "alarm_id": alarm_id}}, connection, source_type="system",
            )
        return "failed" if exhausted else "deferred"

    def _reject(self, row, now, reason):
        attempt_number = row["attempt_count"] + 1
        with self.database.transaction(
            row["actor_id"], "command.failed", "device_command", row["id"],
            source_type="system", auto_audit=False,
        ) as connection:
            current = connection.execute(
                "SELECT status,attempt_count FROM device_commands WHERE id=?", (row["id"],),
            ).fetchone()
            if current is None or current["status"] != "queued" or current["attempt_count"] != row["attempt_count"]:
                return "skipped"
            for state in ("sent", "received", "rejected"):
                self._receipt(connection, row["id"], attempt_number, state, now, {"reason": reason})
            connection.execute(
                """UPDATE device_commands SET status='failed',attempt_count=?,completed_at=?,
                       failure_reason=?,next_attempt_at=NULL WHERE id=?""",
                (attempt_number, now.isoformat(), reason, row["id"]),
            )
            alarm_id = self._fail_strategy_and_alarm(connection, row, reason)
            self.audit.record(
                row["actor_id"], "command.failed", "device_command", row["id"],
                {"before": {"state": "queued"},
                 "after": {"state": "failed", "attempt_count": attempt_number,
                           "reason": reason, "alarm_id": alarm_id}},
                connection, source_type="system",
            )
        return "failed"

    def _finish_failure(self, row, now, reason, state):
        attempt_number = row["attempt_count"] + 1
        with self.database.transaction(
            row["actor_id"], "command.failed", "device_command", row["id"],
            source_type="system", auto_audit=False,
        ) as connection:
            current = connection.execute(
                "SELECT status,attempt_count FROM device_commands WHERE id=?", (row["id"],),
            ).fetchone()
            if current is None or current["status"] != "queued" or current["attempt_count"] != row["attempt_count"]:
                return "skipped"
            self._receipt(connection, row["id"], attempt_number, state, now, {"reason": reason})
            connection.execute(
                """UPDATE device_commands SET status=?,attempt_count=?,completed_at=?,
                       failure_reason=?,next_attempt_at=NULL WHERE id=?""",
                (state, attempt_number, now.isoformat(), reason, row["id"]),
            )
            alarm_id = self._fail_strategy_and_alarm(connection, row, reason)
            self.audit.record(
                row["actor_id"], "command.failed", "device_command", row["id"],
                {"before": {"state": "queued"},
                 "after": {"state": state, "attempt_count": attempt_number,
                           "reason": reason, "alarm_id": alarm_id}},
                connection, source_type="system",
            )
        return "failed"

    def _succeed(self, row, now, planned):
        attempt_number = row["attempt_count"] + 1
        target_status = "rolled_back" if row["command_type"] == "rollback_cycle" else "effective"
        expected_status = "effective" if row["command_type"] == "rollback_cycle" else "pending_dispatch"
        with self.database.transaction(
            row["actor_id"], "command.success", "device_command", row["id"],
            source_type="system", auto_audit=False,
        ) as connection:
            current = connection.execute(
                "SELECT status,attempt_count FROM device_commands WHERE id=?", (row["id"],),
            ).fetchone()
            if current is None or current["status"] != "queued" or current["attempt_count"] != row["attempt_count"]:
                return "skipped"
            for state in ("sent", "received", "validated", "executing", "success"):
                self._receipt(connection, row["id"], attempt_number, state, now, planned)
            connection.execute(
                """UPDATE device_commands SET status='success',attempt_count=?,completed_at=?,
                       failure_reason=NULL,next_attempt_at=NULL WHERE id=?""",
                (attempt_number, now.isoformat(), row["id"]),
            )
            if connection.execute(
                """UPDATE energy_strategies SET status=?,version=version+1,actor_id=?
                   WHERE id=? AND status=?""",
                (target_status, row["actor_id"], row["strategy_id"], expected_status),
            ).rowcount != 1:
                raise CommandValidationError("strategy is no longer ready for this command")
            self.audit.record(
                row["actor_id"], "command.success", "device_command", row["id"],
                {"before": {"state": "queued", "strategy_status": expected_status},
                 "after": {"state": "success", "strategy_status": target_status,
                           "attempt_count": attempt_number,
                           "strategy_version_id": row["strategy_version_id"]}},
                connection, source_type="system",
            )
        return "succeeded"

    def _fail_strategy_and_alarm(self, connection, row, reason):
        connection.execute(
            """UPDATE energy_strategies SET status='failed',version=version+1,actor_id=?
               WHERE id=? AND status IN ('pending_dispatch','effective')""",
            (row["actor_id"], row["strategy_id"]),
        )
        alarm = self.workflows.create_alarm(
            "command", row["id"], "critical",
            {"reason": reason, "command_id": row["id"]},
            actor_id=row["actor_id"], connection=connection, provenance="system",
        )
        return alarm["id"]

    @staticmethod
    def _receipt(connection, command_id, attempt_number, state, now, detail):
        connection.execute(
            """INSERT INTO command_attempts(
                   command_id,attempted_at,attempt_number,state,receipt_at,result_detail,
                   status,source_type
               ) VALUES(?,?,?,?,?,?,?,'virtual_device')""",
            (command_id, now.isoformat(), attempt_number, state, now.isoformat(),
             _json(detail), state),
        )

    def _strategy(self, strategy_id):
        self._id(strategy_id, "strategy id")
        row = self.database.query_one(
            """SELECT strategy.*,version.configuration
               FROM energy_strategies AS strategy
               LEFT JOIN strategy_versions AS version ON version.id=strategy.current_version_id
               WHERE strategy.id=?""",
            (strategy_id,),
        )
        if row is None or row["current_version_id"] is None:
            raise CommandNotFound()
        return row

    @staticmethod
    def _safe_configuration(raw):
        try:
            configuration = json.loads(raw)
        except (TypeError, json.JSONDecodeError):
            raise CommandValidationError("strategy configuration is invalid") from None
        try:
            StrategyService._assert_safe_configuration(configuration)
        except StrategyValidationError as error:
            raise CommandValidationError(str(error)) from error
        return configuration

    def _command_row(self, command_id):
        return self.database.query_one(
            """SELECT command.*,device.well_id
               FROM device_commands AS command
               JOIN devices AS device ON device.id=command.device_id
               WHERE command.id=?""",
            (command_id,),
        )

    def _validate_envelope_binding(self, row, envelope):
        expected = {
            "command_id": row["id"],
            "device_id": row["device_id"],
            "strategy_id": row["strategy_id"],
            "strategy_version_id": row["strategy_version_id"],
            "command_type": row["command_type"],
            "signature": row["signature"],
            "idempotency_key": row["idempotency_key"],
            "issued_at": row["issued_at"],
            "expires_at": row["expires_at"],
        }
        if not isinstance(envelope, dict) or any(
            envelope.get(key) != value for key, value in expected.items()
        ):
            raise CommandValidationError(
                "signed command does not match its persisted dispatch record"
            )
        version = self.database.query_one(
            """SELECT strategy_id,configuration,status,submitted_at
               FROM strategy_versions WHERE id=?""",
            (row["strategy_version_id"],),
        )
        strategy = self.database.query_one(
            "SELECT parent_strategy_id FROM energy_strategies WHERE id=?",
            (row["strategy_id"],),
        )
        expected_owner = row["strategy_id"]
        if row["command_type"] == "rollback_cycle":
            expected_owner = None if strategy is None else strategy["parent_strategy_id"]
        if (
            version is None or strategy is None or version["strategy_id"] != expected_owner
            or version["status"] == "draft" or version["submitted_at"] is None
        ):
            raise CommandValidationError(
                "control command is not linked to an immutable approved strategy version"
            )
        configuration = self._safe_configuration(version["configuration"])
        selected = configuration["selected_candidate"]
        if (
            envelope.get("on_minutes") != selected["on_minutes"]
            or envelope.get("off_minutes") != selected["off_minutes"]
        ):
            raise CommandValidationError(
                "control cycle does not match its approved strategy version"
            )

    @staticmethod
    def _public(row):
        keys = (
            "id", "device_id", "well_id", "strategy_id", "strategy_version_id",
            "command_type", "signature", "idempotency_key", "issued_at", "expires_at",
            "next_attempt_at", "attempt_count", "max_attempts", "completed_at",
            "failure_reason", "status", "actor_id", "source_type", "created_at", "updated_at",
        )
        result = {key: row[key] for key in keys}
        result["state"] = row["status"]
        result["payload"] = CommandService._loads(row["payload"])
        return result

    @staticmethod
    def _loads(raw):
        try:
            value = json.loads(raw)
        except (TypeError, json.JSONDecodeError):
            return {}
        return value if isinstance(value, dict) else {}

    def _time(self, value):
        try:
            return VirtualTerminal._aware(value if value is not None else self.registry.now())
        except SimulatorValidationError as error:
            raise CommandValidationError(str(error)) from error

    @staticmethod
    def _id(value, label):
        if not isinstance(value, str) or not ID_PATTERN.fullmatch(value):
            raise CommandValidationError(f"invalid {label}")
