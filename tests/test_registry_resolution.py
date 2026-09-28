"""Historical interpretation never rewrites raw engineering values or asserts hardware approval."""
import copy
from datetime import datetime, timedelta, timezone
import importlib
import unittest
from uuid import uuid4

from psycopg.types.json import Jsonb

from backend.registry.errors import RegistryError
from backend.registry.point_maps import canonical_point_map
from tests.registry_support import RegistryDatabaseCase


class RegistryAssessmentTests(unittest.TestCase):
    def setUp(self):
        try:
            self.assess = importlib.import_module("backend.registry.resolution").assess
        except ModuleNotFoundError as error:
            if error.name != "backend.registry.resolution":
                raise
            self.fail("Historical telemetry assessment has not been implemented")
        self.sample = {"value": 32.4, "unit": "kN", "quality": "good", "point_code": "load", "source": "untrusted-source"}
        self.context = {"registered": True, "bound": True, "state": "active", "clock_valid": True,
                        "map_version": "9007199254740993", "point": {"unit": "kN", "scale": 100, "offset": 20,
                        "engineering_min": 0.0, "engineering_max": 80.0}}

    def test_engineering_value_is_not_scaled_or_provenance_overwritten(self):
        sample, context = copy.deepcopy(self.sample), copy.deepcopy(self.context)
        result = self.assess(sample, context)
        self.assertEqual((result["value"], result["source"], result["quality"]), (32.4, "untrusted-source", "good"))
        self.assertFalse(result["eligible_for_automation"])
        self.assertEqual(result["application_status"], "unverified")
        self.assertIsNone(result["reported_map_version"])
        self.assertEqual(result["expected_map_version"], "9007199254740993")
        self.assertEqual(result["reasons"], [])
        self.assertTrue(result["matched"])
        self.assertEqual((sample, context), (self.sample, self.context))

    def test_registration_binding_state_and_missing_map_are_independent_reasons(self):
        for key, value, reason in (("registered", False, "not_registered"), ("bound", False, "unbound"),
                                   ("state", "suspended", "state_not_active"), ("state", "retired", "state_not_active"),
                                   ("map_version", None, "no_expected_map"), ("point", None, "unknown_point"),
                                   ("clock_valid", False, "clock_anomaly")):
            with self.subTest(reason=reason):
                result = self.assess(self.sample, self.context | {key: value})
                self.assertIn(reason, result["reasons"])
                self.assertFalse(result["matched"])
                self.assertFalse(result["eligible_for_automation"])

    def test_unit_is_exact_and_quality_is_never_upgraded(self):
        for unit in (None, "KN", "kN "):
            self.assertIn("unit_mismatch", self.assess(self.sample | {"unit": unit}, self.context)["reasons"])
        for quality in ("bad", "uncertain"):
            result = self.assess(self.sample | {"quality": quality}, self.context)
            self.assertIn("quality_" + quality, result["reasons"])
            self.assertEqual(result["quality"], quality)
        no_unit = self.context | {"point": {"unit": None, "engineering_min": None, "engineering_max": None}}
        self.assertNotIn("unit_mismatch", self.assess(self.sample | {"unit": None}, no_unit)["reasons"])

    def test_engineering_bounds_are_inclusive_and_optional(self):
        for value in (0.0, 80.0):
            self.assertNotIn("out_of_range", self.assess(self.sample | {"value": value}, self.context)["reasons"])
        for value in (-0.001, 80.001):
            self.assertIn("out_of_range", self.assess(self.sample | {"value": value}, self.context)["reasons"])
        unbounded = self.context | {"point": {"unit": "kN", "engineering_min": None, "engineering_max": None}}
        self.assertNotIn("out_of_range", self.assess(self.sample | {"value": -1000}, unbounded)["reasons"])


