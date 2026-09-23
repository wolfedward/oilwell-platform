"""Service-level temporal transitions, provenance and all-or-nothing lifecycle changes."""
from datetime import datetime, timedelta
import importlib
from uuid import UUID, uuid4

from psycopg import sql
from psycopg.types.json import Jsonb

from backend.registry.contracts import Actor, validate_command
from backend.registry.errors import RegistryError
from backend.registry.point_maps import canonical_point_map
from backend.registry.transactions import RegistryTransactions
from tests.registry_support import RegistryDatabaseCase


class RegistryLifecycleTests(RegistryDatabaseCase):
    def setUp(self):
        super().setUp()
        try:
            self.lifecycle = importlib.import_module("backend.registry.lifecycle")
        except ModuleNotFoundError as error:
            if error.name != "backend.registry.lifecycle":
                raise
            self.fail("Temporal registry lifecycle has not been implemented")

    def new_well(self):
        return self.command("well.create", area_code="A", well_code="W" + uuid4().hex, name="新井")["entity"]

    def new_device(self):
        return self.command("device.create", gateway_id="GW", device_id="D" + uuid4().hex, name="终端",
                            transport="mqtt_direct", usage="commissioning")["entity"]

    def snapshot(self):
        return {table: self.scalar(sql.SQL("SELECT coalesce(jsonb_agg(to_jsonb(t) ORDER BY to_jsonb(t)::text),'[]'::jsonb) "
                                          "FROM device_registry.{} t").format(sql.Identifier(table)))
                for table in ("devices", "well_bindings", "point_map_assignments", "device_state_periods",
                              "audit_events", "operation_receipts", "registry_head")}

    def reject(self, expected_status, action, target, **body):
        before = self.snapshot()
        with self.assertRaises(RegistryError) as caught:
            self.command(action, target, **body)
        self.assertEqual(caught.exception.status, expected_status)
        self.assertEqual(self.snapshot(), before)
        return caught.exception

    def open_ids(self, device_id):
        return {table: self.scalar(sql.SQL("SELECT array_agg(id ORDER BY id) FROM device_registry.{} "
                                          "WHERE device_id=%s AND valid_to IS NULL").format(sql.Identifier(table)), (device_id,))
                for table in ("well_bindings", "point_map_assignments", "device_state_periods")}

    def test_move_suspends_clears_assignment_and_closes_every_period_at_one_instant(self):
        original = self.fixture(active=True)
        well = self.new_well()
        before = self.open_ids(original["device_id"])
        prior_audits = self.scalar("SELECT count(*) FROM device_registry.audit_events")
        result = self.command("device.bind", original["device_id"], expected_version=original["version"], well_id=well["id"])
        self.assertEqual(result["entity"]["status"], "suspended")
        self.assertEqual(result["entity"]["version"], str(int(original["version"]) + 1))
        instant = datetime.fromisoformat(result["committed_at"])
        for table, ids in before.items():
            self.assertEqual(self.scalar(sql.SQL("SELECT array_agg(valid_to) FROM device_registry.{} WHERE id=ANY(%s)")
                                         .format(sql.Identifier(table)), (ids,)), [instant])
        self.assertEqual(self.scalar("SELECT valid_from FROM device_registry.well_bindings WHERE device_id=%s AND valid_to IS NULL",
                                     (original["device_id"],)), instant)
        self.assertEqual(self.scalar("SELECT valid_from FROM device_registry.device_state_periods WHERE device_id=%s AND valid_to IS NULL",
                                     (original["device_id"],)), instant)
        self.assertIsNone(self.open_ids(original["device_id"])["point_map_assignments"])
        self.assertEqual(self.scalar("SELECT count(*) FROM device_registry.audit_events"), prior_audits + 1)
        audit = self.scalar("SELECT to_jsonb(t) FROM device_registry.audit_events t WHERE action='device.bind' ORDER BY event_at DESC LIMIT 1")
        self.assertIn(str(before["well_bindings"][0]), str(audit["before_data"]))
        self.assertIn(str(self.open_ids(original["device_id"])["well_bindings"][0]), str(audit["after_data"]))
        self.assertIn(str(before["point_map_assignments"][0]), str(audit["after_data"]))

    def test_unbind_suspends_and_preserves_closed_history(self):
        item = self.fixture(active=True)
        result = self.command("device.unbind", item["device_id"], expected_version=item["version"])
        self.assertEqual(result["entity"]["status"], "suspended")
        self.assertEqual(self.scalar("SELECT count(*) FROM device_registry.well_bindings"), 1)
        self.assertEqual(self.scalar("SELECT count(*) FROM device_registry.point_map_assignments"), 1)
        self.assertIsNone(self.open_ids(item["device_id"])["well_bindings"])
        self.assertIsNone(self.open_ids(item["device_id"])["point_map_assignments"])
        self.reject(409, "device.unbind", item["device_id"], expected_version=result["entity"]["version"])

    def test_explicit_suspend_preserves_current_binding_and_map(self):
        item = self.fixture(active=True)
        before = self.open_ids(item["device_id"])
        result = self.command("device.state", item["device_id"], expected_version=item["version"], status="suspended")
        after = self.open_ids(item["device_id"])
        self.assertEqual((after["well_bindings"], after["point_map_assignments"]),
                         (before["well_bindings"], before["point_map_assignments"]))
        self.assertTrue(result["entity"]["usage_locked"])
        self.reject(409, "device.change", item["device_id"], expected_version=result["entity"]["version"], usage="production")

    def test_all_sixteen_state_pairs_follow_explicit_transition_table(self):
        allowed = {("pending", "active"), ("pending", "retired"), ("active", "suspended"),
                   ("active", "retired"), ("suspended", "active"), ("suspended", "retired")}
        for source in ("pending", "active", "suspended", "retired"):
            for target in ("pending", "active", "suspended", "retired"):
                with self.subTest(source=source, target=target):
                    item = self.fixture(active=source in {"active", "suspended"})
                    current = item["version"]
                    if source in {"suspended", "retired"}:
                        current = self.command("device.state", item["device_id"], expected_version=current, status=source)["entity"]["version"]
                    body = dict(expected_version=current, status=target, check_note="核对接线与身份")
                    if (source, target) in allowed:
                        result = self.command("device.state", item["device_id"], **body)["entity"]
                        self.assertEqual((result["status"], result["version"]), (target, str(int(current) + 1)))
                    else:
                        self.reject(422 if target == "pending" else 409, "device.state", item["device_id"], **body)

    def test_activation_requires_binding_approved_assignment_and_human_evidence_not_a_message(self):
        device, well = self.new_device(), self.new_well()
        self.reject(409, "device.state", device["id"], expected_version="1", status="active", check_note="已核对设备")
        self.command("device.bind", device["id"], expected_version="1", well_id=well["id"])
        self.reject(409, "device.state", device["id"], expected_version="2", status="active", check_note="已核对设备")
        item = self.command("map.create", device["id"], expected_version="2", parent_version=None, points=[{"point_code": "load"}])["entity"]
        self.command("map.review", device["id"], expected_version="3", map_version="1", sha256=item["sha256"], decision="approved")
        self.command("map.assign", device["id"], expected_version="4", map_version="1")
        for note in (None, "", "  "):
            body = dict(expected_version="5", status="active")
            if note is not None:
                body["check_note"] = note
            self.reject(422, "device.state", device["id"], **body)
        result = self.command("device.state", device["id"], expected_version="5", status="active", check_note="已核对设备")["entity"]
        self.assertEqual((result["status"], result["check_note"], result["usage_locked"]), ("active", "已核对设备", True))
        self.assertEqual(self.scalar("SELECT count(*) FROM public.ingest_messages"), 0)

    def test_retirement_closes_current_relationships_and_all_other_mutations_are_read_only(self):
        item = self.fixture(active=True)
        result = self.command("device.state", item["device_id"], expected_version=item["version"], status="retired")
        version = result["entity"]["version"]
        for action, body in (("device.change", {"name": "不可改"}), ("device.bind", {"well_id": item["well_id"]}),
                             ("device.unbind", {}), ("map.create", {"parent_version": "1", "points": [{"point_code": "x"}]}),
                             ("map.review", {"map_version": "1", "sha256": item["sha256"], "decision": "approved"}),
                             ("map.assign", {"map_version": "1"}), ("device.state", {"status": "active", "check_note": "不能复活"})):
            self.reject(409, action, item["device_id"], expected_version=version, **body)
        self.assertIsNone(self.open_ids(item["device_id"])["well_bindings"])
        self.assertIsNone(self.open_ids(item["device_id"])["point_map_assignments"])
        self.assertEqual(self.scalar("SELECT status FROM device_registry.device_state_periods WHERE valid_to IS NULL"), "retired")
        self.assertEqual(self.scalar("SELECT count(*) FROM device_registry.device_state_periods"), 3)

    def test_same_binding_and_map_are_conflicts_but_same_key_retry_is_receipt_replay(self):
        item = self.fixture()
        self.reject(409, "device.bind", item["device_id"], expected_version=item["version"], well_id=item["well_id"])
        self.reject(409, "map.assign", item["device_id"], expected_version=item["version"], map_version=item["map_version"])
        key = uuid4()
        body = dict(expected_version=item["version"], status="retired")
        first = self.command("device.state", item["device_id"], key=key, **body)
        replay = self.command("device.state", item["device_id"], key=key, **body)
        self.assertEqual(replay, first | {"replayed": True})

    def test_multiple_devices_can_share_one_well(self):
        well = self.new_well()
        for _ in range(2):
            device = self.new_device()
            self.command("device.bind", device["id"], expected_version="1", well_id=well["id"])
        self.assertEqual(self.scalar("SELECT count(*) FROM device_registry.well_bindings WHERE well_id=%s AND valid_to IS NULL", (well["id"],)), 2)

    def test_unknown_or_archived_well_cannot_be_binding_target(self):
        well, device = self.new_well(), self.new_device()
        self.reject(404, "device.bind", device["id"], expected_version="1", well_id=str(uuid4()))
        self.command("well.change", well["id"], expected_version="1", status="archived")
        self.reject(409, "device.bind", device["id"], expected_version="1", well_id=well["id"])

    def test_assignment_requires_binding_and_approved_snapshot_from_this_device(self):
        device = self.new_device()
        draft = self.command("map.create", device["id"], expected_version="1", parent_version=None, points=[{"point_code": "load"}])["entity"]
        self.reject(409, "map.assign", device["id"], expected_version="2", map_version="1")
        well = self.new_well()
        self.command("device.bind", device["id"], expected_version="2", well_id=well["id"])
        self.reject(409, "map.assign", device["id"], expected_version="3", map_version="1")
        self.command("map.review", device["id"], expected_version="3", map_version="1", sha256=draft["sha256"], decision="rejected")
        self.reject(409, "map.assign", device["id"], expected_version="4", map_version="1")
        other = self.new_device()
        self.command("device.bind", other["id"], expected_version="1", well_id=well["id"])
        self.reject(404, "map.assign", other["id"], expected_version="2", map_version="1")

    def test_reassignment_of_active_device_requires_recheck_but_does_not_change_well(self):
        item = self.fixture(active=True)
        before = self.open_ids(item["device_id"])
        newmap = self.command("map.create", item["device_id"], expected_version=item["version"], parent_version="1",
                              points=[{"point_code": "load", "unit": "kN", "engineering_max": 90}])["entity"]
        reviewed = self.command("map.review", item["device_id"], expected_version=newmap["device_version"],
                                map_version="2", sha256=newmap["sha256"], decision="approved")["entity"]
        result = self.command("map.assign", item["device_id"], expected_version=reviewed["device_version"], map_version="2")
        self.assertEqual(result["entity"]["status"], "suspended")
        self.assertEqual(self.open_ids(item["device_id"])["well_bindings"], before["well_bindings"])
        instant = datetime.fromisoformat(result["committed_at"])
        self.assertEqual(self.scalar("SELECT valid_to FROM device_registry.point_map_assignments WHERE map_version=1"), instant)
        self.assertEqual(self.scalar("SELECT valid_from FROM device_registry.point_map_assignments WHERE map_version=2"), instant)

    def test_assignment_rejects_inconsistent_transport_even_if_stored_snapshot_is_approved(self):
        device, well = self.new_device(), self.new_well()
        self.command("device.bind", device["id"], expected_version="1", well_id=well["id"])
        document, raw, digest = canonical_point_map("modbus_tcp", [{"point_code": "load", "unit_id": 1,
                              "function_code": 3, "address": 0, "data_type": "float32"}])
        # Explicit corrupt/imported-row fixture; the registration API never permits this.
        with self.connect("migrator") as conn:
            event = conn.execute("SELECT id FROM device_registry.audit_events WHERE object_id=%s LIMIT 1", (device["id"],)).fetchone()[0]
            conn.execute("INSERT INTO device_registry.point_map_versions (device_id,version,transport,canonical_rule,canonical_bytes,"
                         "content,sha256,status,created_event_id,reviewed_at,review_event_id) "
                         "VALUES (%s,1,'modbus_tcp',%s,%s,%s,%s,'approved',%s,clock_timestamp(),%s)",
                         (device["id"], document["rule"], raw, Jsonb(document), digest, event, event))
        self.reject(409, "map.assign", device["id"], expected_version="2", map_version="1")

    def test_clock_at_or_before_interval_start_rolls_back_the_whole_change(self):
        from backend.registry.service import lock_references
        item, well = self.fixture(active=True), self.new_well()
        start = self.scalar("SELECT valid_from FROM device_registry.well_bindings WHERE device_id=%s AND valid_to IS NULL", (item["device_id"],))
        before = self.snapshot()
        for forced in (start, start - timedelta(microseconds=1)):
            command = validate_command("device.bind", UUID(item["device_id"]), Actor("platform-local", "101", "test", "测试", "administrator"),
                                       {"expected_version": item["version"], "well_id": well["id"], "reason": "时钟验证", "idempotency_key": str(uuid4())},
                                       method="POST", route=f"/api/registry/devices/{item['device_id']}/bindings")
            with self.assertRaises(RegistryError) as caught:
                RegistryTransactions(lambda: self.connect()).run(command,
                    lambda cur, cmd, instant, event: self.lifecycle.apply_lifecycle(cur, cmd, forced, event), lock=lock_references)
            self.assertEqual(caught.exception.code, "registry.clock_conflict")
            self.assertEqual(self.snapshot(), before)

    def test_audit_insert_failure_rolls_back_closed_and_opened_relationships(self):
        item, well = self.fixture(active=True), self.new_well()
        before = self.snapshot()
        with self.connect("migrator") as conn:
            conn.execute("CREATE FUNCTION device_registry.lifecycle_test_reject() RETURNS trigger LANGUAGE plpgsql "
                         "AS $$ BEGIN RAISE EXCEPTION 'test audit insert failure'; END $$")
            conn.execute("CREATE TRIGGER lifecycle_test_reject BEFORE INSERT ON device_registry.audit_events "
                         "FOR EACH ROW EXECUTE FUNCTION device_registry.lifecycle_test_reject()")
        try:
            self.reject(503, "device.bind", item["device_id"], expected_version=item["version"], well_id=well["id"])
        finally:
            with self.connect("migrator") as conn:
                conn.execute("DROP TRIGGER lifecycle_test_reject ON device_registry.audit_events")
                conn.execute("DROP FUNCTION device_registry.lifecycle_test_reject()")
        self.assertEqual(self.snapshot(), before)

    def test_period_closure_accepts_only_three_fixed_history_tables(self):
        with self.assertRaises(ValueError):
            self.lifecycle.close_periods(None, "devices; DELETE FROM devices", uuid4(), datetime.now())
