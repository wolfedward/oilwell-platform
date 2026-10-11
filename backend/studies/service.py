"""Authenticated, append-only engineering study operations."""
import json
import threading
from uuid import uuid4

from backend.auth import AuthError, PermissionDenied, SessionExpired

from .contracts import (KINDS, SELECTABLE, StudyError, canonical, digest, uuid,
                        validate_envelope, validate_review, validate_selection)
from .permissions import CREATE, EXPORT, READ, REVIEW, SELECT

_COMPUTE_SLOT = threading.BoundedSemaphore(1)
_RESULT_STATUSES = {
    "condition_training": frozenset(("completed",)),
    "condition_prediction": frozenset(("completed",)),
    "recovery_fit": frozenset(("fitted", "rejected")),
    "energy_schedule": frozenset(("feasible", "no_feasible_candidate")),
    "energy_effects": frozenset(("descriptive", "insufficient_evidence")),
    "motor_spectrum": frozenset(("completed",)),
    "protection_replay": frozenset(("completed",)),
}
_APPROVABLE = {
    "condition_training": "completed", "condition_prediction": "completed",
    "recovery_fit": "fitted", "energy_schedule": "feasible",
    "energy_effects": "descriptive", "motor_spectrum": "completed",
    "protection_replay": "completed",
}
_SAFE_RESULT_FLAGS = {"field_validation": False, "control_eligible": False,
                      "fault_confirmation": False, "hardware_execution": False,
                      "dispatch_allowed": False, "causal_effect_proven": False,
                      "shadow_only": True}


def _authority(auth, token, permission):
    try:
        return auth.require(token, permission)
    except PermissionDenied:
        raise StudyError("forbidden", "Permission denied", 403) from None
    except (AuthError, SessionExpired):
        raise StudyError("unauthenticated", "Authentication required", 401) from None


