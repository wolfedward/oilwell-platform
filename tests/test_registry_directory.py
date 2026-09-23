"""Production directory identity, stale writes, provenance and real row-lock timing."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import importlib
import json
import threading
import time
from uuid import UUID, uuid4

from backend.registry.errors import RegistryError
from tests.registry_support import RegistryDatabaseCase


class RegistryDirectoryTests(RegistryDatabaseCase):
    def setUp(self):
        super().setUp()
        try:
            importlib.import_module("backend.registry.service")
        except ModuleNotFoundError as error:
            if error.name != "backend.registry.service":
                raise
            self.fail("Production directory service has not been implemented")

    def device(self, **changes):
        return self.command("device.create", **({"gateway_id": "GW", "device_id": "D", "name": "井场终端",
                            "transport": "mqtt_direct", "usage": "commissioning"} | changes))["entity"]

    def well(self, **changes):
        return self.command("well.create", **({"area_code": "A", "well_code": "W1", "name": "一号井"} | changes))["entity"]

    def assert_rejected(self, expected_status, action, target, **body):
        prior = self.scalar("SELECT revision FROM device_registry.registry_head")
        with self.assertRaises(RegistryError) as caught:
            self.command(action, target, **body)
        self.assertEqual(caught.exception.status, expected_status)
        self.assertEqual(self.scalar("SELECT revision FROM device_registry.registry_head"), prior)
        return caught.exception

    def test_service_starts_with_empty_production_catalog_not_virtual_inventory(self):
        self.assertEqual(self.scalar("SELECT count(*) FROM device_registry.devices"), 0)
        self.assertEqual(self.scalar("SELECT count(*) FROM device_registry.wells"), 0)
        self.device()
        self.assertEqual(self.scalar("SELECT count(*) FROM device_registry.devices"), 1)
        self.assertEqual(self.scalar("SELECT count(*) FROM device_registry.wells"), 0)

    def test_new_device_is_pending_with_audited_state_and_server_uuid(self):
        device = self.device(usage="production")
        self.assertEqual(UUID(device["id"]).version, 4)
        self.assertEqual((device["version"], device["status"], device["usage_locked"]), ("1", "pending", False))
        self.assertEqual(device["transport"], "mqtt_direct")
        self.assertEqual(device["manufacturer"], "")
        with self.connect("reader") as conn:
            state = conn.execute("SELECT status,valid_from,valid_to,audit_event_id FROM device_registry.device_state_periods "
                                 "WHERE device_id=%s", (device["id"],)).fetchone()
            audit = conn.execute("SELECT id,event_at,object_id FROM device_registry.audit_events").fetchone()
        self.assertEqual(state, ("pending", audit[1], None, audit[0]))
        self.assertEqual(str(audit[2]), device["id"])
        self.assertEqual(datetime.fromisoformat(device["created_at"]), state[1])
        self.assertEqual(self.scalar("SELECT count(*) FROM device_registry.well_bindings"), 0)
        self.assertEqual(self.scalar("SELECT count(*) FROM device_registry.point_map_versions"), 0)
        json.dumps(device)  # Public records must not leak native UUID/datetime objects.

    def test_identity_is_gateway_plus_device_not_serial_number(self):
        first = self.device(serial_number="same-serial")
        second = self.device(gateway_id="GW2", serial_number="same-serial")
        self.assertNotEqual(first["id"], second["id"])
        self.assert_rejected(409, "device.create", None, gateway_id="GW", device_id="D", name="终端",
                             transport="mqtt_direct", usage="commissioning")
        self.assertEqual(self.scalar("SELECT count(*) FROM device_registry.devices"), 2)

    def test_well_identity_is_scoped_by_area_and_case_sensitive(self):
        wells = [self.well(), self.well(area_code="B"), self.well(well_code="w1")]
        self.assertEqual(len({row["id"] for row in wells}), 3)
        self.assertTrue(all(row["status"] == "active" and row["version"] == "1" for row in wells))
        self.assert_rejected(409, "well.create", None, area_code="A", well_code="W1", name="重复井")

    def test_device_metadata_change_is_versioned_audited_and_preserves_identity(self):
        device = self.device()
        result = self.command("device.change", device["id"], expected_version="1", name="更新名称",
                              manufacturer="厂家", model="M1", serial_number="S1", check_note="核对铭牌")
        updated = result["entity"]
        self.assertEqual((updated["name"], updated["version"], updated["model"]), ("更新名称", "2", "M1"))
        for field in ("id", "gateway_id", "device_id", "transport", "created_at"):
            self.assertEqual(updated[field], device[field])
        row = self.scalar("SELECT after_data FROM device_registry.audit_events WHERE action='device.change'")
        self.assertEqual((row["name"], row["version"]), ("更新名称", "2"))
        self.assertEqual(self.scalar("SELECT count(*) FROM device_registry.device_state_periods"), 1)

    def test_stale_version_keeps_first_edit_and_does_not_add_audit(self):
        device = self.device()
        self.command("device.change", device["id"], expected_version="1", name="新名")
        self.assert_rejected(409, "device.change", device["id"], expected_version="1", name="迟到修改")
        self.assertEqual(self.scalar("SELECT name FROM device_registry.devices"), "新名")
        self.assertEqual(self.scalar("SELECT count(*) FROM device_registry.audit_events"), 2)

    def test_same_metadata_is_not_a_change(self):
        device = self.device()
        self.assert_rejected(409, "device.change", device["id"], expected_version="1", name=device["name"])
        well = self.well()
        self.assert_rejected(409, "well.change", well["id"], expected_version="1", name=well["name"])

    def test_unknown_uuid_returns_not_found_not_an_insert(self):
        for action in ("well.change", "device.change"):
            self.assert_rejected(404, action, str(uuid4()), expected_version="1", name="未知记录")
        self.assertEqual(self.scalar("SELECT count(*) FROM device_registry.audit_events"), 0)

    def test_identity_and_privileged_fields_cannot_be_changed(self):
        device = self.device()
        for field, value in (("gateway_id", "OTHER"), ("device_id", "OTHER"), ("transport", "modbus_tcp"),
                              ("created_at", "2020-01-01T00:00:00Z"), ("status", "active"), ("usage_locked", False)):
            self.assert_rejected(422, "device.change", device["id"], expected_version="1", **{field: value})
        well = self.well()
        for field in ("well_code", "area_code", "actor"):
            self.assert_rejected(422, "well.change", well["id"], expected_version="1", **{field: "OTHER"})

    def test_usage_can_change_only_while_pending_and_never_after_locked(self):
        device = self.device()
        result = self.command("device.change", device["id"], expected_version="1", usage="production")
        self.assertEqual((result["entity"]["usage"], result["entity"]["version"]), ("production", "2"))
        with self.connect("migrator") as conn:
            conn.execute("UPDATE device_registry.devices SET usage_locked=true WHERE id=%s", (device["id"],))
        self.assert_rejected(409, "device.change", device["id"], expected_version="2", usage="commissioning")
        for index, state in enumerate(("active", "suspended")):
            other = self.device(device_id=f"D{index}")
            with self.connect("migrator") as conn:
                conn.execute("UPDATE device_registry.devices SET status=%s WHERE id=%s", (state, other["id"]))
            self.assert_rejected(409, "device.change", other["id"], expected_version="1", usage="production")

    def test_retired_identity_cannot_be_edited_or_recycled(self):
        device = self.device()
        with self.connect("migrator") as conn:
            conn.execute("UPDATE device_registry.devices SET status='retired' WHERE id=%s", (device["id"],))
        self.assert_rejected(409, "device.change", device["id"], expected_version="1", name="重用")
        self.assert_rejected(409, "device.create", None, gateway_id="GW", device_id="D", name="替换",
                             transport="mqtt_direct", usage="commissioning")

    def test_well_archive_is_audited_and_cannot_be_repeated_or_reversed(self):
        well = self.well()
        result = self.command("well.change", well["id"], expected_version="1", status="archived")
        self.assertEqual((result["entity"]["status"], result["entity"]["version"]), ("archived", "2"))
        self.assert_rejected(409, "well.change", well["id"], expected_version="2", status="archived")
        self.assert_rejected(422, "well.change", well["id"], expected_version="2", status="active")

    def test_well_with_open_binding_cannot_be_archived(self):
        well, device = self.well(), self.device()
        with self.connect("migrator") as conn:
            event = conn.execute("SELECT id FROM device_registry.audit_events WHERE object_id=%s", (device["id"],)).fetchone()[0]
            conn.execute("INSERT INTO device_registry.well_bindings(device_id,well_id,valid_from,audit_event_id) VALUES (%s,%s,clock_timestamp(),%s)",
                         (device["id"], well["id"], event))
        self.assert_rejected(409, "well.change", well["id"], expected_version="1", status="archived")
        self.assertEqual(self.scalar("SELECT status FROM device_registry.wells"), "active")

    def test_chinese_length_and_literal_html_are_preserved_as_text(self):
        label = '<img src=x onerror="alert(1)">井'
        device = self.device(name=label)
        self.assertEqual(device["name"], label)
        well = self.well(name="井" * 120)
        self.assertEqual(len(well["name"]), 120)
        self.assert_rejected(422, "well.change", well["id"], expected_version="1", name="井" * 121)

    def test_retry_returns_original_entity_without_extra_state_or_audit(self):
        key = uuid4()
        body = dict(gateway_id="GW", device_id="D", name="终端", transport="mqtt_direct", usage="commissioning")
        first = self.command("device.create", key=key, **body)
        second = self.command("device.create", key=key, **body)
        self.assertEqual(second, first | {"replayed": True})
        self.assertEqual(self.scalar("SELECT count(*) FROM device_registry.device_state_periods"), 1)
        self.assertEqual(self.scalar("SELECT count(*) FROM device_registry.audit_events"), 1)

    def test_concurrent_duplicate_identity_has_exactly_one_success(self):
        barrier = threading.Barrier(2)
        def create():
            barrier.wait(timeout=5)
            try:
                self.device()
                return "ok"
            except RegistryError as error:
                return error.status
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(create), pool.submit(create)]
            self.assertCountEqual([future.result(timeout=10) for future in futures], ["ok", 409])
        self.assertEqual(self.scalar("SELECT count(*) FROM device_registry.devices"), 1)
        self.assertEqual(self.scalar("SELECT count(*) FROM device_registry.audit_events"), 1)

    def test_registry_changes_never_modify_original_message_or_sample_rows(self):
        now = datetime.now(timezone.utc)
        with self.connect("migrator") as conn:
            conn.execute("INSERT INTO public.ingest_messages(message_id,topic,gateway_id,device_id,boot_id,sequence_first,"
                         "sequence_last,created_at,payload_sha256,raw_payload) VALUES ('fixture','oilfield/v1/GW/D/telemetry',"
                         "'GW','D','boot',1,1,%s,%s,'{}')", (now, "0" * 64))
            conn.execute("INSERT INTO public.telemetry_samples(message_id,sample_index,gateway_id,device_id,point_code,"
                         "measured_at,sequence,value,quality,unit,source) VALUES ('fixture',0,'GW','D','load',%s,1,32.4,'good','kN','fixture')", (now,))
        def originals():
            return [self.scalar("SELECT jsonb_agg(to_jsonb(t)) FROM public." + table + " t")
                    for table in ("ingest_messages", "telemetry_samples")]
        before = originals()
        self.well()
        device = self.device()
        self.command("device.change", device["id"], expected_version="1", name="改名")
        self.assertEqual(originals(), before)

    def test_timestamp_is_taken_after_actual_device_lock_wait(self):
        device = self.device()
        self.settings["writer"]["application_name"] = "registry-lock-test-" + uuid4().hex
        observer = self.connect("writer")
        observer.autocommit = True
        holder = self.connect("migrator")
        holder.execute("SELECT id FROM device_registry.devices WHERE id=%s FOR UPDATE", (device["id"],))
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(self.command, "device.change", device["id"], expected_version="1", name="锁后改名")
            try:
                deadline = time.monotonic() + 4
                while not observer.execute("SELECT count(*) FROM pg_stat_activity WHERE application_name=%s AND wait_event_type='Lock'",
                                           (self.settings["writer"]["application_name"],)).fetchone()[0]:
                    self.assertFalse(future.done(), "Writer did not wait for the held device lock")
                    self.assertLess(time.monotonic(), deadline)
                    threading.Event().wait(.01)
                released = holder.execute("SELECT clock_timestamp()").fetchone()[0]
            finally:
                holder.rollback()
            result = future.result(timeout=5)
        self.assertGreaterEqual(datetime.fromisoformat(result["entity"]["updated_at"]), released)

    def test_entity_version_overflow_is_conflict_without_update(self):
        device = self.device()
        with self.connect("migrator") as conn:
            conn.execute("UPDATE device_registry.devices SET version=9223372036854775807 WHERE id=%s", (device["id"],))
        self.assert_rejected(409, "device.change", device["id"], expected_version="9223372036854775807", name="不可溢出")
