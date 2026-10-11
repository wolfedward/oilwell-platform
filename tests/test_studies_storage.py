"""Engineering study persistence against a private, temporary identity database."""
import hashlib
import json
import sqlite3
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

from backend.auth import AuthService
from backend.database import Database
from backend.studies.contracts import StudyError, validate_envelope
from backend.studies.service import StudyService
from backend.studies.storage import migrate_studies


def envelope(kind="motor_spectrum", payload=None, source_kind="software_fixture"):
    return {"request_id": str(uuid4()), "kind": kind, "title": "Study A",
            "source": {"kind": source_kind, "description": "fixture", "references": []},
            "payload": {"sample": 1} if payload is None else payload}


class StudiesStoreTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.db = Database(Path(temporary.name) / "identity.sqlite")
        self.db.migrate()
        migrate_studies(self.db)
        self.auth = AuthService(self.db)
        self.tokens = {}
        expires = (datetime.now(timezone(timedelta(hours=8))) + timedelta(hours=2)).isoformat()
        with self.db.connection() as conn:
            for code in ("studies.read", "studies.create", "studies.review", "studies.select", "studies.export"):
                conn.execute("INSERT INTO permissions(code,name) VALUES(?,?)", (code, code))
            conn.execute("INSERT INTO roles(code,name) VALUES('study_role','Study')")
            role = conn.execute("SELECT id FROM roles WHERE code='study_role'").fetchone()[0]
            conn.execute("INSERT INTO role_permissions(role_id,permission_id) SELECT ?,id FROM permissions WHERE code LIKE 'studies.%'", (role,))
            for name in ("author", "reviewer"):
                conn.execute("INSERT INTO users(username,display_name,role_id,status) VALUES(?,?,?,'active')", (name, name, role))
                user = conn.execute("SELECT id FROM users WHERE username=?", (name,)).fetchone()[0]
                token = uuid4().hex
                conn.execute("INSERT INTO sessions(user_id,token_hash,expires_at) VALUES(?,?,?)", (user, hashlib.sha256(token.encode()).hexdigest(), expires))
                self.tokens[name] = token
        self.engines = {kind: ("v1", lambda payload: {"status": "completed", "echo": payload}) for kind in (
            "condition_training", "condition_prediction", "recovery_fit", "energy_schedule",
            "energy_effects", "motor_spectrum", "protection_replay")}
        self.service = StudyService(self.db, self.auth, self.engines)

    def test_envelope_refuses_unknown_and_nonfinite_and_forged_source(self):
        for change in ({"extra": True}, {"kind": []}, {"payload": {"value": float("nan")}},
                       {"source": {"kind": "submitted_observation", "description": "", "references": []}}):
            body = envelope()
            body.update(change)
            with self.subTest(change=change), self.assertRaises(StudyError):
                validate_envelope(body)

    def test_create_restart_read_and_body_bound_replay(self):
        body = envelope()
        first = self.service.create(self.tokens["author"], body)
        again = StudyService(self.db, self.auth, self.engines).create(self.tokens["author"], body)
        self.assertEqual(first["id"], again["id"])
        self.assertEqual(self.db.scalar("SELECT COUNT(*) FROM study_records"), 1)
        read = self.service.get(self.tokens["author"], first["id"])
        self.assertEqual(read["source"], body["source"])
        self.assertEqual(read["payload"], body["payload"])
        self.assertEqual(read["result"]["echo"], body["payload"])
        audit = self.db.query_one("SELECT details_json FROM audit_logs WHERE action='studies.create' AND object_id=?", (first["id"],))
        self.assertEqual(json.loads(audit["details_json"])["after"]["input_sha256"], first["input_sha256"])
        altered = {**body, "payload": {"sample": 2}}
        with self.assertRaisesRegex(StudyError, "request"):
            self.service.create(self.tokens["author"], altered)

    def test_input_digest_binds_source_claim_and_payload(self):
        first = envelope()
        second = envelope(payload=first["payload"])
        second["source"]["description"] = "different source"
        left = self.service.create(self.tokens["author"], first)
        right = self.service.create(self.tokens["author"], second)
        self.assertNotEqual(left["input_sha256"], right["input_sha256"])

    def test_engine_mutation_cannot_rewrite_committed_input(self):
        def mutate(payload):
            payload["sample"] = 999
            return {"status": "completed"}
        service = StudyService(self.db, self.auth, {"motor_spectrum": ("v1", mutate)})
        body = envelope()
        record = service.create(self.tokens["author"], body)
        self.assertEqual(service.get(self.tokens["author"], record["id"])["payload"], {"sample": 1})
        self.assertEqual(body["payload"], {"sample": 1})

    def test_review_is_independent_versioned_and_append_only(self):
        record = self.service.create(self.tokens["author"], envelope())
        review = {"request_id": str(uuid4()), "expected_version": 1, "decision": "approved", "comment": "checked"}
        with self.assertRaises(StudyError):
            self.service.review(self.tokens["author"], record["id"], review)
        approved = self.service.review(self.tokens["reviewer"], record["id"], review)
        self.assertEqual(approved["review_status"], "approved")
        self.assertEqual(approved["version"], 2)
        self.assertEqual(self.service.review(self.tokens["reviewer"], record["id"], review)["version"], 2)
        with self.assertRaises(StudyError):
            self.service.review(self.tokens["reviewer"], record["id"], {**review, "request_id": str(uuid4())})
        self.assertEqual(self.db.scalar("SELECT COUNT(*) FROM study_reviews"), 1)

    def test_revocation_invalidates_selection_and_export_is_audited(self):
        record = self.service.create(self.tokens["author"], envelope("protection_replay"))
        self.service.review(self.tokens["reviewer"], record["id"], {"request_id": str(uuid4()), "expected_version": 1, "decision": "approved", "comment": "ok"})
        selected = self.service.select(self.tokens["reviewer"], "protection_replay", {"request_id": str(uuid4()), "expected_version": 0, "record_id": record["id"], "comment": "shadow"})
        self.assertEqual(selected["record_id"], record["id"])
        exported = self.service.export(self.tokens["author"], record["id"])
        self.assertEqual(json.loads(exported)["id"], record["id"])
        self.assertEqual(self.db.scalar("SELECT COUNT(*) FROM audit_logs WHERE action='studies.export'"), 1)
        self.service.review(self.tokens["reviewer"], record["id"], {"request_id": str(uuid4()), "expected_version": 2, "decision": "revoked", "comment": "withdrawn"})
        current = self.service.get(self.tokens["author"], record["id"])
        self.assertFalse(current["is_selected"])
        self.assertEqual(current["review_status"], "revoked")
        self.assertEqual([item["decision"] for item in current["review_history"]], ["approved", "revoked"])
        self.assertEqual(current["selection_history"][0]["record_id"], record["id"])
        self.service.review(self.tokens["reviewer"], record["id"], {"request_id": str(uuid4()), "expected_version": 3, "decision": "approved", "comment": "reapproved"})
        self.assertFalse(self.service.get(self.tokens["author"], record["id"])["is_selected"])
        self.service.select(self.tokens["reviewer"], "protection_replay", {"request_id": str(uuid4()), "expected_version": 1, "record_id": record["id"], "comment": "reselect"})
        self.assertTrue(self.service.get(self.tokens["author"], record["id"])["is_selected"])

    def test_saved_record_rejects_sql_update_and_exposes_selection_version(self):
        record = self.service.create(self.tokens["author"], envelope("protection_replay"))
        with self.assertRaises(sqlite3.IntegrityError):
            with self.db.connection() as conn:
                conn.execute("UPDATE study_records SET title='changed' WHERE id=?", (record["id"],))
        self.assertEqual(self.service.get(self.tokens["author"], record["id"])["selection_version"], 0)

    def test_request_lookup_is_actor_scoped_and_body_bound(self):
        body = envelope()
        record = self.service.create(self.tokens["author"], body)
        self.assertEqual(self.service.request(self.tokens["author"], body["request_id"])["id"], record["id"])
        with self.assertRaises(StudyError) as missing:
            self.service.request(self.tokens["reviewer"], body["request_id"])
        self.assertEqual(missing.exception.status, 404)

    def test_authority_revoked_during_compute_prevents_commit(self):
        body = envelope()
        def revoke(payload):
            with self.db.connection() as conn:
                conn.execute("UPDATE sessions SET revoked_at='2020-01-01' WHERE token_hash=?", (hashlib.sha256(self.tokens["author"].encode()).hexdigest(),))
            return {"status": "completed"}
        service = StudyService(self.db, self.auth, {"motor_spectrum": ("v1", revoke)})
        with self.assertRaises(StudyError):
            service.create(self.tokens["author"], body)
        self.assertEqual(self.db.scalar("SELECT COUNT(*) FROM study_records"), 0)

    def test_audit_failure_rolls_back_record_and_receipt(self):
        with self.db.connection() as conn:
            conn.execute("""CREATE TRIGGER block_study_audit BEFORE INSERT ON audit_logs
                WHEN NEW.action='studies.create' BEGIN SELECT RAISE(FAIL,'audit unavailable'); END""")
        body = envelope()
        with self.assertRaises(Exception):
            self.service.create(self.tokens["author"], body)
        self.assertEqual(self.db.scalar("SELECT COUNT(*) FROM study_records"), 0)
        self.assertEqual(self.db.scalar("SELECT COUNT(*) FROM study_receipts"), 0)

    def test_committed_replay_does_not_need_new_audit_write(self):
        body = envelope()
        first = self.service.create(self.tokens["author"], body)
        with self.db.connection() as conn:
            conn.execute("""CREATE TRIGGER block_study_audit BEFORE INSERT ON audit_logs
                WHEN NEW.action='studies.create' BEGIN SELECT RAISE(FAIL,'audit unavailable'); END""")
        self.assertEqual(self.service.create(self.tokens["author"], body)["id"], first["id"])
        review = {"request_id": str(uuid4()), "expected_version": 1, "decision": "approved", "comment": "ok"}
        approved = self.service.review(self.tokens["reviewer"], first["id"], review)
        with self.db.connection() as conn:
            conn.execute("""CREATE TRIGGER block_review_audit BEFORE INSERT ON audit_logs
                WHEN NEW.action='studies.review' BEGIN SELECT RAISE(FAIL,'audit unavailable'); END""")
        self.assertEqual(self.service.review(self.tokens["reviewer"], first["id"], review), approved)

    def test_concurrent_same_key_retries_to_one_immutable_record(self):
        entered, release = threading.Event(), threading.Event()
        def slow(payload):
            entered.set()
            self.assertTrue(release.wait(3))
            return {"status": "completed"}
        service = StudyService(self.db, self.auth, {"motor_spectrum": ("v1", slow)})
        body, done = envelope(), []
        worker = threading.Thread(target=lambda: done.append(service.create(self.tokens["author"], body)))
        worker.start()
        self.assertTrue(entered.wait(3))
        try:
            with self.assertRaises(StudyError) as busy:
                service.create(self.tokens["author"], body)
            self.assertEqual(busy.exception.status, 503)
        finally:
            release.set()
            worker.join(3)
        self.assertFalse(worker.is_alive())
        self.assertEqual(service.create(self.tokens["author"], body)["id"], done[0]["id"])
        self.assertEqual(self.db.scalar("SELECT COUNT(*) FROM study_records"), 1)

    def test_prediction_dependency_requires_selected_approved_model(self):
        train = envelope("condition_training")
        engines = dict(self.engines)
        engines["condition_training"] = ("v1", lambda payload: {"status": "completed", "artifact": {"weights": [1]}})
        service = StudyService(self.db, self.auth, engines)
        model = service.create(self.tokens["author"], train)
        prediction = envelope("condition_prediction", {"model_record_id": model["id"], "schema": "v1", "rows": [], "confidence_min": 0.8, "ood_margin": 1})
        with self.assertRaises(StudyError):
            service.create(self.tokens["author"], prediction)
        service.review(self.tokens["reviewer"], model["id"], {"request_id": str(uuid4()), "expected_version": 1, "decision": "approved", "comment": "ok"})
        service.select(self.tokens["reviewer"], "condition_training", {"request_id": str(uuid4()), "expected_version": 0, "record_id": model["id"], "comment": "shadow"})
        result = service.create(self.tokens["author"], prediction)
        self.assertEqual(result["dependencies"][0]["record_id"], model["id"])
        self.assertEqual(service.get(self.tokens["author"], result["id"])["result"]["echo"]["artifact"], {"weights": [1]})
        with self.assertRaises(StudyError):
            service.create(self.tokens["author"], envelope("condition_prediction", {**prediction["payload"], "artifact": {}}))

    def test_dependency_revocation_during_prediction_prevents_commit(self):
        engines = dict(self.engines)
        engines["condition_training"] = ("v1", lambda payload: {"status": "completed", "artifact": {"weights": [1]}})
        service = StudyService(self.db, self.auth, engines)
        model = service.create(self.tokens["author"], envelope("condition_training"))
        service.review(self.tokens["reviewer"], model["id"], {"request_id": str(uuid4()), "expected_version": 1, "decision": "approved", "comment": "ok"})
        service.select(self.tokens["reviewer"], "condition_training", {"request_id": str(uuid4()), "expected_version": 0, "record_id": model["id"], "comment": "shadow"})
        def revoke(payload):
            service.review(self.tokens["reviewer"], model["id"], {"request_id": str(uuid4()), "expected_version": 2, "decision": "revoked", "comment": "withdrawn"})
            return {"status": "completed"}
        service.engines["condition_prediction"] = ("v1", revoke)
        prediction = envelope("condition_prediction", {"model_record_id": model["id"], "schema": "v1", "rows": [], "confidence_min": 0.8, "ood_margin": 1})
        with self.assertRaises(StudyError):
            service.create(self.tokens["author"], prediction)
        self.assertEqual(self.db.scalar("SELECT COUNT(*) FROM study_records WHERE kind='condition_prediction'"), 0)

    def test_feasible_schedule_can_be_approved_and_selected(self):
        engines = dict(self.engines)
        engines["recovery_fit"] = ("v1", lambda payload: {"status": "fitted", "well_id": "W-1"})
        engines["energy_schedule"] = ("v1", lambda payload: {"status": "feasible"})
        service = StudyService(self.db, self.auth, engines)
        recovery = service.create(self.tokens["author"], envelope("recovery_fit"))
        service.review(self.tokens["reviewer"], recovery["id"], {"request_id": str(uuid4()), "expected_version": 1, "decision": "approved", "comment": "ok"})
        schedule = service.create(self.tokens["author"], envelope("energy_schedule", {"wells": [{"well_id": "W-1", "recovery_record_id": recovery["id"]}]}))
        self.assertEqual(schedule["status"], "feasible")
        service.review(self.tokens["reviewer"], schedule["id"], {"request_id": str(uuid4()), "expected_version": 1, "decision": "approved", "comment": "ok"})
        selected = service.select(self.tokens["reviewer"], "energy_schedule", {"request_id": str(uuid4()), "expected_version": 0, "record_id": schedule["id"], "comment": "shadow"})
        self.assertEqual(selected["record_id"], schedule["id"])


if __name__ == "__main__":
    unittest.main()
