"""Transactional alarm and maintenance work-order lifecycle service."""

from datetime import datetime, timedelta, timezone
import json
import threading
import re
import sqlite3
import uuid

from backend.state_machine import InvalidState, transition


SHANGHAI = timezone(timedelta(hours=8))
ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")
SOURCE_TYPES = {"diagnosis", "command", "strategy"}
SEVERITIES = {"info", "warning", "critical"}
WORK_ORDER_KINDS = {
    "inspection", "production_adjustment", "equipment_repair", "safety_check",
}
ALARM_WORKFLOW = {
    "new": {"confirmed", "processing", "closed", "false_positive", "escalated"},
    "confirmed": {"assigned", "processing", "closed", "false_positive", "escalated"},
    "assigned": {"processing", "closed", "escalated"},
    "processing": {"closed", "escalated"},
    "escalated": {"confirmed", "assigned", "processing", "closed", "false_positive"},
    "acknowledged": {"assigned", "processing", "closed", "escalated"},
    "dispatched": {"assigned", "processing", "closed", "escalated"},
    "closed": set(),
    "false_positive": set(),
}
ALARM_READ_STATES = set(ALARM_WORKFLOW)
WORK_ORDER_WORKFLOW = {
    "pending": {"accepted", "overdue"},
    "accepted": {"processing", "overdue"},
    "processing": {"review", "overdue"},
    "review": {"processing", "closed", "overdue"},
    "completed": {"processing", "closed", "overdue"},
    "overdue": {"accepted", "processing", "review", "closed"},
    "closed": set(),
}
WORK_ORDER_READ_STATES = set(WORK_ORDER_WORKFLOW) | {"completed"}


class WorkflowValidationError(ValueError):
    pass


class WorkflowNotFound(RuntimeError):
    pass


class WorkflowVersionConflict(RuntimeError):
    pass


def _json(value) -> str:
    try:
        return json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError):
        raise WorkflowValidationError("details must contain valid JSON values") from None


