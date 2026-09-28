"""Real commit/rollback, idempotency and lock-order guarantees, not mocked SQL."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import importlib
import threading
from uuid import uuid4

import psycopg

from backend.registry.contracts import Actor, Change, validate_command
from backend.registry.errors import RegistryError
from tests.registry_support import RegistryDatabaseCase


class RegistryTransactionTests(RegistryDatabaseCase):
    def setUp(self):
        super().setUp()
        try:
            self.transactions = importlib.import_module("backend.registry.transactions")
        except ModuleNotFoundError as error:
            if error.name != "backend.registry.transactions":
                raise
            self.fail("Atomic registry transactions have not been implemented")
        self.actor = Actor("platform-local", "101", "operator", "测试操作者", "administrator")
        self.runner = self.transactions.RegistryTransactions(lambda: self.connect("writer"))
        self.calls = []

    def command(self, *, key=None, actor=None, method="POST", route="/api/registry/wells", **changes):
        body = {"area_code": "A", "well_code": "W1", "name": "一号井", "reason": "隔离库验证",
                "idempotency_key": str(key or uuid4())} | changes
        return validate_command("well.create", None, actor or self.actor, body, method=method, route=route)

    def apply(self, cur, command, instant, event_id):
        entity_id = uuid4()
        cur.execute("INSERT INTO device_registry.wells(id,area_code,well_code,name,created_at,updated_at) "
                    "VALUES (%s,%s,%s,%s,%s,%s)", (entity_id, command.body["area_code"], command.body["well_code"],
                                                  command.body["name"], instant, instant))
        self.calls.append(entity_id)
        entity = {"id": str(entity_id), "version": "1", "name": command.body["name"]}
        return Change("well", entity_id, {}, entity, entity)

    def assert_counts(self, business, *, revision):
        for table in ("wells", "audit_events", "operation_receipts"):
            self.assertEqual(self.scalar("SELECT count(*) FROM device_registry." + table), business, table)
        self.assertEqual(self.scalar("SELECT revision FROM device_registry.registry_head"), revision)

    def test_receipt_replay_calls_business_once_and_preserves_original_commit(self):
        command = self.command()
        first = self.runner.run(command, self.apply)
        replay = self.runner.run(command, self.apply)
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(first | {"replayed": True}, replay)
        self.assertEqual(first["registry_revision"], "2")
        self.assertEqual(first["entity"]["version"], "1")
        self.assert_counts(1, revision=2)

    def test_same_key_changed_body_method_or_route_is_conflict_without_mutation(self):
        key = uuid4()
        first = self.runner.run(self.command(key=key), self.apply)
        for change in ({"reason": "变更原因"}, {"method": "PUT"}, {"route": "/api/registry/other"}):
            with self.subTest(change=change), self.assertRaises(RegistryError) as caught:
                self.runner.run(self.command(key=key, **change), self.apply)
            self.assertEqual(caught.exception.code, "registry.idempotency_conflict")
            self.assertEqual(caught.exception.status, 409)
        self.assertEqual(self.runner.run(self.command(key=key), self.apply)["entity"], first["entity"])
        self.assert_counts(1, revision=2)

    def test_same_key_is_scoped_to_actor_and_identity_domain(self):
        key = uuid4()
        actors = (self.actor, replace(self.actor, user_id="102"), replace(self.actor, domain="isolated-other-domain"))
        for index, actor in enumerate(actors):
            self.runner.run(self.command(key=key, actor=actor, well_code=f"W{index}"), self.apply)
        self.assert_counts(3, revision=4)

    def test_audit_failure_rolls_back_business_receipt_and_revision(self):
        with self.connect("migrator") as conn:
            conn.execute("CREATE TRIGGER registry_test_reject_audit BEFORE INSERT ON device_registry.audit_events "
                         "FOR EACH ROW EXECUTE FUNCTION device_registry.immutable_row()")
        try:
            with self.assertRaises(RegistryError) as caught:
                self.runner.run(self.command(), self.apply)
            self.assertEqual(caught.exception.status, 503)
            self.assert_counts(0, revision=1)
        finally:
            with self.connect("migrator") as conn:
                conn.execute("DROP TRIGGER registry_test_reject_audit ON device_registry.audit_events")
        self.runner.run(self.command(), self.apply)
        self.assert_counts(1, revision=2)

    def test_actual_commit_then_lost_connection_is_recovered_by_original_key(self):
        real = self.connect("writer")

        class LostCommitAcknowledgement:
            def __getattr__(self, name):
                return getattr(real, name)

            def __enter__(self):
                real.__enter__()
                return self

            def __exit__(self, kind, value, traceback):
                result = real.__exit__(kind, value, traceback)
                if kind is None:
                    raise psycopg.OperationalError("injected lost reply after actual commit")
                return result

        uncertain = self.transactions.RegistryTransactions(lambda: LostCommitAcknowledgement())
        command = self.command()
        with self.assertRaises(RegistryError) as caught:
            uncertain.run(command, self.apply)
        self.assertEqual(caught.exception.status, 503)
        # This proves the injection is AFTER a committed transaction, not before.
        self.assert_counts(1, revision=2)
        original = self.scalar("SELECT result FROM device_registry.operation_receipts")
        recovered = self.runner.run(command, self.apply)
        self.assertEqual(recovered, original | {"replayed": True})
        self.assertEqual(len(self.calls), 1)
        self.assert_counts(1, revision=2)

    def test_two_sessions_same_key_have_only_one_committed_change(self):
        command = self.command()
        barrier = threading.Barrier(2)

        def execute():
            barrier.wait(timeout=5)
            return self.runner.run(command, self.apply)

        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(execute), pool.submit(execute)]
            results = [future.result(timeout=10) for future in futures]
        self.assertEqual(sorted(result["replayed"] for result in results), [False, True])
        self.assertEqual(results[0]["entity"], results[1]["entity"])
        self.assertEqual(len(self.calls), 1)
        self.assert_counts(1, revision=2)

    def test_lock_hook_happens_before_clock_and_all_timestamps_match(self):
        observed = []

        def lock(cur, command):
            cur.execute("SELECT clock_timestamp() AS acquired")
            observed.append(cur.fetchone()["acquired"])

        def apply(cur, command, instant, event_id):
            self.assertGreaterEqual(instant, observed[0])
            self.assertEqual(instant.utcoffset(), timedelta(0))
            return self.apply(cur, command, instant, event_id)

        result = self.runner.run(self.command(), apply, lock=lock)
        stamp = datetime.fromisoformat(result["committed_at"])
        for table, column in (("wells", "created_at"), ("wells", "updated_at"),
                               ("audit_events", "event_at"), ("operation_receipts", "committed_at")):
            self.assertEqual(self.scalar("SELECT " + column + " FROM device_registry." + table), stamp)

    def test_replay_does_not_rerun_reference_lock_or_take_new_business_time(self):
        locks = []
        command = self.command()
        self.runner.run(command, self.apply, lock=lambda cur, cmd: locks.append(True))
        self.runner.run(command, self.apply, lock=lambda cur, cmd: self.fail("Replay cannot relock retired references"))
        self.assertEqual(locks, [True])

    def test_audit_records_authenticated_actor_reason_before_after_and_request(self):
        command = self.command()
        result = self.runner.run(command, self.apply)
        with self.connect("reader") as conn:
            row = conn.execute("SELECT actor,action,object_type,object_id,reason,before_data,after_data,request_id "
                               "FROM device_registry.audit_events").fetchone()
        self.assertEqual(row[0], {"domain": "platform-local", "user_id": "101", "username": "operator",
                                  "display_name": "测试操作者", "role_code": "administrator"})
        self.assertEqual(row[1:3], ("well.create", "well"))
        self.assertEqual(str(row[3]), result["entity"]["id"])
        self.assertEqual(row[4:7], ("隔离库验证", {}, result["entity"]))
        self.assertEqual(row[7], command.key)

    def test_revision_above_javascript_precision_remains_decimal_string(self):
        with self.connect("migrator") as conn:
            conn.execute("UPDATE device_registry.registry_head SET revision=9007199254740992")
        result = self.runner.run(self.command(), self.apply)
        self.assertEqual(result["registry_revision"], "9007199254740993")

    def test_revision_overflow_rolls_back_instead_of_wrapping(self):
        with self.connect("migrator") as conn:
            conn.execute("UPDATE device_registry.registry_head SET revision=9223372036854775807")
        with self.assertRaises(RegistryError) as caught:
            self.runner.run(self.command(), self.apply)
        self.assertEqual(caught.exception.status, 409)
        self.assert_counts(0, revision=9223372036854775807)
        self.assertEqual(self.transactions.bump(9223372036854775806), 9223372036854775807)

    def test_equal_or_backward_clock_rolls_back_without_fabricating_time(self):
        for delta in (timedelta(0), timedelta(microseconds=1)):
            def apply(cur, command, instant, event_id):
                self.apply(cur, command, instant, event_id)
                self.transactions.ensure_later(instant, [instant + delta])
            with self.assertRaises(RegistryError) as caught:
                self.runner.run(self.command(), apply)
            self.assertEqual(caught.exception.code, "registry.clock_conflict")
            self.assert_counts(0, revision=1)
        now = datetime.now(timezone.utc)
        self.transactions.ensure_later(now, [now - timedelta(microseconds=1)])
        self.transactions.ensure_later(now, [])

    def test_unique_conflict_returns_409_without_second_audit_or_raw_sql(self):
        self.runner.run(self.command(), self.apply)
        with self.assertRaises(RegistryError) as caught:
            self.runner.run(self.command(), self.apply)
        self.assertEqual(caught.exception.status, 409)
        self.assertNotIn("INSERT", caught.exception.message)
        self.assert_counts(1, revision=2)

    def test_head_lock_timeout_is_safe_retryable_failure(self):
        holder = self.connect("migrator")
        holder.execute("SELECT revision FROM device_registry.registry_head WHERE singleton=1 FOR UPDATE")
        try:
            with self.assertRaises(RegistryError) as caught:
                self.runner.run(self.command(), self.apply)
            self.assertEqual(caught.exception.status, 503)
            self.assertIsInstance(caught.exception.__cause__, psycopg.errors.LockNotAvailable)
        finally:
            holder.rollback()
        self.assert_counts(0, revision=1)

    def test_statement_timeout_rolls_back_and_removes_driver_details(self):
        def apply(cur, command, instant, event_id):
            self.apply(cur, command, instant, event_id)
            cur.execute("SELECT pg_sleep(4)")
        with self.assertRaises(RegistryError) as caught:
            self.runner.run(self.command(), apply)
        self.assertEqual(caught.exception.status, 503)
        self.assertNotIn("pg_sleep", caught.exception.message)
        self.assert_counts(0, revision=1)

    def test_connection_failure_never_exposes_credentials(self):
        def disconnected():
            raise psycopg.OperationalError("host=private-host password=test-secret connection failed")
        runner = self.transactions.RegistryTransactions(disconnected)
        with self.assertRaises(RegistryError) as caught:
            runner.run(self.command(), self.apply)
        self.assertEqual(caught.exception.status, 503)
        self.assertNotIn("private-host", str(caught.exception))
        self.assertNotIn("test-secret", str(caught.exception))

    def test_missing_head_fails_closed_before_business_mutation(self):
        with self.connect("migrator") as conn:
            conn.execute("DELETE FROM device_registry.registry_head")
        try:
            with self.assertRaises(RegistryError) as caught:
                self.runner.run(self.command(), self.apply)
            self.assertEqual(caught.exception.status, 503)
            self.assertEqual(self.scalar("SELECT count(*) FROM device_registry.wells"), 0)
        finally:
            with self.connect("migrator") as conn:
                conn.execute("INSERT INTO device_registry.registry_head(singleton,revision) VALUES (1,1)")