class RegistryResolutionTests(RegistryDatabaseCase):
    def setUp(self):
        super().setUp()
        try:
            importlib.import_module("backend.registry.queries")
        except ModuleNotFoundError as error:
            if error.name != "backend.registry.queries":
                raise
            self.fail("Historical registry queries have not been implemented")

    def history(self):
        t = self.scalar("SELECT clock_timestamp()") - timedelta(hours=2)
        device, well_a, well_b, event = [uuid4() for _ in range(4)]
        document, raw, digest = canonical_point_map("mqtt_direct", [{"point_code": "load", "unit": "kN", "engineering_min": 0, "engineering_max": 80}])
        with self.connect("migrator") as conn:
            conn.execute("INSERT INTO device_registry.audit_events(id,actor,action,object_type,object_id,reason,before_data,after_data,event_at,request_id) "
                         "VALUES (%s,'{}','fixture','device',%s,'历史查询测试','{}','{}',%s,%s)", (event, device, t - timedelta(hours=1), uuid4()))
            for identifier, code in ((well_a, "A"), (well_b, "B")):
                conn.execute("INSERT INTO device_registry.wells(id,area_code,well_code,name,created_at,updated_at) VALUES (%s,'AREA',%s,%s,%s,%s)",
                             (identifier, code, code + "井", t - timedelta(hours=2), t - timedelta(hours=2)))
            conn.execute("INSERT INTO device_registry.devices(id,gateway_id,device_id,name,transport,usage,status,usage_locked,version,created_at,updated_at) "
                         "VALUES (%s,'GW','D','终端','mqtt_direct','commissioning','retired',true,4,%s,%s)", (device, t - timedelta(hours=1), t + timedelta(minutes=1)))
            for well, start, end in ((well_a, t - timedelta(hours=1), t), (well_b, t, t + timedelta(minutes=1))):
                conn.execute("INSERT INTO device_registry.well_bindings(device_id,well_id,valid_from,valid_to,audit_event_id) VALUES (%s,%s,%s,%s,%s)",
                             (device, well, start, end, event))
            for state, start, end in (("active", t - timedelta(hours=1), t), ("suspended", t, t + timedelta(minutes=1)), ("retired", t + timedelta(minutes=1), None)):
                conn.execute("INSERT INTO device_registry.device_state_periods(device_id,status,valid_from,valid_to,audit_event_id) VALUES (%s,%s,%s,%s,%s)",
                             (device, state, start, end, event))
            conn.execute("INSERT INTO device_registry.point_map_versions(device_id,version,transport,canonical_rule,canonical_bytes,content,sha256,status,created_at,created_event_id,reviewed_at,review_event_id) "
                         "VALUES (%s,1,'mqtt_direct',%s,%s,%s,%s,'approved',%s,%s,%s,%s)",
                         (device, document["rule"], raw, Jsonb(document), digest, t - timedelta(hours=1), event, t - timedelta(hours=1), event))
            conn.execute("INSERT INTO device_registry.point_map_assignments(device_id,map_version,valid_from,valid_to,audit_event_id) VALUES (%s,1,%s,%s,%s)",
                         (device, t - timedelta(hours=1), t, event))
        return {"t": t, "device": str(device), "well_a": str(well_a), "well_b": str(well_b)}

    def sample(self, measured, *, received=None, gateway="GW", device="D", index=0, value=32.4, unit="kN", quality="good", point="load"):
        received = received or self.scalar("SELECT clock_timestamp()")
        message = "sample-" + uuid4().hex
        with self.connect("migrator") as conn:
            conn.execute("INSERT INTO public.ingest_messages(message_id,topic,gateway_id,device_id,boot_id,sequence_first,sequence_last,created_at,received_at,payload_sha256,raw_payload) "
                         "VALUES (%s,'fixture',%s,%s,'boot',1,1,%s,%s,%s,'{}')", (message, gateway, device, measured, received, "0" * 64))
            conn.execute("INSERT INTO public.telemetry_samples(message_id,sample_index,gateway_id,device_id,point_code,measured_at,sequence,value,quality,unit,source,received_at) "
                         "VALUES (%s,%s,%s,%s,%s,%s,1,%s,%s,%s,'fixture',%s)", (message, index, gateway, device, point, measured, value, quality, unit, received))
        return message

    def test_late_samples_keep_microsecond_historical_well_and_state_after_retirement(self):
        item = self.history()
        t = item["t"]
        for measured in (t - timedelta(microseconds=1), t, t + timedelta(microseconds=1)):
            self.sample(measured, received=t + timedelta(hours=1))
        result = self.read("resolved_telemetry", gateway_id="GW", device_id="D", from_time=t - timedelta(seconds=1), to_time=t + timedelta(seconds=1))
        self.assertEqual(result["total"], 3)
        self.assertCountEqual([(row["well_code"], row["sample_state"], row["current_state"]) for row in result["items"]],
                             [("A", "active", "retired"), ("B", "suspended", "retired"), ("B", "suspended", "retired")])
        oldest = result["items"][-1]
        self.assertEqual((oldest["value"], oldest["expected_map_version"], oldest["application_status"], oldest["quality"]), (32.4, "1", "unverified", "good"))
        self.assertNotIn("clock_anomaly", oldest["reasons"])
        self.assertTrue(oldest["matched"])
        self.assertFalse(oldest["eligible_for_automation"])
        self.assertIsNone(oldest["reported_map_version"])
        self.assertEqual(oldest["source"], "fixture")
        self.assertEqual(oldest["sequence"], "1")

    def test_well_filter_uses_historical_binding_and_exclusive_end(self):
        item = self.history()
        t = item["t"]
        self.sample(t - timedelta(microseconds=1))
        self.sample(t)
        rows = self.read("resolved_telemetry", well_id=item["well_a"], from_time=t - timedelta(seconds=1), to_time=t + timedelta(seconds=1))
        self.assertEqual([row["well_code"] for row in rows["items"]], ["A"])
        rows = self.read("resolved_telemetry", gateway_id="GW", device_id="D", from_time=t, to_time=t + timedelta(seconds=1))
        self.assertEqual([row["well_code"] for row in rows["items"]], ["B"])
        rows = self.read("resolved_telemetry", gateway_id="GW", device_id="D", from_time=t - timedelta(seconds=1), to_time=t)
        self.assertEqual([row["well_code"] for row in rows["items"]], ["A"])

    def test_unregistered_pairs_and_pre_registration_samples_stay_unregistered(self):
        item = self.history()
        measured = item["t"] - timedelta(hours=1, microseconds=1)
        self.sample(measured)
        self.sample(measured, gateway="UNKNOWN")
        for gateway in ("GW", "UNKNOWN"):
            rows = self.read("resolved_telemetry", gateway_id=gateway, device_id="D", from_time=measured - timedelta(seconds=1), to_time=measured + timedelta(seconds=1))
            row = rows["items"][0]
            self.assertIsNone(row["registry_device_id"])
            self.assertIsNone(row["well_id"])
            self.assertIn("not_registered", row["reasons"])
        self.assertEqual(self.scalar("SELECT count(*) FROM device_registry.devices"), 1)

    def test_unit_range_quality_and_unknown_point_are_read_only_assessments(self):
        item = self.history()
        measured = item["t"] - timedelta(seconds=5)
        for changes in ({"unit": "KN"}, {"value": 80.1}, {"quality": "bad"}, {"quality": "uncertain"}, {"point": "unknown"}):
            self.sample(measured, **changes)
        before = self.scalar("SELECT jsonb_agg(to_jsonb(s) ORDER BY message_id) FROM public.telemetry_samples s")
        result = self.read("resolved_telemetry", gateway_id="GW", device_id="D", from_time=measured - timedelta(seconds=1), to_time=measured + timedelta(seconds=1))
        reasons = {reason for row in result["items"] for reason in row["reasons"]}
        self.assertEqual(reasons, {"unit_mismatch", "out_of_range", "quality_bad", "quality_uncertain", "unknown_point"})
        self.assertEqual(self.scalar("SELECT jsonb_agg(to_jsonb(s) ORDER BY message_id) FROM public.telemetry_samples s"), before)

    def test_receipt_cutoff_keeps_later_arrivals_out_of_subsequent_pages(self):
        now = self.scalar("SELECT clock_timestamp()")
        for index in range(2):
            self.sample(now - timedelta(seconds=10 + index))
        filters = dict(gateway_id="GW", device_id="D", from_time=now - timedelta(minutes=1), to_time=now + timedelta(minutes=1), page_size=1)
        first = self.read("resolved_telemetry", **filters)
        late_id = self.sample(now - timedelta(seconds=1))
        second = self.read("resolved_telemetry", **filters, page=2, revision=first["registry_revision"], received_before=first["received_before"], query_total=first["query_total"])
        self.assertEqual((first["total"], second["total"]), (2, 2))
        self.assertEqual(second["received_before"], first["received_before"])
        self.assertNotEqual(first["items"][0]["message_id"], second["items"][0]["message_id"])
        self.assertNotIn(late_id, [row["message_id"] for row in first["items"] + second["items"]])

    def test_registry_change_between_pages_requires_refresh(self):
        now = self.scalar("SELECT clock_timestamp()")
        filters = dict(gateway_id="GW", device_id="D", from_time=now - timedelta(hours=1), to_time=now + timedelta(seconds=1), page_size=1)
        for _ in range(2):
            self.sample(now - timedelta(seconds=10))
        first = self.read("resolved_telemetry", **filters)
        self.command("well.create", area_code="A", well_code="W", name="修订变化")
        with self.assertRaises(RegistryError) as caught:
            self.read("resolved_telemetry", **filters, page=2, revision=first["registry_revision"], received_before=first["received_before"], query_total=first["query_total"])
        self.assertEqual((caught.exception.code, caught.exception.status), ("registry.query_changed", 409))

    def test_default_window_does_not_slide_when_following_the_same_cutoff(self):
        now = self.scalar("SELECT clock_timestamp()")
        for index in range(2):
            self.sample(now - timedelta(seconds=10 + index))
        first = self.read("resolved_telemetry", gateway_id="GW", device_id="D", page_size=1)
        second = self.read("resolved_telemetry", gateway_id="GW", device_id="D", page_size=1, page=2,
                           revision=first["registry_revision"], received_before=first["received_before"], query_total=first["query_total"])
        self.assertEqual((second["from_time"], second["to_time"]), (first["from_time"], first["to_time"]))
        self.assertEqual(first["to_time"], first["received_before"])

    def test_future_measurement_is_anomaly_but_late_arrival_is_not(self):
        now = self.scalar("SELECT clock_timestamp()")
        self.sample(now + timedelta(seconds=400), received=now)
        result = self.read("resolved_telemetry", gateway_id="GW", device_id="D", from_time=now, to_time=now + timedelta(minutes=10))
        self.assertIn("clock_anomaly", result["items"][0]["reasons"])

    def test_inflight_pre_cutoff_insert_committed_between_pages_requires_refresh(self):
        now = self.scalar("SELECT clock_timestamp()")
        for index in range(2):
            self.sample(now - timedelta(seconds=10 + index))
        pending = self.connect("migrator")
        pending.execute("INSERT INTO public.ingest_messages(message_id,topic,gateway_id,device_id,boot_id,sequence_first,sequence_last,created_at,payload_sha256,raw_payload) "
                        "VALUES ('pending','fixture','GW','D','boot',1,1,%s,%s,'{}')", (now, "0" * 64))
        pending.execute("INSERT INTO public.telemetry_samples(message_id,sample_index,gateway_id,device_id,point_code,measured_at,sequence,value,quality,unit,source) "
                        "VALUES ('pending',0,'GW','D','load',%s,1,32.4,'good','kN','fixture')", (now - timedelta(seconds=1),))
        filters = dict(gateway_id="GW", device_id="D", from_time=now - timedelta(minutes=1), to_time=now + timedelta(minutes=1), page_size=1)
        first = self.read("resolved_telemetry", **filters)
        self.assertEqual((first["total"], first["query_total"]), (2, "2"))
        pending.commit()  # Real transaction began before the first request but became visible after it.
        with self.assertRaises(RegistryError) as caught:
            self.read("resolved_telemetry", **filters, page=2, revision=first["registry_revision"],
                      received_before=first["received_before"], query_total=first["query_total"])
        self.assertEqual((caught.exception.status, caught.exception.code), (409, "registry.query_changed"))
