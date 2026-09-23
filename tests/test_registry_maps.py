"""Immutable per-device point-map snapshots and one-time review on real PostgreSQL."""
from concurrent.futures import ThreadPoolExecutor
import hashlib
import importlib
import json
import threading
import time
from uuid import uuid4

import psycopg
from psycopg.types.json import Jsonb

from backend.registry.errors import RegistryError
from backend.registry.point_maps import canonical_point_map
from tests.registry_support import RegistryDatabaseCase


class RegistryMapTests(RegistryDatabaseCase):
    def setUp(self):
        super().setUp()
        try:
            importlib.import_module("backend.registry.maps")
        except ModuleNotFoundError as error:
            if error.name != "backend.registry.maps":
                raise
            self.fail("Production point-map lifecycle has not been implemented")

    def device(self, **changes):
        return self.command("device.create", **({"gateway_id": "GW", "device_id": "D", "name": "终端",
                            "transport": "mqtt_direct", "usage": "commissioning"} | changes))["entity"]

    def create_map(self, device, **changes):
        return self.command("map.create", device["id"], **({"expected_version": device["version"],
                            "parent_version": None, "points": [{"point_code": "load", "unit": "kN"}]} | changes))["entity"]

    def review(self, item, **changes):
        return self.command("map.review", item["id"], **({"expected_version": item["device_version"],
                            "map_version": item["map_version"], "sha256": item["sha256"],
                            "decision": "approved"} | changes))["entity"]

    def assert_rejected(self, expected_status, action, target, **body):
        prior = self.scalar("SELECT revision FROM device_registry.registry_head")
        with self.assertRaises(RegistryError) as caught:
            self.command(action, target, **body)
        self.assertEqual(caught.exception.status, expected_status)
        self.assertEqual(self.scalar("SELECT revision FROM device_registry.registry_head"), prior)
        return caught.exception

    def test_creation_stores_exact_canonical_bytes_and_advances_device_once(self):
        device = self.device()
        points = [{"point_code": "z", "unit": "米", "engineering_max": 100},
                  {"point_code": "a", "unit": "kN", "engineering_min": -0.0}]
        item = self.create_map(device, points=points)
        document, raw, digest = canonical_point_map("mqtt_direct", points)
        with self.connect("reader") as conn:
            stored = conn.execute("SELECT canonical_bytes,content,sha256,canonical_rule,status,created_at "
                                  "FROM device_registry.point_map_versions").fetchone()
        self.assertEqual(stored[:5], (raw, document, digest, document["rule"], "draft"))
        self.assertEqual(hashlib.sha256(stored[0]).hexdigest(), digest)
        self.assertEqual((item["id"], item["version"], item["device_version"], item["map_version"], item["sha256"]),
                         (device["id"], "2", "2", "1", digest))
        self.assertEqual(self.scalar("SELECT version FROM device_registry.devices"), 2)
        self.assertEqual(self.scalar("SELECT updated_at FROM device_registry.devices"), stored[5])
        self.assertEqual(self.scalar("SELECT count(*) FROM device_registry.point_map_assignments"), 0)
        self.assertEqual(self.scalar("SELECT count(*) FROM device_registry.device_state_periods"), 1)
        json.dumps(item)

    def test_review_requires_exact_snapshot_hash_and_preserves_draft_on_conflict(self):
        device = self.device()
        item = self.create_map(device)
        failure = self.assert_rejected(409, "map.review", device["id"], expected_version="2", map_version="1",
                                       sha256="0" * 64, decision="approved")
        self.assertEqual(failure.code, "registry.snapshot_conflict")
        self.assertEqual(self.scalar("SELECT status FROM device_registry.point_map_versions"), "draft")
        self.assertEqual(self.scalar("SELECT version FROM device_registry.devices"), 2)
        approved = self.review(item)
        self.assertEqual((approved["status"], approved["version"], approved["device_version"]), ("approved", "3", "3"))

    def test_approved_and_rejected_decisions_are_final(self):
        for index, decision in enumerate(("approved", "rejected")):
            device = self.device(device_id=f"D{index}")
            item = self.create_map(device)
            reviewed = self.review(item, decision=decision)
            self.assert_rejected(409, "map.review", device["id"], expected_version=reviewed["device_version"],
                                 map_version=item["map_version"], sha256=item["sha256"], decision="approved")
            with self.connect("writer") as conn:
                with self.assertRaises(psycopg.Error):
                    conn.execute("UPDATE device_registry.point_map_versions SET reviewed_at=clock_timestamp() "
                                 "WHERE device_id=%s", (device["id"],))
                conn.rollback()

    def test_writer_cannot_replace_draft_content_hash_bytes_or_parent(self):
        device = self.device()
        item = self.create_map(device)
        statements = ["SET canonical_bytes='changed'::bytea", "SET content='{}'::jsonb",
                      "SET parent_version=NULL", "SET sha256='" + "f" * 64 + "'"]
        for statement in statements:
            with self.connect("writer") as conn:
                with self.assertRaises(psycopg.errors.InsufficientPrivilege):
                    conn.execute("UPDATE device_registry.point_map_versions " + statement + " WHERE device_id=%s", (device["id"],))
                conn.rollback()
        self.assertEqual(self.scalar("SELECT sha256 FROM device_registry.point_map_versions"), item["sha256"])

    def test_same_content_still_creates_new_version_and_never_updates_draft(self):
        device = self.device()
        first = self.create_map(device)
        before = self.scalar("SELECT to_jsonb(t)-'canonical_bytes' FROM device_registry.point_map_versions t")
        second = self.create_map(device | {"version": first["device_version"]}, parent_version="1")
        self.assertEqual((second["map_version"], second["device_version"]), ("2", "3"))
        self.assertEqual(first["sha256"], second["sha256"])
        self.assertEqual(self.scalar("SELECT to_jsonb(t)-'canonical_bytes' FROM device_registry.point_map_versions t WHERE version=1"), before)
        self.assertEqual(self.scalar("SELECT parent_version FROM device_registry.point_map_versions WHERE version=2"), 1)

    def test_parent_must_be_latest_of_this_device(self):
        device = self.device()
        self.assert_rejected(409, "map.create", device["id"], expected_version="1", parent_version="1", points=[{"point_code": "load"}])
        item = self.create_map(device)
        for parent in (None, "2"):
            self.assert_rejected(409, "map.create", device["id"], expected_version=item["device_version"],
                                 parent_version=parent, points=[{"point_code": "load"}])
        second = self.create_map(device | {"version": "2"}, parent_version="1")
        self.assert_rejected(409, "map.create", device["id"], expected_version=second["device_version"],
                             parent_version="1", points=[{"point_code": "load"}])

    def test_other_device_version_is_not_a_review_target(self):
        first = self.create_map(self.device())
        second = self.device(device_id="D2")
        self.assert_rejected(404, "map.review", second["id"], expected_version="1", map_version="1",
                             sha256=first["sha256"], decision="approved")

    def test_device_transport_controls_point_semantics(self):
        modbus_point = {"point_code": "load", "unit": "kN", "unit_id": 1, "function_code": 3,
                        "address": 12, "data_type": "float32"}
        direct = self.device()
        self.assert_rejected(422, "map.create", direct["id"], expected_version="1", parent_version=None, points=[modbus_point])
        modbus = self.device(device_id="D2", transport="modbus_tcp")
        self.assert_rejected(422, "map.create", modbus["id"], expected_version="1", parent_version=None, points=[{"point_code": "load"}])
        item = self.create_map(modbus, points=[modbus_point])
        self.assertEqual(self.scalar("SELECT transport FROM device_registry.point_map_versions WHERE device_id=%s", (modbus["id"],)), "modbus_tcp")
        self.assertEqual(item["sha256"], canonical_point_map("modbus_tcp", [modbus_point])[2])
        self.assert_rejected(422, "map.create", modbus["id"], expected_version="2", parent_version="1",
                             transport="mqtt_direct", points=[{"point_code": "load"}])

    def test_metadata_change_makes_earlier_review_version_stale(self):
        device = self.device()
        item = self.create_map(device)
        self.command("device.change", device["id"], expected_version="2", name="新名")
        self.assert_rejected(409, "map.review", device["id"], expected_version="2", map_version="1",
                             sha256=item["sha256"], decision="approved")
        self.assertEqual(self.scalar("SELECT status FROM device_registry.point_map_versions"), "draft")

    def test_concurrent_reviews_have_one_winner(self):
        item = self.create_map(self.device())
        barrier = threading.Barrier(2)
        def review(decision):
            barrier.wait(timeout=5)
            try:
                self.review(item, decision=decision)
                return "ok"
            except RegistryError as error:
                return error.status
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(review, decision) for decision in ("approved", "rejected")]
            self.assertCountEqual([future.result(timeout=10) for future in futures], ["ok", 409])
        self.assertEqual(self.scalar("SELECT count(*) FROM device_registry.audit_events WHERE action='map.review'"), 1)
        self.assertEqual(self.scalar("SELECT version FROM device_registry.devices"), 3)

    def test_retired_device_rejects_creation_and_review(self):
        device = self.device()
        item = self.create_map(device)
        with self.connect("migrator") as conn:
            conn.execute("UPDATE device_registry.devices SET status='retired' WHERE id=%s", (device["id"],))
        self.assert_rejected(409, "map.create", device["id"], expected_version="2", parent_version="1", points=[{"point_code": "load"}])
        self.assert_rejected(409, "map.review", device["id"], expected_version="2", map_version="1", sha256=item["sha256"], decision="approved")

    def test_audit_links_exact_review_and_creation_instants_and_snapshot(self):
        item = self.create_map(self.device())
        self.review(item, decision="rejected")
        with self.connect("reader") as conn:
            row = conn.execute("SELECT p.created_at,p.reviewed_at,c.event_at,r.event_at,r.after_data "
                               "FROM device_registry.point_map_versions p JOIN device_registry.audit_events c ON c.id=p.created_event_id "
                               "JOIN device_registry.audit_events r ON r.id=p.review_event_id").fetchone()
        self.assertEqual(row[:2], row[2:4])
        self.assertEqual((row[4]["map_version"], row[4]["sha256"], row[4]["status"]), ("1", item["sha256"], "rejected"))
        self.assertEqual(row[4]["canonical_rule"], "registry.point-map.canonical.v1")
        self.assertEqual(self.scalar("SELECT count(*) FROM device_registry.point_map_assignments"), 0)

    def test_retries_reuse_both_creation_and_review_receipts(self):
        device = self.device()
        create_key, review_key = uuid4(), uuid4()
        body = dict(expected_version="1", parent_version=None, points=[{"point_code": "load"}])
        first = self.command("map.create", device["id"], key=create_key, **body)
        replay = self.command("map.create", device["id"], key=create_key, **body)
        self.assertEqual(replay, first | {"replayed": True})
        item = first["entity"]
        body = dict(expected_version="2", map_version="1", sha256=item["sha256"], decision="approved")
        review = self.command("map.review", device["id"], key=review_key, **body)
        replay = self.command("map.review", device["id"], key=review_key, **body)
        self.assertEqual(replay, review | {"replayed": True})
        self.assertEqual(self.scalar("SELECT count(*) FROM device_registry.point_map_versions"), 1)
        self.assertEqual(self.scalar("SELECT count(*) FROM device_registry.audit_events"), 3)

    def test_review_timestamp_follows_actual_point_map_lock_wait(self):
        item = self.create_map(self.device())
        self.settings["writer"]["application_name"] = "registry-map-lock-" + uuid4().hex
        observer = self.connect("writer")
        observer.autocommit = True
        holder = self.connect("migrator")
        holder.execute("SELECT version FROM device_registry.point_map_versions WHERE device_id=%s FOR UPDATE", (item["id"],))
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(self.review, item)
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
            future.result(timeout=5)
        reviewed_at = self.scalar("SELECT reviewed_at FROM device_registry.point_map_versions")
        self.assertGreaterEqual(reviewed_at, released)

    def test_map_version_overflow_rejects_without_advancing_device(self):
        device = self.device()
        document, raw, digest = canonical_point_map("mqtt_direct", [{"point_code": "load"}])
        with self.connect("migrator") as conn:
            event = conn.execute("SELECT id FROM device_registry.audit_events").fetchone()[0]
            conn.execute("INSERT INTO device_registry.point_map_versions(device_id,version,transport,canonical_rule,"
                         "canonical_bytes,content,sha256,created_event_id) VALUES (%s,9223372036854775807,'mqtt_direct',%s,%s,%s,%s,%s)",
                         (device["id"], document["rule"], raw, Jsonb(document), digest, event))
        failure = self.assert_rejected(409, "map.create", device["id"], expected_version="1", parent_version="9223372036854775807",
                                       points=[{"point_code": "load"}])
        self.assertEqual(failure.code, "registry.version_exhausted")
        self.assertEqual(self.scalar("SELECT version FROM device_registry.devices"), 1)