class WorkflowService:
    """Own all lifecycle transitions and their inseparable audit evidence."""

    def __init__(self, database, audit, clock=None):
        self.database = database
        self.audit = audit
        self.clock = clock or (lambda: datetime.now(SHANGHAI))

    def create_alarm(
        self, source_type, source_id, severity, details, actor_id=None,
        *, connection=None, provenance=None,
    ) -> dict:
        self._source_type(source_type)
        self._id(source_id, "source id")
        if severity not in SEVERITIES:
            raise WorkflowValidationError("invalid alarm severity")
        if not isinstance(details, dict):
            raise WorkflowValidationError("alarm details must be an object")
        details_json = _json(details)
        if provenance is None:
            provenance = "manual" if actor_id is not None else "system"
        if provenance not in {"manual", "system", "algorithm"}:
            raise WorkflowValidationError("invalid alarm provenance")
        if connection is not None:
            alarm_id = self._create_alarm_in_transaction(
                connection, source_type, source_id, severity, details_json, actor_id,
                provenance,
            )
            return self._get_alarm(alarm_id, connection)
        with self.database.transaction(
            actor_id, "alarm.create", "alarm", None,
            source_type="system" if actor_id is None else "manual", auto_audit=False,
            immediate=True,
        ) as transaction_connection:
            alarm_id = self._create_alarm_in_transaction(
                transaction_connection, source_type, source_id, severity,
                details_json, actor_id, provenance,
            )
        return self.get_alarm(alarm_id)

    def _create_alarm_in_transaction(
        self, connection, source_type, source_id, severity, details_json, actor_id,
        provenance,
    ):
        existing = connection.execute(
            """SELECT id FROM alarms
               WHERE source_object_type=? AND source_object_id=?
               ORDER BY created_at,id LIMIT 1""",
            (source_type, source_id),
        ).fetchone()
        if existing is not None:
            return existing["id"]
        source = self._resolve_source(connection, source_type, source_id)
        alarm_id = "AL-" + uuid.uuid4().hex.upper()
        connection.execute(
            """INSERT INTO alarms(
                   id,well_id,diagnosis_id,strategy_id,command_id,alarm_type,severity,
                   details_json,source_object_type,source_object_id,status,actor_id,source_type
               ) VALUES(?,?,?,?,?,?,?,?,?,?,'new',?,?)""",
            (
                alarm_id, source["well_id"], source.get("diagnosis_id"),
                source.get("strategy_id"), source.get("command_id"), source["alarm_type"],
                severity, details_json, source_type, source_id, actor_id, provenance,
            ),
        )
        self.audit.record(
            actor_id, "alarm.create", "alarm", alarm_id,
            {"before": {}, "after": {
                "status": "new", "severity": severity,
                "source_object_type": source_type, "source_object_id": source_id,
            }},
            connection, source_type=provenance,
        )
        return alarm_id

    def transition_alarm(self, alarm_id, target, actor, comment) -> dict:
        self._id(alarm_id, "alarm id")
        self._actor(actor)
        comment = self._comment(comment)
        if target in {"assigned", "processing", "closed"}:
            raise WorkflowValidationError(
                "assignment, processing and closure are controlled by work orders"
            )
        with self.database.transaction(
            actor, "alarm.transition", "alarm", alarm_id,
            source_type="manual", auto_audit=False,
        ) as connection:
            active_order = connection.execute(
                """SELECT id FROM work_orders
                   WHERE alarm_id=? AND status!='closed' ORDER BY created_at,id LIMIT 1""",
                (alarm_id,),
            ).fetchone()
            if active_order is not None and target != "escalated":
                raise WorkflowValidationError(
                    "an active work order controls this alarm lifecycle"
                )
            self._transition_alarm_in_transaction(
                connection, alarm_id, target, actor, comment,
            )
        return self.get_alarm(alarm_id)

    def _transition_alarm_in_transaction(
        self, connection, alarm_id, target, actor, comment,
    ):
        row = connection.execute("SELECT * FROM alarms WHERE id=?", (alarm_id,)).fetchone()
        if row is None:
            raise WorkflowNotFound()
        transition(row["status"], target, ALARM_WORKFLOW)
        now = self._now().isoformat()
        confirmed_at = now if target == "confirmed" else row["confirmed_at"]
        closed_at = now if target in {"closed", "false_positive"} else row["closed_at"]
        resolution = comment if target in {"closed", "false_positive"} else row["resolution_comment"]
        cursor = connection.execute(
            """UPDATE alarms SET status=?,confirmed_at=?,closed_at=?,resolution_comment=?,
                      actor_id=?,version=version+1
               WHERE id=? AND status=? AND version=?""",
            (
                target, confirmed_at, closed_at, resolution, actor, alarm_id,
                row["status"], row["version"],
            ),
        )
        if cursor.rowcount != 1:
            raise WorkflowVersionConflict()
        self.audit.record(
            actor, "alarm.transition", "alarm", alarm_id,
            {"before": {"status": row["status"], "version": row["version"]},
             "after": {"status": target, "version": row["version"] + 1},
             "comment": comment},
            connection,
        )

    def create_work_order(
        self, alarm_id, kind, assignee_id, actor, due_at=None,
    ) -> dict:
        self._id(alarm_id, "alarm id")
        self._actor(actor)
        self._actor(assignee_id, "assignee id")
        if kind not in WORK_ORDER_KINDS:
            raise WorkflowValidationError("invalid work-order kind")
        now = self._now()
        due = self._due(due_at, now)
        with self.database.transaction(
            actor, "work_order.create", "work_order", None,
            source_type="manual", auto_audit=False, immediate=True,
        ) as connection:
            alarm = connection.execute(
                """SELECT alarm.*,well.name AS well_name
                   FROM alarms AS alarm JOIN wells AS well ON well.id=alarm.well_id
                   WHERE alarm.id=?""",
                (alarm_id,),
            ).fetchone()
            if alarm is None:
                raise WorkflowNotFound()
            if alarm["status"] not in {"confirmed", "escalated"}:
                raise WorkflowValidationError(
                    "only confirmed or escalated alarms can be assigned"
                )
            assignee = connection.execute(
                """SELECT user.id FROM users AS user
                   JOIN role_permissions AS grant_row ON grant_row.role_id=user.role_id
                   JOIN permissions AS permission ON permission.id=grant_row.permission_id
                   WHERE user.id=? AND user.status='active' AND grant_row.status='active'
                     AND permission.status='active' AND permission.code='maintenance.manage'""",
                (assignee_id,),
            ).fetchone()
            if assignee is None:
                raise WorkflowValidationError(
                    "assignee must be an active maintenance operator"
                )
            existing = connection.execute(
                "SELECT id FROM work_orders WHERE alarm_id=?", (alarm_id,),
            ).fetchone()
            if existing is not None:
                raise WorkflowValidationError("alarm already has a work order")
            work_order_id = "WO-" + uuid.uuid4().hex.upper()
            title = f"{alarm['well_name']} {self._kind_name(kind)}"
            connection.execute(
                """INSERT INTO work_orders(
                       id,well_id,alarm_id,title,kind,assignee_id,due_at,status,
                       actor_id,source_type
                   ) VALUES(?,?,?,?,?,?,?,'pending',?,'manual')""",
                (
                    work_order_id, alarm["well_id"], alarm_id, title, kind,
                    assignee_id, due.isoformat(), actor,
                ),
            )
            self._log(
                connection, work_order_id, None, "pending", "工单已创建并指派",
                actor, now,
            )
            self._transition_alarm_in_transaction(
                connection, alarm_id, "assigned", actor, f"已派单 {work_order_id}",
            )
            self.audit.record(
                actor, "work_order.create", "work_order", work_order_id,
                {"before": {}, "after": {
                    "status": "pending", "alarm_id": alarm_id, "kind": kind,
                    "assignee_id": assignee_id, "due_at": due.isoformat(),
                }},
                connection,
            )
        return self.get_work_order(work_order_id)

    def transition_work_order(self, work_order_id, target, actor, comment) -> dict:
        self._id(work_order_id, "work-order id")
        self._actor(actor)
        comment = self._comment(comment)
        with self.database.transaction(
            actor, "work_order.transition", "work_order", work_order_id,
            source_type="manual", auto_audit=False,
        ) as connection:
            row = connection.execute(
                "SELECT * FROM work_orders WHERE id=?", (work_order_id,),
            ).fetchone()
            if row is None:
                raise WorkflowNotFound()
            if target == "overdue":
                raise WorkflowValidationError("overdue state is managed by the scheduler")
            transition(row["status"], target, WORK_ORDER_WORKFLOW)
            if row["status"] == "overdue":
                previous = row["overdue_from_status"]
                if previous not in WORK_ORDER_WORKFLOW or target not in WORK_ORDER_WORKFLOW[previous]:
                    raise InvalidState(
                        f"transition from overdue {previous!r} to {target!r} is not allowed"
                    )
            if target in {"accepted", "processing", "review"} and actor != row["assignee_id"]:
                raise WorkflowValidationError("only the assignee can perform this transition")
            if target == "closed" and actor == row["assignee_id"]:
                raise WorkflowValidationError("work order must be closed by an independent reviewer")
            self._update_work_order(connection, row, target, actor, comment, self._now())
        return self.get_work_order(work_order_id)

    def _update_work_order(self, connection, row, target, actor, comment, now):
        accepted_at = now.isoformat() if target == "accepted" else row["accepted_at"]
        completed_at = now.isoformat() if target == "review" else row["completed_at"]
        closed_at = now.isoformat() if target == "closed" else row["closed_at"]
        closed_by = actor if target == "closed" else row["closed_by"]
        overdue_from = row["status"] if target == "overdue" else row["overdue_from_status"]
        cursor = connection.execute(
            """UPDATE work_orders SET status=?,accepted_at=?,completed_at=?,closed_at=?,
                      closed_by=?,overdue_from_status=?,actor_id=?,version=version+1
               WHERE id=? AND status=? AND version=?""",
            (
                target, accepted_at, completed_at, closed_at, closed_by, overdue_from,
                actor, row["id"], row["status"], row["version"],
            ),
        )
        if cursor.rowcount != 1:
            raise WorkflowVersionConflict()
        self._log(connection, row["id"], row["status"], target, comment, actor, now)
        if target == "processing":
            alarm = connection.execute(
                "SELECT status FROM alarms WHERE id=?", (row["alarm_id"],),
            ).fetchone()
            if (
                alarm is not None
                and alarm["status"] not in {"processing", "closed", "false_positive"}
            ):
                self._transition_alarm_in_transaction(
                    connection, row["alarm_id"], "processing", actor,
                    f"工单 {row['id']} 开始处置",
                )
        elif target == "closed":
            alarm = connection.execute(
                "SELECT status FROM alarms WHERE id=?", (row["alarm_id"],),
            ).fetchone()
            other_active = connection.execute(
                """SELECT 1 FROM work_orders
                   WHERE alarm_id=? AND id!=? AND status!='closed' LIMIT 1""",
                (row["alarm_id"], row["id"]),
            ).fetchone()
            if (
                alarm is not None and other_active is None
                and alarm["status"] not in {"closed", "false_positive"}
            ):
                self._transition_alarm_in_transaction(
                    connection, row["alarm_id"], "closed", actor, comment,
                )
        action = "work_order.overdue" if target == "overdue" else "work_order.transition"
        self.audit.record(
            actor, action, "work_order", row["id"],
            {"before": {"status": row["status"], "version": row["version"]},
             "after": {"status": target, "version": row["version"] + 1},
             "comment": comment},
            connection, source_type="system" if target == "overdue" else "manual",
        )

    def evaluate_overdue(self, now=None) -> dict:
        current = self._now() if now is None else self._time(now)
        rows = self.database.query_all(
            """SELECT id FROM work_orders
               WHERE status IN ('pending','accepted','processing','review')
                 AND due_at IS NOT NULL AND due_at<? ORDER BY due_at,id""",
            (current.isoformat(),),
        )
        changed = 0
        for item in rows:
            with self.database.transaction(
                None, "work_order.overdue", "work_order", item["id"],
                source_type="system", auto_audit=False,
            ) as connection:
                row = connection.execute(
                    "SELECT * FROM work_orders WHERE id=?", (item["id"],),
                ).fetchone()
                if row is None or row["status"] not in {"pending", "accepted", "processing", "review"}:
                    continue
                if row["due_at"] is None or row["due_at"] >= current.isoformat():
                    continue
                self._update_work_order(
                    connection, row, "overdue", None, "超过要求完成时间", current,
                )
                changed += 1
        return {"evaluated": changed, "overdue": changed}

    def get_alarm(self, alarm_id) -> dict:
        self._id(alarm_id, "alarm id")
        row = self.database.query_one(
            """SELECT alarm.*,well.name AS well_name
               FROM alarms AS alarm JOIN wells AS well ON well.id=alarm.well_id
               WHERE alarm.id=?""",
            (alarm_id,),
        )
        if row is None:
            raise WorkflowNotFound()
        return self._alarm_public(row)

    def _get_alarm(self, alarm_id, connection):
        row = connection.execute(
            """SELECT alarm.*,well.name AS well_name
               FROM alarms AS alarm JOIN wells AS well ON well.id=alarm.well_id
               WHERE alarm.id=?""",
            (alarm_id,),
        ).fetchone()
        return self._alarm_public(row)

    def get_work_order(self, work_order_id) -> dict:
        self._id(work_order_id, "work-order id")
        row = self.database.query_one(
            """SELECT work_order.*,well.name AS well_name,user.display_name AS assignee_name
               FROM work_orders AS work_order
               LEFT JOIN wells AS well ON well.id=work_order.well_id
               LEFT JOIN users AS user ON user.id=work_order.assignee_id
               WHERE work_order.id=?""",
            (work_order_id,),
        )
        if row is None:
            raise WorkflowNotFound()
        result = self._order_public(row)
        result["logs"] = [dict(item) for item in self.database.query_all(
            """SELECT id,from_status,to_status,comment,changed_at,actor_id,source_type
               FROM work_order_logs WHERE work_order_id=? ORDER BY id""",
            (work_order_id,),
        )]
        return result

    def list_alarms(self, filters) -> dict:
        return self._list(filters, "alarm")

    def list_work_orders(self, filters) -> dict:
        return self._list(filters, "work_order")

    def _list(self, filters, resource):
        if not isinstance(filters, dict):
            raise WorkflowValidationError("filters must be an object")
        allowed = {"well_id", "status", "severity", "assignee_id", "page", "page_size"}
        if set(filters) - allowed:
            raise WorkflowValidationError("unknown workflow filter")
        page = self._integer(filters.get("page", 1), "page", 1_000_000)
        size = self._integer(filters.get("page_size", 20), "page_size", 100)
        clauses, values = [], []
        if filters.get("well_id"):
            self._id(filters["well_id"], "well id")
            clauses.append(f"{resource}.well_id=?")
            values.append(filters["well_id"])
        if filters.get("status"):
            known_states = ALARM_READ_STATES if resource == "alarm" else WORK_ORDER_READ_STATES
            if filters["status"] not in known_states:
                raise WorkflowValidationError("invalid workflow status")
            clauses.append(f"{resource}.status=?")
            values.append(filters["status"])
        if resource == "alarm" and filters.get("severity"):
            if filters["severity"] not in SEVERITIES:
                raise WorkflowValidationError("invalid alarm severity")
            clauses.append("alarm.severity=?")
            values.append(filters["severity"])
        if resource == "work_order" and filters.get("assignee_id") not in {None, ""}:
            assignee = self._integer(filters["assignee_id"], "assignee_id", 2_147_483_647)
            clauses.append("work_order.assignee_id=?")
            values.append(assignee)
        ignored = {"assignee_id"} if resource == "alarm" else {"severity"}
        if any(filters.get(key) not in {None, ""} for key in ignored):
            raise WorkflowValidationError("filter does not apply to this resource")
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        table = "alarms" if resource == "alarm" else "work_orders"
        total = self.database.scalar(
            f"SELECT COUNT(*) FROM {table} AS {resource}" + where, values,
        )
        if resource == "alarm":
            rows = self.database.query_all(
                "SELECT alarm.*,well.name AS well_name FROM alarms AS alarm "
                "JOIN wells AS well ON well.id=alarm.well_id" + where
                + " ORDER BY alarm.updated_at DESC,alarm.id DESC LIMIT ? OFFSET ?",
                [*values, size, (page - 1) * size],
            )
            items = [self._alarm_public(row) for row in rows]
        else:
            rows = self.database.query_all(
                "SELECT work_order.*,well.name AS well_name,user.display_name AS assignee_name "
                "FROM work_orders AS work_order LEFT JOIN wells AS well ON well.id=work_order.well_id "
                "LEFT JOIN users AS user ON user.id=work_order.assignee_id" + where
                + " ORDER BY work_order.updated_at DESC,work_order.id DESC LIMIT ? OFFSET ?",
                [*values, size, (page - 1) * size],
            )
            items = [self._order_public(row) for row in rows]
        return {"items": items, "page": page, "page_size": size, "total": total}

    def trace(self, work_order_id) -> dict:
        order = self.get_work_order(work_order_id)
        alarm = self.get_alarm(order["alarm_id"])
        strategy_id = alarm["strategy_id"]
        command_id = alarm["command_id"]
        approval_ids = [] if strategy_id is None else self.database.column(
            """SELECT id FROM approvals
               WHERE object_type='energy_strategy' AND object_id=? ORDER BY created_at,id""",
            (strategy_id,),
        )
        object_ids = [
            item for item in (
                work_order_id, alarm["id"], alarm["diagnosis_id"], strategy_id,
                command_id, *approval_ids,
            ) if item is not None
        ]
        placeholders = ",".join("?" for _ in object_ids)
        audit_ids = self.database.column(
            f"SELECT id FROM audit_logs WHERE object_id IN ({placeholders}) ORDER BY id",
            object_ids,
        ) if object_ids else []
        return {
            "work_order_id": work_order_id,
            "alarm_id": alarm["id"],
            "diagnosis_id": alarm["diagnosis_id"],
            "strategy_id": strategy_id,
            "approval_ids": approval_ids,
            "command_id": command_id,
            "audit_ids": audit_ids,
            "work_order": order,
            "alarm": alarm,
        }

    @staticmethod
    def _resolve_source(connection, source_type, source_id):
        if source_type == "diagnosis":
            row = connection.execute(
                "SELECT id,well_id,diagnosis_type FROM diagnoses WHERE id=?", (source_id,),
            ).fetchone()
            if row is not None:
                return {"well_id": row["well_id"], "diagnosis_id": row["id"],
                        "alarm_type": row["diagnosis_type"]}
        elif source_type == "command":
            row = connection.execute(
                """SELECT command.id,command.strategy_id,command.command_type,device.well_id
                   FROM device_commands AS command
                   JOIN devices AS device ON device.id=command.device_id WHERE command.id=?""",
                (source_id,),
            ).fetchone()
            if row is not None:
                return {"well_id": row["well_id"], "strategy_id": row["strategy_id"],
                        "command_id": row["id"], "alarm_type": "command_delivery_failed"}
        elif source_type == "strategy":
            row = connection.execute(
                "SELECT id,well_id FROM energy_strategies WHERE id=?", (source_id,),
            ).fetchone()
            if row is not None:
                return {"well_id": row["well_id"], "strategy_id": row["id"],
                        "alarm_type": "strategy_exception"}
        raise WorkflowNotFound()

    @staticmethod
    def _log(connection, order_id, before, after, comment, actor, now):
        connection.execute(
            """INSERT INTO work_order_logs(
                   work_order_id,message,from_status,to_status,comment,changed_at,
                   status,actor_id,source_type
               ) VALUES(?,?,?,?,?,?,?,?,?)""",
            (order_id, comment, before, after, comment, now.isoformat(), after, actor,
             "system" if actor is None else "manual"),
        )

    @staticmethod
    def _alarm_public(row):
        result = {key: row[key] for key in (
            "id", "well_id", "well_name", "diagnosis_id", "strategy_id", "command_id",
            "alarm_type", "severity", "source_object_type", "source_object_id",
            "confirmed_at", "closed_at", "resolution_comment", "version", "status",
            "actor_id", "source_type", "created_at", "updated_at",
        )}
        try:
            details = json.loads(row["details_json"])
        except (TypeError, json.JSONDecodeError):
            details = {}
        result["details"] = details if isinstance(details, dict) else {}
        return result

    @staticmethod
    def _order_public(row):
        keys = (
            "id", "well_id", "well_name", "alarm_id", "title", "kind", "assignee_id",
            "assignee_name", "due_at", "accepted_at", "completed_at", "closed_at",
            "closed_by", "overdue_from_status", "version", "status", "actor_id",
            "source_type", "created_at", "updated_at",
        )
        return {key: row[key] for key in keys}

    @staticmethod
    def _kind_name(kind):
        return {
            "inspection": "异常工况巡检", "production_adjustment": "生产参数调整",
            "equipment_repair": "设备检修", "safety_check": "安全复核",
        }[kind]

    def _due(self, value, now):
        if value is None:
            return now + timedelta(hours=4)
        due = self._time(value)
        if due <= now - timedelta(days=365):
            raise WorkflowValidationError("due_at is outside the supported range")
        if due > now + timedelta(days=30):
            raise WorkflowValidationError("due_at cannot exceed 30 days")
        return due

    def _now(self):
        return self._time(self.clock())

    @staticmethod
    def _time(value):
        if isinstance(value, str):
            try:
                value = datetime.fromisoformat(value)
            except ValueError:
                raise WorkflowValidationError("timestamp must be ISO 8601") from None
        if not isinstance(value, datetime) or value.tzinfo is None:
            raise WorkflowValidationError("timestamp must include timezone")
        return value.astimezone(SHANGHAI)

    @staticmethod
    def _comment(value):
        if not isinstance(value, str) or not 2 <= len(value.strip()) <= 500:
            raise WorkflowValidationError("comment must contain 2-500 characters")
        return value.strip()

    @staticmethod
    def _actor(value, label="actor id"):
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise WorkflowValidationError(f"{label} must be a positive integer")

    @staticmethod
    def _integer(value, label, maximum):
        if isinstance(value, str):
            try:
                number = int(value)
            except ValueError:
                raise WorkflowValidationError(f"{label} must be an integer") from None
            if str(number) != value:
                raise WorkflowValidationError(f"{label} must be an integer")
        else:
            number = value
        if isinstance(number, bool) or not isinstance(number, int) or not 1 <= number <= maximum:
            raise WorkflowValidationError(f"{label} is out of range")
        return number

    @staticmethod
    def _id(value, label):
        if not isinstance(value, str) or not ID_PATTERN.fullmatch(value):
            raise WorkflowValidationError(f"invalid {label}")

    @staticmethod
    def _source_type(value):
        if value not in SOURCE_TYPES:
            raise WorkflowValidationError("invalid alarm source type")


