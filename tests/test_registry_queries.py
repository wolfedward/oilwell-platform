"""Read-only registry directories, histories, independent link state and stable snapshots."""
from datetime import datetime, timedelta, timezone
import importlib
import json
from uuid import uuid4

import psycopg
from psycopg import sql

from backend.registry.errors import RegistryError
from tests.registry_support import RegistryDatabaseCase


class RegistryQueryTests(RegistryDatabaseCase):
    def setUp(self):
        super().setUp()
        try:
            self.queries = importlib.import_module("backend.registry.queries")
        except ModuleNotFoundError as error:
            if error.name != "backend.registry.queries":
                raise
            self.fail("Read-only registry queries have not been implemented")

    def device(self, **changes):
        return self.command("device.create", **({"gateway_id": "GW", "device_id": "D", "name": "终端",
                            "transport": "mqtt_direct", "usage": "commissioning"} | changes))["entity"]

    def test_empty_catalog_does_not_invent_records(self):
        for resource in ("wells", "devices"):
            result = self.read(resource)
            self.assertEqual((result["items"], result["total"], result["page"], result["page_size"], result["registry_revision"]), ([], 0, 1, 20, "1"))
            self.assertEqual(datetime.fromisoformat(result["received_before"]).utcoffset(), timedelta(0))

    def test_device_detail_and_histories_are_scoped_and_versions_are_strings(self):
        item = self.fixture(active=True)
        other = self.device(device_id="OTHER")
        detail = self.read("device", id=item["device_id"])
        self.assertEqual((detail["id"], detail["version"], detail["status"]), (item["device_id"], item["version"], "active"))
        self.assertNotIn("link_status", detail)
        states = self.read("state_history", id=item["device_id"])
        self.assertEqual([row["status"] for row in states["items"]], ["active", "pending"])
        for resource in ("bindings", "map_assignments", "point_maps"):
            result = self.read(resource, id=item["device_id"])
            self.assertEqual(result["total"], 1)
            self.assertEqual(result["items"][0]["device_id"], item["device_id"])
        maps = self.read("point_maps", id=item["device_id"])["items"][0]
        self.assertEqual((maps["version"], maps["status"], maps["sha256"]), ("1", "approved", item["sha256"]))
        self.assertIn("points", maps["content"])
        self.assertNotIn("canonical_bytes", maps)
        self.assertEqual(self.read("point_maps", id=other["id"])["items"], [])
        json.dumps({"detail": detail, "states": states, "maps": maps})

    def test_metadata_and_histories_never_read_raw_telemetry(self):
        item = self.fixture(active=True)
        role = sql.Identifier(self.settings["reader"]["user"])
        with self.connect("migrator") as conn:
            conn.execute(sql.SQL("REVOKE SELECT ON public.ingest_messages,public.telemetry_samples FROM {}").format(role))
        try:
            for resource, filters in (("wells", {}), ("devices", {}), ("device", {"id": item["device_id"]}),
                                      ("bindings", {"id": item["device_id"]}), ("point_maps", {"id": item["device_id"]}),
                                      ("map_assignments", {"id": item["device_id"]}), ("state_history", {"id": item["device_id"]}), ("audit", {})):
                self.read(resource, **filters)
            with self.assertRaises(RegistryError) as caught:
                self.read("link_status", id=item["device_id"])
            self.assertEqual(caught.exception.status, 503)
        finally:
            with self.connect("migrator") as conn:
                conn.execute(sql.SQL("GRANT SELECT ON public.ingest_messages,public.telemetry_samples TO {}").format(role))

    def test_unknown_uuid_returns_not_found_not_empty_success(self):
        for resource in ("device", "state_history", "bindings", "point_maps", "map_assignments", "link_status"):
            with self.subTest(resource=resource), self.assertRaises(RegistryError) as caught:
                self.read(resource, id=str(uuid4()))
            self.assertEqual(caught.exception.status, 404)

    def test_directory_filters_and_pagination_preserve_precision(self):
        self.device(name="甲")
        second = self.device(device_id="D2", usage="production", name="乙")
        with self.connect("migrator") as conn:
            conn.execute("UPDATE device_registry.devices SET version=9007199254740993 WHERE id=%s", (second["id"],))
        result = self.read("devices", usage="production", gateway_id="GW", device_id="D2", status="pending")
        self.assertEqual((result["total"], result["items"][0]["version"]), (1, "9007199254740993"))
        first = self.read("devices", page_size=1)
        second_page = self.read("devices", page_size=1, page=2, revision=first["registry_revision"], received_before=first["received_before"])
        self.assertEqual((first["total"], second_page["total"]), (2, 2))
        self.assertNotEqual(first["items"][0]["id"], second_page["items"][0]["id"])

    def test_invalid_query_shapes_and_pagination_fail_before_database_access(self):
        calls = []
        def forbidden():
            calls.append(True)
            raise AssertionError("Invalid query reached PostgreSQL")
        reader = self.queries.RegistryQueries(forbidden, online_seconds=120, delayed_seconds=900, clock_tolerance_seconds=300)
        cases = [("unknown", {}), ("devices", {"sql": "SELECT *"}), ("devices", {"status": "unknown"}),
                 ("devices", {"page": True}), ("devices", {"page": 0}), ("devices", {"page_size": 201}),
                 ("devices", {"page_size": 1.5}), ("devices", {"page": "9" * 1000}),
                 ("devices", {"page": 100002, "page_size": 1}), ("device", {"id": "not-uuid"}),
                 ("resolved_telemetry", {"gateway_id": "GW"}), ("resolved_telemetry", {"device_id": "D"}),
                 ("resolved_telemetry", {"well_id": str(uuid4()), "gateway_id": "GW", "device_id": "D"}),
                 ("resolved_telemetry", {}), ("devices", {"page": 2}),
                 ("resolved_telemetry", {"gateway_id": "GW", "device_id": "D", "page": 2, "revision": "1", "received_before": "2026-09-22T00:00:00Z"}),
                 ("resolved_telemetry", {"gateway_id": "GW", "device_id": "D", "query_total": True}),
                 ("resolved_telemetry", {"gateway_id": "GW", "device_id": "D", "query_total": "-1"}),
                 ("audit", {"from_time": "2026-09-23T00:00:00"}),
                 ("audit", {"from_time": "2026-09-24T00:00:00Z", "to_time": "2026-09-23T00:00:00Z"}),
                 ("audit", {"from_time": "2026-01-01T00:00:00Z", "to_time": "2026-03-01T00:00:00Z"})]
        for resource, filters in cases:
            with self.subTest(resource=resource, filters=filters), self.assertRaises(RegistryError) as caught:
                reader.read(resource, filters)
            self.assertEqual(caught.exception.status, 422)
        self.assertEqual(calls, [])

    def test_future_receipt_cutoff_is_rejected_instead_of_silently_clamped(self):
        future = self.scalar("SELECT clock_timestamp()") + timedelta(hours=1)
        with self.assertRaises(RegistryError) as caught:
            self.read("devices", received_before=future)
        self.assertEqual(caught.exception.status, 422)

    def test_audit_filters_default_window_and_original_actor_snapshot(self):
        item = self.fixture()
        result = self.read("audit", object_id=item["device_id"], actor_id="101", action="map.review")
        self.assertEqual(result["total"], 1)
        event = result["items"][0]
        self.assertEqual((event["actor"]["user_id"], event["action"]), ("101", "map.review"))
        self.assertEqual(event["object_id"], item["device_id"])
        with self.connect("migrator") as conn:
            conn.execute("INSERT INTO device_registry.audit_events(id,actor,action,object_type,object_id,reason,before_data,after_data,event_at,request_id) "
                         "VALUES (%s,'{}','old.fixture','device',%s,'历史记录','{}','{}',clock_timestamp()-interval '2 days',%s)",
                         (uuid4(), item["device_id"], uuid4()))
        current = self.read("audit", object_id=item["device_id"])
        self.assertNotIn("old.fixture", [row["action"] for row in current["items"]])
        now = self.scalar("SELECT clock_timestamp()")
        history = self.read("audit", object_id=item["device_id"], from_time=now - timedelta(days=3), to_time=now)
        self.assertIn("old.fixture", [row["action"] for row in history["items"]])

    def test_active_catalog_without_messages_is_never_reported_not_online(self):
        item = self.fixture(active=True)
        link = self.read("link_status", id=item["device_id"])
        self.assertEqual(link["link_status"], "never_reported")
        self.assertIsNone(link["last_received_at"])
        self.assertEqual(self.read("device", id=item["device_id"])["status"], "active")

    def test_link_state_uses_received_time_and_separates_gateway_plus_device(self):
        now = self.scalar("SELECT clock_timestamp()")
        for index, (age, expected) in enumerate(((10, "online"), (300, "delayed"), (1800, "offline"), (-600, "clock_anomaly"))):
            device = self.device(gateway_id="GW" + str(index))
            with self.connect("migrator") as conn:
                conn.execute("INSERT INTO public.ingest_messages(message_id,topic,gateway_id,device_id,boot_id,sequence_first,sequence_last,created_at,received_at,payload_sha256,raw_payload) "
                             "VALUES (%s,'fixture',%s,'D','boot',1,1,%s,%s,%s,'{}')",
                             ("link-" + str(index), "GW" + str(index), now - timedelta(days=10), now - timedelta(seconds=age), "0" * 64))
            link = self.read("link_status", id=device["id"])
            self.assertEqual(link["link_status"], expected)
            self.assertEqual(link["gateway_id"], "GW" + str(index))
            self.assertEqual(link["thresholds"]["online_seconds"], 120)
            self.assertEqual(link["thresholds"]["delayed_seconds"], 900)

    def test_repeatable_read_keeps_revision_and_rows_from_one_real_snapshot(self):
        well = self.command("well.create", area_code="A", well_code="W", name="旧名")["entity"]
        prior_revision = str(self.scalar("SELECT revision FROM device_registry.registry_head"))
        case = self
        class ConnectionProxy:
            def __init__(self):
                self.real = case.connect("reader")
                self.fired = False
            def __getattr__(self, key):
                return getattr(self.real, key)
            def __enter__(self):
                self.real.__enter__()
                return self
            def __exit__(self, *args):
                return self.real.__exit__(*args)
            def cursor(self, *args, **kwargs):
                real = self.real.cursor(*args, **kwargs)
                owner = self
                class CursorProxy:
                    def __getattr__(self, key):
                        return getattr(real, key)
                    def __enter__(self):
                        real.__enter__()
                        return self
                    def __exit__(self, *args):
                        return real.__exit__(*args)
                    def execute(self, statement, params=None):
                        real.execute(statement, params)
                        if "registry_head" in str(statement) and not owner.fired:
                            owner.fired = True
                            isolation = owner.real.execute("SHOW transaction_isolation").fetchone()
                            read_only = owner.real.execute("SHOW transaction_read_only").fetchone()
                            case.assertEqual(next(iter(isolation.values())) if isinstance(isolation, dict) else isolation[0], "repeatable read")
                            case.assertEqual(next(iter(read_only.values())) if isinstance(read_only, dict) else read_only[0], "on")
                            case.command("well.change", well["id"], expected_version="1", name="新名")
                        return self
                return CursorProxy()
        connection = ConnectionProxy()
        reader = self.queries.RegistryQueries(lambda: connection, online_seconds=120, delayed_seconds=900, clock_tolerance_seconds=300)
        result = reader.read("wells", {})
        self.assertTrue(connection.fired, "Test hook did not reach the real revision SELECT")
        self.assertEqual((result["registry_revision"], result["items"][0]["name"]), (prior_revision, "旧名"))
        fresh = self.read("wells")
        self.assertEqual(fresh["items"][0]["name"], "新名")
        self.assertNotEqual(fresh["registry_revision"], prior_revision)

    def test_connection_error_is_sanitized_not_an_empty_catalog(self):
        def disconnected():
            raise psycopg.OperationalError("host=private password=do-not-leak")
        reader = self.queries.RegistryQueries(disconnected, online_seconds=120, delayed_seconds=900, clock_tolerance_seconds=300)
        with self.assertRaises(RegistryError) as caught:
            reader.read("devices", {})
        self.assertEqual(caught.exception.status, 503)
        self.assertNotIn("private", str(caught.exception))
        self.assertNotIn("do-not-leak", str(caught.exception))

    def test_missing_revision_head_is_unavailable_not_a_successful_empty_catalog(self):
        with self.connect("migrator") as conn:
            conn.execute("DELETE FROM device_registry.registry_head")
        try:
            with self.assertRaises(RegistryError) as caught:
                self.read("devices")
            self.assertEqual(caught.exception.status, 503)
        finally:
            with self.connect("migrator") as conn:
                conn.execute("INSERT INTO device_registry.registry_head(singleton,revision) VALUES (1,1)")