class StudyService:
    """Engines: kind -> (algorithm_version: str, callable(payload) -> finite dict)."""

    def __init__(self, database, auth, engines):
        self.database, self.auth, self.engines = database, auth, dict(engines)

    def _receipt(self, actor_id, request_id, action, body_hash, connection=None):
        row = (connection.execute("SELECT action,body_sha256,response_json FROM study_receipts WHERE actor_id=? AND request_id=?", (actor_id, request_id)).fetchone()
               if connection else self.database.query_one("SELECT action,body_sha256,response_json FROM study_receipts WHERE actor_id=? AND request_id=?", (actor_id, request_id)))
        if row is None:
            return None
        if row["action"] != action or row["body_sha256"] != body_hash:
            raise StudyError("request_conflict", "request_id was used with different request content", 409)
        return json.loads(row["response_json"])

    @staticmethod
    def _save_receipt(conn, actor_id, request_id, action, body_hash, response):
        conn.execute("INSERT INTO study_receipts(actor_id,request_id,action,body_sha256,response_json) VALUES(?,?,?,?,?)",
                     (actor_id, request_id, action, body_hash, canonical(response)))

    @staticmethod
    def _audit(conn, actor_id, action, object_type, object_id, after):
        conn.execute("""INSERT INTO audit_logs(actor_id,action,object_type,object_id,details_json,source_type)
            VALUES(?,?,?,?,?,'manual')""",
            (actor_id, action, object_type, object_id, canonical({"before": {}, "after": after})))

    def _row(self, conn, record_id):
        uuid(record_id, "record_id")
        row = conn.execute("SELECT * FROM study_records WHERE id=?", (record_id,)).fetchone()
        if row is None:
            raise StudyError("not_found", "study record not found", 404)
        return row

    @staticmethod
    def _review_state(conn, record_id):
        row = conn.execute("SELECT version,decision FROM study_reviews WHERE record_id=? ORDER BY version DESC LIMIT 1", (record_id,)).fetchone()
        return (row["version"] + 1, row["decision"]) if row else (1, "unreviewed")

    @staticmethod
    def _selection_state(conn, kind):
        row = conn.execute("SELECT version,record_id,review_version FROM study_selections WHERE kind=? ORDER BY version DESC LIMIT 1", (kind,)).fetchone()
        return (row["version"], row["record_id"], row["review_version"]) if row else (0, None, None)

    def _dependency(self, conn, record_id, kind, require_selected=False,
                    include_result=False, include_dependencies=False):
        uuid(record_id, "record_id")
        columns = "id,kind,source_kind,result_sha256,result_status"
        if include_result:
            columns += ",result_json"
        if include_dependencies:
            columns += ",dependencies_json"
        row = conn.execute(f"SELECT {columns} FROM study_records WHERE id=?", (record_id,)).fetchone()
        if row is None:
            raise StudyError("not_found", "study record not found", 404)
        if row["kind"] != kind:
            raise StudyError("invalid_dependency", "dependency kind mismatch")
        version, state = self._review_state(conn, record_id)
        if state != "approved":
            raise StudyError("invalid_dependency", "dependency is not currently approved", 409)
        if require_selected:
            _, selected_id, selected_review = self._selection_state(conn, kind)
            if selected_id != record_id or selected_review != version - 1:
                raise StudyError("invalid_dependency", "model is not currently selected", 409)
        return row, version - 1

    def _resolve(self, kind, payload, source_kind):
        if kind == "condition_prediction":
            if set(payload) != {"model_record_id", "schema", "rows", "confidence_min", "ood_margin"}:
                raise StudyError("invalid_payload", "invalid prediction payload fields")
            record_id = uuid(payload["model_record_id"], "model_record_id")
            with self.database.connection() as conn:
                row, version = self._dependency(conn, record_id, "condition_training", True, include_result=True)
            result = json.loads(row["result_json"])
            if type(result.get("artifact")) is not dict:
                raise StudyError("invalid_dependency", "approved model has no artifact", 409)
            if row["source_kind"] != source_kind:
                raise StudyError("source_mismatch", "dependency source kind differs", 422)
            compute_payload = {**payload, "artifact": result["artifact"]}
            deps = [{"record_id": record_id, "kind": row["kind"], "result_sha256": row["result_sha256"], "review_version": version}]
        elif kind == "energy_schedule":
            if type(payload.get("wells")) is not list or len(payload["wells"]) > 32:
                raise StudyError("invalid_payload", "wells must be a bounded list")
            deps, wells = [], []
            with self.database.connection() as conn:
                for well in payload["wells"]:
                    if type(well) is not dict or "recovery" in well or "recovery_record_id" not in well:
                        raise StudyError("invalid_payload", "well requires recovery_record_id")
                    record_id = uuid(well["recovery_record_id"], "recovery_record_id")
                    row, version = self._dependency(conn, record_id, "recovery_fit", include_result=True)
                    result = json.loads(row["result_json"])
                    if result.get("status") != "fitted":
                        raise StudyError("invalid_dependency", "recovery is not fitted", 409)
                    if row["source_kind"] != source_kind:
                        raise StudyError("source_mismatch", "dependency source kind differs", 422)
                    wells.append({**well, "recovery": result})
                    deps.append({"record_id": record_id, "kind": row["kind"], "result_sha256": row["result_sha256"], "review_version": version})
            compute_payload = {**payload, "wells": wells}
        else:
            if "artifact" in payload or "recovery" in payload:
                raise StudyError("invalid_payload", "caller-supplied dependency artifact is forbidden")
            compute_payload, deps = payload, []
        canonical(compute_payload)
        return compute_payload, deps

    def _recheck_dependencies(self, conn, deps, source_kind):
        for dep in deps:
            row, version = self._dependency(conn, dep["record_id"], dep["kind"], dep["kind"] == "condition_training")
            if version != dep["review_version"] or row["result_sha256"] != dep["result_sha256"] or row["source_kind"] != source_kind:
                raise StudyError("stale_dependency", "dependency changed during operation", 409)

    def create(self, token, body):
        actor = _authority(self.auth, token, CREATE)
        validate_envelope(body)
        kind, request_id, body_hash = body["kind"], body["request_id"], digest(body)
        prior = self._receipt(actor.id, request_id, "create", body_hash)
        if prior is not None:
            return prior
        engine = self.engines.get(kind)
        if (type(engine) is not tuple or len(engine) != 2 or type(engine[0]) is not str
                or not engine[0] or not callable(engine[1])):
            raise StudyError("engine_unavailable", "study engine unavailable", 503)
        compute_payload, deps = self._resolve(kind, body["payload"], body["source"]["kind"])
        compute_payload = json.loads(canonical(compute_payload))
        if not _COMPUTE_SLOT.acquire(blocking=False):
            raise StudyError("busy", "study computation is busy; retry the same request_id", 503)
        try:
            try:
                result = engine[1](compute_payload)
            except StudyError:
                raise
            except Exception:
                raise StudyError("engine_failed", "study calculation failed", 422) from None
            if type(result) is not dict or type(result.get("status")) is not str or result["status"] not in _RESULT_STATUSES[kind]:
                raise StudyError("invalid_result", "engine returned unsupported status")
            if any(key in result and result[key] is not expected for key, expected in _SAFE_RESULT_FLAGS.items()):
                raise StudyError("invalid_result", "engine returned unsafe study claim")
            result_json = canonical(result)
        finally:
            _COMPUTE_SLOT.release()
        record_id = str(uuid4())
        with self.database.transaction(actor.id, "studies.create", "study", record_id,
                                       source_type="manual", immediate=True, auto_audit=False) as conn:
            current = _authority(self.auth, token, CREATE)
            if current.id != actor.id:
                raise StudyError("unauthenticated", "identity changed", 401)
            prior = self._receipt(actor.id, request_id, "create", body_hash, conn)
            if prior is not None:
                return prior
            self._recheck_dependencies(conn, deps, body["source"]["kind"])
            response = {"id": record_id, "kind": kind, "schema_version": 1,
                        "algorithm_version": engine[0], "status": result["status"],
                        "input_sha256": digest({"source": body["source"], "payload": body["payload"],
                                                "dependencies": deps}), "result_sha256": digest(result),
                        "version": 1, "review_status": "unreviewed", "dependencies": deps}
            conn.execute("""INSERT INTO study_records(id,request_id,actor_id,kind,title,schema_version,algorithm_version,
                source_kind,source_json,payload_json,result_json,result_status,dependencies_json,input_sha256,result_sha256,body_sha256)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", (record_id, request_id, actor.id, kind, body["title"], 1,
                engine[0], body["source"]["kind"], canonical(body["source"]), canonical(body["payload"]), result_json,
                result["status"], canonical(deps), response["input_sha256"], response["result_sha256"], body_hash))
            conn.execute("INSERT INTO study_events(record_id,actor_id,action,detail_json) VALUES(?,?,?,?)",
                         (record_id, actor.id, "created", canonical({"request_id": request_id})))
            self._save_receipt(conn, actor.id, request_id, "create", body_hash, response)
            self._audit(conn, actor.id, "studies.create", "study", record_id,
                        {"input_sha256": response["input_sha256"], "result_sha256": response["result_sha256"],
                         "kind": kind, "request_id": request_id})
            return response

    def _project(self, conn, row, include_body=True):
        version, state = self._review_state(conn, row["id"])
        selection_version, selected_id, selected_review = self._selection_state(conn, row["kind"])
        deps = json.loads(row["dependencies_json"])
        if include_body:
            reviews = conn.execute("""SELECT version,decision,comment,actor_id,created_at FROM study_reviews
                WHERE record_id=? ORDER BY version DESC LIMIT 100""", (row["id"],)).fetchall()
            selections = conn.execute("""SELECT version,record_id,review_version,comment,actor_id,created_at FROM study_selections
                WHERE kind=? ORDER BY version DESC LIMIT 100""", (row["kind"],)).fetchall()
        dependency_current = True
        try:
            self._recheck_dependencies(conn, deps, row["source_kind"])
        except StudyError:
            dependency_current = False
        selected = (selected_id == row["id"] and state == "approved"
                    and selected_review == version - 1 and dependency_current)
        result = {"id": row["id"], "kind": row["kind"], "title": row["title"], "actor_id": row["actor_id"],
                "created_at": row["created_at"], "schema_version": row["schema_version"],
                "algorithm_version": row["algorithm_version"], "source": json.loads(row["source_json"]),
                "status": row["result_status"],
                "dependencies": deps, "dependency_current": dependency_current,
                "input_sha256": row["input_sha256"], "result_sha256": row["result_sha256"],
                "version": version, "review_status": state, "is_selected": selected,
                "selection_version": selection_version,
                "review_scope": "software_study_only", "selection_scope": "shadow_only",
                "field_validation": False, "control_eligible": False,
                "eligible": state == "approved" and dependency_current}
        if include_body:
            result.update({"payload": json.loads(row["payload_json"]), "result": json.loads(row["result_json"]),
                           "review_history": [dict(item) for item in reversed(reviews)],
                           "selection_history": [dict(item) for item in reversed(selections)],
                           "review_history_truncated": version - 1 > len(reviews),
                           "selection_history_truncated": selection_version > len(selections)})
        return result

    def list(self, token, kind=None, limit=50, offset=0):
        _authority(self.auth, token, READ)
        if kind is not None and kind not in KINDS:
            raise StudyError("unsupported_kind", "unsupported study kind")
        if type(limit) is not int or not 1 <= limit <= 100 or type(offset) is not int or not 0 <= offset <= 1000000:
            raise StudyError("invalid_page", "invalid list pagination")
        with self.database.connection() as conn:
            rows = conn.execute("""SELECT id,kind,title,actor_id,created_at,schema_version,algorithm_version,
                source_kind,source_json,result_status,dependencies_json,input_sha256,result_sha256
                FROM study_records WHERE (? IS NULL OR kind=?) ORDER BY created_at DESC,id DESC LIMIT ? OFFSET ?""",
                                (kind, kind, limit, offset)).fetchall()
            return [self._project(conn, row, include_body=False) for row in rows]

    def get(self, token, id):
        _authority(self.auth, token, READ)
        with self.database.connection() as conn:
            return self._project(conn, self._row(conn, id))

    def request(self, token, request_id):
        actor = _authority(self.auth, token, CREATE)
        _authority(self.auth, token, READ)
        uuid(request_id)
        row = self.database.query_one(
            "SELECT action,response_json FROM study_receipts WHERE actor_id=? AND request_id=?",
            (actor.id, request_id))
        if row is None or row["action"] != "create":
            raise StudyError("not_found", "request receipt not found", 404)
        return json.loads(row["response_json"])

    def review(self, token, id, body):
        actor = _authority(self.auth, token, REVIEW)
        validate_review(body)
        body_hash, request_id = digest({"record_id": id, **body}), body["request_id"]
        with self.database.transaction(actor.id, "studies.review", "study", id,
                                       source_type="manual", immediate=True, auto_audit=False) as conn:
            _authority(self.auth, token, REVIEW)
            prior = self._receipt(actor.id, request_id, "review", body_hash, conn)
            if prior is not None:
                return prior
            row = self._row(conn, id)
            if row["actor_id"] == actor.id:
                raise StudyError("self_review", "author cannot review own study", 403)
            version, current = self._review_state(conn, id)
            if version != body["expected_version"]:
                raise StudyError("stale_version", "study review version changed", 409)
            if body["decision"] == "revoked" and current != "approved":
                raise StudyError("invalid_decision", "only an approval can be revoked", 409)
            if body["decision"] == "approved" and json.loads(row["result_json"])["status"] != _APPROVABLE[row["kind"]]:
                raise StudyError("invalid_decision", "result status cannot be approved", 409)
            self._recheck_dependencies(conn, json.loads(row["dependencies_json"]), row["source_kind"])
            conn.execute("INSERT INTO study_reviews(record_id,actor_id,version,decision,comment) VALUES(?,?,?,?,?)",
                         (id, actor.id, version, body["decision"], body["comment"]))
            conn.execute("INSERT INTO study_events(record_id,actor_id,action,detail_json) VALUES(?,?,?,?)",
                         (id, actor.id, "review", canonical({"decision": body["decision"], "version": version})))
            response = {"id": id, "version": version + 1, "review_status": body["decision"],
                        "review_scope": "software_study_only"}
            self._save_receipt(conn, actor.id, request_id, "review", body_hash, response)
            self._audit(conn, actor.id, "studies.review", "study", id,
                        {"decision": body["decision"], "version": version, "request_id": request_id})
            return response

    def select(self, token, kind, body):
        actor = _authority(self.auth, token, SELECT)
        if kind not in SELECTABLE:
            raise StudyError("unsupported_kind", "study kind cannot be selected")
        validate_selection(body)
        body_hash, request_id = digest({"kind": kind, **body}), body["request_id"]
        with self.database.transaction(actor.id, "studies.select", "study_selection", kind,
                                       source_type="manual", immediate=True, auto_audit=False) as conn:
            _authority(self.auth, token, SELECT)
            prior = self._receipt(actor.id, request_id, "select", body_hash, conn)
            if prior is not None:
                return prior
            version, _, _ = self._selection_state(conn, kind)
            if version != body["expected_version"]:
                raise StudyError("stale_version", "selection version changed", 409)
            review_version = None
            if body["record_id"] is not None:
                row, review_version = self._dependency(conn, body["record_id"], kind, include_dependencies=True)
                self._recheck_dependencies(conn, json.loads(row["dependencies_json"]), row["source_kind"])
            conn.execute("INSERT INTO study_selections(kind,version,record_id,review_version,actor_id,comment) VALUES(?,?,?,?,?,?)",
                         (kind, version + 1, body["record_id"], review_version, actor.id, body["comment"]))
            response = {"kind": kind, "version": version + 1, "record_id": body["record_id"],
                        "selection_scope": "shadow_only"}
            self._save_receipt(conn, actor.id, request_id, "select", body_hash, response)
            self._audit(conn, actor.id, "studies.select", "study_selection", kind,
                        {"record_id": body["record_id"], "version": version + 1, "request_id": request_id})
            return response

    def export(self, token, id):
        actor = _authority(self.auth, token, EXPORT)
        with self.database.transaction(actor.id, "studies.export", "study", id,
                                       source_type="manual", immediate=True) as conn:
            _authority(self.auth, token, EXPORT)
            result = self._project(conn, self._row(conn, id))
            return (canonical(result) + "\n").encode("utf-8")