class WorkflowWorker:
    """Periodically advances due work orders without requiring an API caller."""

    def __init__(self, service: WorkflowService, *, poll_seconds=30.0):
        if (
            isinstance(poll_seconds, bool)
            or not isinstance(poll_seconds, (int, float))
            or poll_seconds <= 0
        ):
            raise ValueError("poll_seconds must be positive")
        self.service = service
        self.poll_seconds = float(poll_seconds)
        self._stop = threading.Event()
        self._thread = None

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run, name="workflow-overdue-worker", daemon=False,
        )
        self._thread.start()

    def stop(self):
        self._stop.set()
        thread = self._thread
        if thread and thread is not threading.current_thread():
            thread.join(timeout=7)
            if thread.is_alive():
                raise RuntimeError("workflow overdue worker did not stop")

    def is_alive(self):
        return bool(self._thread and self._thread.is_alive())

    def _run(self):
        while not self._stop.is_set():
            try:
                self.service.evaluate_overdue()
            except Exception as error:
                self._record_failure(error)
            self._stop.wait(self.poll_seconds)

    def _record_failure(self, error):
        try:
            with self.service.database.transaction(
                None, "workflow.worker_error", "workflow_worker", None,
                source_type="system",
            ) as connection:
                connection.execute(
                    """INSERT INTO system_events(
                           event_type,detail,status,source_type
                       ) VALUES('workflow_worker_error',?,'open','system')""",
                    (str(error)[:1000],),
                )
        except Exception:
            pass
