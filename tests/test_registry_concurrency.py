"""Two real PostgreSQL sessions prove ordering, races and bounded lock failures."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
import importlib
import threading
import time
from uuid import uuid4

from backend.registry.errors import RegistryError
from tests.registry_support import RegistryDatabaseCase


class RegistryConcurrencyTests(RegistryDatabaseCase):
    def setUp(self):
        super().setUp()
        try:
            importlib.import_module("backend.registry.lifecycle")
        except ModuleNotFoundError as error:
            if error.name != "backend.registry.lifecycle":
                raise
            self.fail("Temporal registry lifecycle has not been implemented")

    def well(self):
        return self.command("well.create", area_code="A", well_code="W" + uuid4().hex, name="新井")["entity"]

    def race(self, jobs):
        barrier = threading.Barrier(len(jobs))
        def run(action, target, body):
            barrier.wait(timeout=5)
            try:
                self.command(action, target, **body)
                return "ok"
            except RegistryError as error:
                return error.status
        with ThreadPoolExecutor(max_workers=len(jobs)) as pool:
            futures = [pool.submit(run, *job) for job in jobs]
            return [future.result(timeout=10) for future in futures]

    def test_archive_and_bind_cannot_both_succeed(self):
        well = self.well()
        device = self.command("device.create", gateway_id="GW", device_id="D", name="终端", transport="mqtt_direct", usage="commissioning")["entity"]
        results = self.race([("well.change", well["id"], {"expected_version": "1", "status": "archived"}),
                             ("device.bind", device["id"], {"expected_version": "1", "well_id": well["id"]})])
        self.assertCountEqual(results, ["ok", 409])
        self.assertEqual(self.scalar("SELECT count(*) FROM device_registry.well_bindings b JOIN device_registry.wells w "
                                     "ON w.id=b.well_id WHERE b.valid_to IS NULL AND w.status='archived'"), 0)

    def test_two_moves_with_same_device_version_have_exactly_one_success(self):
        item, first, second = self.fixture(active=True), self.well(), self.well()
        before = self.scalar("SELECT count(*) FROM device_registry.audit_events")
        results = self.race([("device.bind", item["device_id"], {"expected_version": item["version"], "well_id": well["id"]})
                             for well in (first, second)])
        self.assertCountEqual(results, ["ok", 409])
        self.assertEqual(self.scalar("SELECT count(*) FROM device_registry.well_bindings"), 2)
        self.assertEqual(self.scalar("SELECT count(*) FROM device_registry.well_bindings WHERE valid_to IS NULL"), 1)
        self.assertEqual(self.scalar("SELECT version FROM device_registry.devices"), int(item["version"]) + 1)
        self.assertEqual(self.scalar("SELECT count(*) FROM device_registry.audit_events"), before + 1)

    def test_head_lock_timeout_has_no_partial_effect_and_same_key_can_retry(self):
        item, well, key = self.fixture(active=True), self.well(), uuid4()
        before = self.scalar("SELECT revision FROM device_registry.registry_head")
        holder = self.connect("migrator")
        holder.execute("SELECT revision FROM device_registry.registry_head FOR UPDATE")
        body = dict(expected_version=item["version"], well_id=well["id"])
        try:
            with self.assertRaises(RegistryError) as caught:
                self.command("device.bind", item["device_id"], key=key, **body)
            self.assertEqual(caught.exception.status, 503)
        finally:
            holder.rollback()
        self.assertEqual(self.scalar("SELECT revision FROM device_registry.registry_head"), before)
        self.assertEqual(self.scalar("SELECT count(*) FROM device_registry.well_bindings"), 1)
        result = self.command("device.bind", item["device_id"], key=key, **body)
        self.assertFalse(result["replayed"])
        self.assertEqual(result["entity"]["status"], "suspended")

    def assert_reference_lock_before_time(self, statement, params, action, target, **body):
        self.settings["writer"]["application_name"] = "registry-ref-lock-" + uuid4().hex
        observer = self.connect("writer")
        observer.autocommit = True
        holder = self.connect("migrator")
        holder.execute(statement, params)
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(self.command, action, target, **body)
            try:
                deadline = time.monotonic() + 4
                while not observer.execute("SELECT count(*) FROM pg_stat_activity WHERE application_name=%s AND wait_event_type='Lock'",
                                           (self.settings["writer"]["application_name"],)).fetchone()[0]:
                    self.assertFalse(future.done())
                    self.assertLess(time.monotonic(), deadline)
                    threading.Event().wait(.01)
                released = holder.execute("SELECT clock_timestamp()").fetchone()[0]
            finally:
                holder.rollback()
            result = future.result(timeout=5)
        self.assertGreaterEqual(datetime.fromisoformat(result["committed_at"]), released)
        return result

    def test_bind_timestamp_is_after_waiting_for_target_well_lock(self):
        item, well = self.fixture(), self.well()
        result = self.assert_reference_lock_before_time("SELECT id FROM device_registry.wells WHERE id=%s FOR UPDATE", (well["id"],),
                 "device.bind", item["device_id"], expected_version=item["version"], well_id=well["id"])
        self.assertEqual(self.scalar("SELECT valid_from FROM device_registry.well_bindings WHERE well_id=%s", (well["id"],)),
                         datetime.fromisoformat(result["committed_at"]))

    def test_assignment_timestamp_is_after_waiting_for_snapshot_lock(self):
        item = self.fixture()
        newmap = self.command("map.create", item["device_id"], expected_version=item["version"], parent_version="1", points=[{"point_code": "new"}])["entity"]
        reviewed = self.command("map.review", item["device_id"], expected_version=newmap["device_version"], map_version="2",
                                sha256=newmap["sha256"], decision="approved")["entity"]
        result = self.assert_reference_lock_before_time("SELECT version FROM device_registry.point_map_versions WHERE device_id=%s AND version=2 FOR UPDATE",
                 (item["device_id"],), "map.assign", item["device_id"], expected_version=reviewed["device_version"], map_version="2")
        self.assertEqual(self.scalar("SELECT valid_from FROM device_registry.point_map_assignments WHERE map_version=2"),
                         datetime.fromisoformat(result["committed_at"]))

    def test_unbind_timestamp_is_after_waiting_for_open_history_row(self):
        item = self.fixture(active=True)
        result = self.assert_reference_lock_before_time("SELECT id FROM device_registry.well_bindings WHERE device_id=%s AND valid_to IS NULL FOR UPDATE",
                 (item["device_id"],), "device.unbind", item["device_id"], expected_version=item["version"])
        self.assertEqual(self.scalar("SELECT valid_to FROM device_registry.well_bindings WHERE device_id=%s", (item["device_id"],)),
                         datetime.fromisoformat(result["committed_at"]))
