"""Reliable live-frame ingestion, quality-event, and replay tests."""

from datetime import datetime, timedelta, timezone
import http.client
import json
from pathlib import Path
import sqlite3
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor

from backend.database import Database
from backend.protocol import decode_frame, encode_frame
from backend.seed import seed_database
from backend.services.acquisition import AcquisitionService, AcquisitionWorker
from backend.simulator import SimulatorRegistry
from backend.http_server import create_server


SHANGHAI = timezone(timedelta(hours=8))
NOW = datetime(2026, 6, 22, 10, 0, tzinfo=SHANGHAI)


def telemetry_payload(**overrides):
    telemetry = {
        "current": 20.1,
        "voltage": 380.2,
        "active_power": 5.4,
        "power_factor": 0.89,
        "frequency": 50.01,
        "stroke": 2.7,
        "strokes_per_minute": 4.2,
        "fillage": 0.82,
    }
    telemetry.update(overrides)
    return {
        "kind": "telemetry",
        "status": "online",
        "telemetry": telemetry,
        "connection_mode": "virtual",
        "hardware_connected": False,
    }


class AcquisitionMigrationTests(unittest.TestCase):
    def test_pre_acquisition_tables_gain_columns_before_live_indexes_are_created(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "legacy.db"
            connection = sqlite3.connect(path)
            connection.executescript("""
                CREATE TABLE raw_messages (
                    id INTEGER PRIMARY KEY, device_id TEXT, received_at TEXT NOT NULL,
                    payload TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'received', actor_id INTEGER,
                    source_type TEXT NOT NULL DEFAULT 'device', created_at TEXT NOT NULL, updated_at TEXT NOT NULL
                );
                CREATE TABLE telemetry (
                    id INTEGER PRIMARY KEY, well_id TEXT NOT NULL, device_id TEXT, channel_id TEXT,
                    measured_at TEXT NOT NULL, metric_code TEXT NOT NULL, value REAL NOT NULL, unit TEXT,
                    status TEXT NOT NULL DEFAULT 'valid', actor_id INTEGER, source_type TEXT NOT NULL DEFAULT 'device',
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL
                );
                CREATE TABLE data_quality_events (
                    id INTEGER PRIMARY KEY, telemetry_id INTEGER, well_id TEXT, event_type TEXT NOT NULL,
                    detail TEXT, status TEXT NOT NULL DEFAULT 'open', actor_id INTEGER,
                    source_type TEXT NOT NULL DEFAULT 'system', created_at TEXT NOT NULL, updated_at TEXT NOT NULL
                );
            """)
            connection.close()
            database = Database(path)
            database.migrate()
            self.assertIn("sequence_text", {row[1] for row in database.query_all("PRAGMA table_info(raw_messages)")})
            self.assertIn("kind", {row[1] for row in database.query_all("PRAGMA table_info(data_quality_events)")})
            self.assertIn("raw_message_id", {row[1] for row in database.query_all("PRAGMA table_info(telemetry)")})
            self.assertIn("raw_message_id", {row[1] for row in database.query_all("PRAGMA table_info(device_heartbeats)")})
            indexes = set(database.column("SELECT name FROM sqlite_master WHERE type='index'"))
            self.assertIn("raw_messages_device_sequence_idx", indexes)


class AcquisitionServiceTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.database = Database(Path(self.temporary.name) / "platform.db")
        self.database.migrate()
        seed_database(self.database)
        self.registry = SimulatorRegistry(self.database, clock=lambda: NOW)
        self.service = AcquisitionService(self.database, self.registry, clock=lambda: NOW)

    def frame(self, sequence, payload=None, *, device_id="D-001", timestamp=NOW):
        return encode_frame(device_id, sequence, payload or telemetry_payload(), timestamp=timestamp)

    def test_valid_telemetry_retains_exact_raw_frame_and_lineage(self):
        raw = self.frame(20)
        result = self.service.ingest(raw)

        self.assertTrue(result.accepted)
        message = self.database.query_one(
            "SELECT * FROM raw_messages WHERE id=?", (result.raw_message_id,)
        )
        self.assertEqual(bytes(message["raw_frame"]), raw)
        self.assertEqual(
            (message["device_id"], message["sequence_text"], message["frame_kind"], message["status"], message["source_type"]),
            ("D-001", "20", "telemetry", "accepted", "virtual_device"),
        )
        rows = self.database.query_all(
            "SELECT metric_code, value, unit, device_id, well_id, raw_message_id, source_type FROM telemetry WHERE raw_message_id=? ORDER BY metric_code",
            (result.raw_message_id,),
        )
        self.assertEqual(len(rows), 8)
        self.assertTrue(all(row["device_id"] == "D-001" and row["well_id"] == "W-001" for row in rows))
        self.assertTrue(all(row["source_type"] == "virtual_device" for row in rows))

    def test_sequence_gap_accepts_current_frame_and_records_exact_missing_range(self):
        self.assertTrue(self.service.ingest(self.frame(20)).accepted)
        result = self.service.ingest(self.frame(22))

        self.assertTrue(result.accepted)
        event = self.database.query_one(
            "SELECT * FROM data_quality_events WHERE device_id='D-001' AND kind='sequence_gap' ORDER BY id DESC LIMIT 1"
        )
        self.assertEqual((event["missing_from"], event["missing_to"], event["status"]), (21, 21, "open"))

    def test_bad_checksum_is_retained_and_rejected_without_business_rows(self):
        raw = bytearray(self.frame(20))
        raw[-1] ^= 0x01
        result = self.service.ingest(bytes(raw))

        self.assertFalse(result.accepted)
        self.assertEqual(result.error_code, "checksum_error")
        message = self.database.query_one("SELECT * FROM raw_messages WHERE id=?", (result.raw_message_id,))
        self.assertEqual(bytes(message["raw_frame"]), bytes(raw))
        self.assertEqual((message["status"], message["rejection_code"]), ("rejected", "checksum_error"))
        self.assertEqual(self.database.scalar("SELECT COUNT(*) FROM telemetry WHERE raw_message_id=?", (result.raw_message_id,)), 0)

    def test_duplicate_and_concurrent_duplicate_have_one_business_winner_but_keep_attempts(self):
        raw = self.frame(30)
        with ThreadPoolExecutor(max_workers=4) as workers:
            results = list(workers.map(self.service.ingest, [raw] * 4))
        self.assertEqual(sum(result.accepted for result in results), 1)
        self.assertEqual(self.database.scalar("SELECT COUNT(*) FROM raw_messages WHERE sequence_text='30' AND source_type='virtual_device'"), 4)
        self.assertEqual(self.database.scalar("SELECT COUNT(*) FROM telemetry WHERE raw_message_id IN (SELECT id FROM raw_messages WHERE sequence_text='30')"), 8)
        self.assertEqual(self.database.scalar("SELECT COUNT(*) FROM data_quality_events WHERE kind='duplicate'"), 3)

    def test_database_serializes_duplicates_across_independent_service_instances(self):
        raw = self.frame(31)
        services = [
            AcquisitionService(self.database, self.registry, clock=lambda: NOW)
            for _ in range(4)
        ]
        with ThreadPoolExecutor(max_workers=4) as workers:
            futures = [workers.submit(service.ingest, raw) for service in services]
            results = [future.result() for future in futures]

        self.assertEqual(sum(result.accepted for result in results), 1)
        self.assertEqual(
            self.database.scalar(
                "SELECT COUNT(*) FROM raw_messages WHERE sequence_text='31' AND source_type='virtual_device'"
            ),
            4,
        )
        self.assertEqual(
            self.database.scalar(
                "SELECT COUNT(*) FROM telemetry WHERE raw_message_id IN "
                "(SELECT id FROM raw_messages WHERE sequence_text='31' AND source_type='virtual_device')"
            ),
            8,
        )

    def test_late_missing_sequence_repairs_gap_without_moving_cursor_backwards(self):
        self.service.ingest(self.frame(20))
        self.service.ingest(self.frame(22))
        late = self.service.ingest(self.frame(21))
        event = self.database.query_one("SELECT * FROM data_quality_events WHERE kind='sequence_gap'")
        self.assertTrue(late.accepted)
        self.assertEqual((event["status"], event["missing_sequences"]), ("repaired", "[]"))
        self.assertEqual(self.database.scalar("SELECT last_sequence FROM acquisition_cursors WHERE device_id='D-001'"), 22)

    def test_non_gap_sequence_behind_cursor_is_rejected_as_stale(self):
        self.service.ingest(self.frame(20))
        result = self.service.ingest(self.frame(19))

        self.assertEqual((result.accepted, result.status, result.error_code), (False, "rejected", "stale_sequence"))
        raw = self.database.query_one("SELECT status,rejection_code FROM raw_messages WHERE id=?", (result.raw_message_id,))
        self.assertEqual((raw["status"], raw["rejection_code"]), ("rejected", "stale_sequence"))
        self.assertEqual(self.database.scalar("SELECT COUNT(*) FROM telemetry WHERE raw_message_id=?", (result.raw_message_id,)), 0)
        self.assertEqual(self.database.scalar("SELECT COUNT(*) FROM data_quality_events WHERE raw_message_id=? AND kind='stale_sequence'", (result.raw_message_id,)), 1)

    def test_unknown_device_u64_overflow_and_outlier_are_typed_rejections(self):
        cases = (
            (self.frame(40, device_id="D-UNKNOWN"), "unknown_device"),
            (self.frame(2**63), "sequence_out_of_range"),
            (self.frame(41, telemetry_payload(voltage=900.0)), "range_outlier"),
        )
        for raw, code in cases:
            with self.subTest(code=code):
                result = self.service.ingest(raw)
                self.assertFalse(result.accepted)
                self.assertEqual(result.error_code, code)
                self.assertEqual(bytes(self.database.query_one("SELECT raw_frame FROM raw_messages WHERE id=?", (result.raw_message_id,))[0]), raw)

    def test_future_and_old_timestamp_boundaries_are_rejected_while_delay_is_accepted_and_flagged(self):
        future = self.service.ingest(self.frame(50, timestamp=NOW + timedelta(minutes=5, seconds=1)))
        old = self.service.ingest(self.frame(51, timestamp=NOW - timedelta(days=7, seconds=1)))
        delayed = self.service.ingest(self.frame(52, timestamp=NOW - timedelta(minutes=16)))
        self.assertEqual((future.error_code, old.error_code), ("timestamp_future", "timestamp_out_of_range"))
        self.assertTrue(delayed.accepted)
        self.assertEqual(self.database.scalar("SELECT COUNT(*) FROM data_quality_events WHERE raw_message_id=? AND kind='delay'", (delayed.raw_message_id,)), 1)

    def test_terminal_packet_loss_history_replays_missing_frame_idempotently(self):
        terminal = self.registry.get("D-001")
        terminal.apply_scenario("packet_loss", 30, now=NOW)
        emitted = terminal.tick(NOW + timedelta(minutes=1))
        sequences = [self.service.ingest(raw).sequence for raw in emitted]
        self.assertEqual(sequences, [1, 3])
        gap = self.database.query_one("SELECT id,version FROM data_quality_events WHERE kind='sequence_gap' AND status='open'")
        self.assertIsNotNone(gap)

        replay = self.service.replay("D-001", 2, 2)
        again = self.service.replay("D-001", 2, 2)
        self.assertEqual((replay.restored, replay.unavailable), (1, ()))
        self.assertEqual((again.restored, again.status), (0, "already_repaired"))
        self.assertEqual(self.database.scalar("SELECT COUNT(*) FROM raw_messages WHERE device_id='D-001' AND sequence_text='2'"), 1)
        self.assertEqual(self.database.scalar("SELECT status FROM data_quality_events WHERE id=?", (gap["id"],)), "repaired")

    def test_replay_across_two_gaps_links_each_raw_to_its_matching_event(self):
        terminal = self.registry.get("D-001")
        archived = [*terminal.tick(NOW), *terminal.tick(NOW + timedelta(minutes=1))]
        raw_by_sequence = {decode_frame(raw).sequence: raw for raw in archived}
        for sequence in (1, 3, 4, 6):
            self.service.ingest(raw_by_sequence[sequence])
        gaps = self.database.query_all("SELECT id,missing_from FROM data_quality_events WHERE kind='sequence_gap' ORDER BY id")
        self.assertEqual([row["missing_from"] for row in gaps], [2, 5])

        replay = self.service.replay("D-001", 2, 5)
        self.assertEqual((replay.restored, replay.status), (2, "repaired"))
        links = self.database.query_all(
            "SELECT sequence_number,replay_event_id FROM raw_messages WHERE status='replayed' ORDER BY sequence_number"
        )
        self.assertEqual(
            [(row["sequence_number"], row["replay_event_id"]) for row in links],
            [(2, gaps[0]["id"]), (5, gaps[1]["id"])],
        )

    def test_replay_reports_unavailable_terminal_history_and_leaves_gap_open(self):
        self.service.ingest(self.frame(60))
        self.service.ingest(self.frame(62))
        result = self.service.replay("D-001", 61, 61)
        self.assertEqual((result.restored, result.unavailable, result.status), (0, (61,), "incomplete"))
        self.assertEqual(self.database.scalar("SELECT status FROM data_quality_events WHERE missing_from=61 AND missing_to=61"), "open")

    def test_heartbeat_and_electric_card_have_strict_accepted_persistence(self):
        terminal = self.registry.get("D-001")
        results = [self.service.ingest(raw) for raw in terminal.tick(NOW)]
        self.assertEqual([result.kind for result in results], ["heartbeat", "telemetry", "electric_card"])
        self.assertTrue(all(result.accepted for result in results))
        self.assertEqual(self.database.scalar("SELECT COUNT(*) FROM device_heartbeats WHERE source_type='virtual_device'"), 1)
        heartbeat = self.database.query_one("SELECT raw_message_id FROM device_heartbeats WHERE source_type='virtual_device'")
        self.assertIsNotNone(heartbeat["raw_message_id"])
        self.assertEqual(
            self.database.scalar("SELECT frame_kind FROM raw_messages WHERE id=?", (heartbeat["raw_message_id"],)),
            "heartbeat",
        )
        electric = self.database.query_one("SELECT * FROM raw_messages WHERE frame_kind='electric_card' AND source_type='virtual_device'")
        self.assertIsNotNone(electric["raw_frame"])
        self.assertEqual(self.database.scalar("SELECT COUNT(*) FROM telemetry WHERE raw_message_id=?", (electric["id"],)), 0)

    def test_unknown_kind_and_partial_gap_are_bounded_quality_results(self):
        unknown = encode_frame("D-001", 80, {"kind": "bogus"}, timestamp=NOW)
        rejected = self.service.ingest(unknown)
        self.assertEqual((rejected.accepted, rejected.error_code), (False, "schema_error"))
        self.service.ingest(self.frame(90))
        self.service.ingest(self.frame(94))
        self.service.ingest(self.frame(92))
        event = self.database.query_one("SELECT missing_from,missing_to,missing_sequences,status FROM data_quality_events WHERE kind='sequence_gap' AND missing_from=91")
        self.assertEqual((event["missing_from"], event["missing_to"], json.loads(event["missing_sequences"]), event["status"]), (91, 93, [91, 93], "open"))

    def test_audit_failure_rolls_back_raw_cursor_quality_and_telemetry_together(self):
        with self.database.transaction(None, "test.fail_acquisition_audit", "audit", None):
            self.database.execute("""CREATE TRIGGER fail_acquisition_audit BEFORE INSERT ON audit_logs
                WHEN NEW.action='acquisition.ingest' BEGIN SELECT RAISE(ABORT, 'audit unavailable'); END""")
        self.addCleanup(lambda: self.database.execute("DROP TRIGGER IF EXISTS fail_acquisition_audit"))
        before = {
            "raw": self.database.scalar("SELECT COUNT(*) FROM raw_messages"),
            "telemetry": self.database.scalar("SELECT COUNT(*) FROM telemetry"),
            "quality": self.database.scalar("SELECT COUNT(*) FROM data_quality_events"),
        }
        with self.assertRaises(sqlite3.IntegrityError):
            self.service.ingest(self.frame(100))
        self.assertEqual(self.database.scalar("SELECT COUNT(*) FROM raw_messages"), before["raw"])
        self.assertEqual(self.database.scalar("SELECT COUNT(*) FROM telemetry"), before["telemetry"])
        self.assertEqual(self.database.scalar("SELECT COUNT(*) FROM data_quality_events"), before["quality"])
        self.assertIsNone(self.database.query_one("SELECT 1 FROM acquisition_cursors WHERE device_id='D-001'"))

    def test_operational_rows_survive_migration_and_seed_validation_on_restart(self):
        accepted = self.service.ingest(self.frame(110))
        seed_database(self.database)
        self.database.migrate()
        seed_database(self.database)
        row = self.database.query_one("SELECT status,source_type FROM raw_messages WHERE id=?", (accepted.raw_message_id,))
        self.assertEqual((row["status"], row["source_type"]), ("accepted", "virtual_device"))
        self.assertEqual(self.database.scalar("SELECT COUNT(*) FROM telemetry WHERE raw_message_id=?", (accepted.raw_message_id,)), 8)
        restarted = SimulatorRegistry(self.database, clock=lambda: NOW + timedelta(minutes=1))
        self.assertEqual(restarted.get("D-001").sequence, 110)
        next_frames = restarted.get("D-001").tick(NOW + timedelta(minutes=1))
        self.assertEqual([self.service.ingest(raw).sequence for raw in next_frames], [111, 112, 113])

    def test_repair_request_audit_failure_does_not_replay_or_close_gap(self):
        terminal = self.registry.get("D-001")
        terminal.apply_scenario("packet_loss", 30, now=NOW)
        for raw in terminal.tick(NOW + timedelta(minutes=1)):
            self.service.ingest(raw)
        event = self.database.query_one("SELECT id,version FROM data_quality_events WHERE kind='sequence_gap' AND status='open'")
        with self.database.transaction(None, "test.fail_repair_audit", "audit", None):
            self.database.execute("""CREATE TRIGGER fail_repair_audit BEFORE INSERT ON audit_logs
                WHEN NEW.action='data_quality.repair.request' BEGIN SELECT RAISE(ABORT, 'repair audit unavailable'); END""")
        self.addCleanup(lambda: self.database.execute("DROP TRIGGER IF EXISTS fail_repair_audit"))
        actor = self.database.scalar("SELECT id FROM users WHERE username='administrator'")
        with self.assertRaises(sqlite3.IntegrityError):
            self.service.repair_event(actor, event["id"], event["version"])
        self.assertEqual(self.database.scalar("SELECT status FROM data_quality_events WHERE id=?", (event["id"],)), "open")
        self.assertEqual(self.database.scalar("SELECT COUNT(*) FROM raw_messages WHERE source_type='virtual_device' AND sequence_text='2'"), 0)

    def test_legacy_quality_kind_filter_uses_event_type_fallback(self):
        result = self.service.list_quality({
            "kind": "completeness_check", "status": "", "device_id": "", "page": 1, "page_size": 100,
        })
        self.assertEqual(result["total"], 48)
        self.assertTrue(all(item["kind"] == "completeness_check" for item in result["items"]))


class AcquisitionWorkerTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.database = Database(Path(self.temporary.name) / "platform.db")
        self.database.migrate(); seed_database(self.database)
        self.registry = SimulatorRegistry(self.database, cadence_seconds=.05, clock=lambda: NOW)
        self.service = AcquisitionService(self.database, self.registry, clock=lambda: NOW)

    def test_worker_automatically_drains_terminal_mailbox_and_stops_cleanly(self):
        worker = AcquisitionWorker(self.service, self.registry, mailbox_size=2, poll_seconds=.01)
        worker.start(); self.registry.start()
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and self.database.scalar("SELECT COUNT(*) FROM raw_messages WHERE source_type='virtual_device'") < 144:
            time.sleep(.02)
        self.registry.stop(); worker.stop()
        self.assertEqual(self.database.scalar("SELECT COUNT(*) FROM raw_messages WHERE source_type='virtual_device'"), 144)
        self.assertFalse(worker.is_alive())
        self.assertFalse(self.registry._thread.is_alive())
        self.assertEqual(worker.stats()["failed"], 0)

    def test_worker_records_one_failure_and_continues_the_remaining_batch(self):
        original = self.service.ingest
        calls = {"count": 0}
        def fail_once(raw):
            calls["count"] += 1
            if calls["count"] == 1:
                raise RuntimeError("temporary ingest failure")
            return original(raw)
        self.service.ingest = fail_once
        worker = AcquisitionWorker(self.service, self.registry, mailbox_size=2, poll_seconds=.01)
        worker.start(); self.registry.run_once(NOW)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and worker.stats()["accepted"] < 143:
            time.sleep(.02)
        worker.stop()
        self.assertEqual(worker.stats()["failed"], 1)
        self.assertEqual(worker.stats()["accepted"], 143)
        self.assertEqual(self.database.scalar("SELECT COUNT(*) FROM system_events WHERE event_type='acquisition_worker_error'"), 1)
        self.assertFalse(worker.is_alive())

    def test_worker_stop_interrupts_remaining_slow_batch_and_records_delivery_failure(self):
        original = self.service.ingest
        started = threading.Event()
        def slow(raw):
            started.set(); time.sleep(.1); return original(raw)
        self.service.ingest = slow
        worker = AcquisitionWorker(self.service, self.registry, mailbox_size=2, poll_seconds=.01)
        raw = encode_frame("D-001", 500, telemetry_payload(), timestamp=NOW)
        worker.start(); worker.mailbox.put(tuple(raw for _ in range(20)))
        self.assertTrue(started.wait(1))
        before = time.monotonic(); worker.stop(); elapsed = time.monotonic() - before
        self.assertLess(elapsed, 1.0)
        self.assertFalse(worker.is_alive())
        self.assertGreaterEqual(
            self.database.scalar("SELECT COUNT(*) FROM data_quality_events WHERE kind='delivery_failure'"), 1
        )

    def test_fallback_overflow_creates_queryable_delivery_failure_and_mailbox_stats(self):
        registry = SimulatorRegistry(self.database, cadence_seconds=.05, queue_size=1, clock=lambda: NOW)
        service = AcquisitionService(self.database, registry, clock=lambda: NOW + timedelta(minutes=1))
        registry.run_once(NOW)
        registry.run_once(NOW + timedelta(minutes=1))
        self.assertGreater(registry.frame_stats()["dropped"], 0)
        worker = AcquisitionWorker(service, registry, mailbox_size=2, poll_seconds=.01)
        worker.start()
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline and self.database.scalar("SELECT COUNT(*) FROM data_quality_events WHERE kind='delivery_failure'") == 0:
            time.sleep(.02)
        stats = worker.stats()
        worker.stop()
        event = self.database.query_one("SELECT detail FROM data_quality_events WHERE kind='delivery_failure' ORDER BY id DESC")
        self.assertIsNotNone(event)
        detail = json.loads(event["detail"])
        self.assertGreater(detail["dropped_frames"], 0)
        self.assertIn("devices", detail)
        self.assertEqual(stats["mailbox_capacity"], 2)
        self.assertIn("mailbox_queued", stats)


class AcquisitionHttpTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.server = create_server("127.0.0.1", 0, database_path=Path(self.temporary.name) / "platform.db")
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self._close)
        self.admin = self._login("administrator", "Admin@2026")
        self.reviewer = self._login("reviewer", "Review@2026")

    def _close(self):
        self.server.shutdown(); self.thread.join(timeout=5); self.server.server_close()

    def _request(self, method, path, token=None, body=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=3)
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        encoded = None
        if body is not None:
            encoded = json.dumps(body).encode(); headers["Content-Type"] = "application/json"
        connection.request(method, path, body=encoded, headers=headers)
        response = connection.getresponse(); payload = json.loads(response.read().decode()); connection.close()
        return response.status, payload

    def _login(self, username, password):
        status, payload = self._request("POST", "/api/auth/login", body={"username": username, "password": password})
        self.assertEqual(status, 200)
        return payload["data"]["token"]

    def test_live_worker_and_authenticated_read_routes_expose_cursor_data(self):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and self.server.database.scalar("SELECT COUNT(*) FROM raw_messages WHERE source_type='virtual_device'") == 0:
            time.sleep(.02)
        for path in ("/api/acquisition/stream?after_id=0&limit=5", "/api/acquisition/messages?page=1&page_size=5", "/api/data-quality?page=1&page_size=5"):
            status, payload = self._request("GET", path, self.admin)
            self.assertEqual(status, 200, (path, payload))
            self.assertTrue(payload["ok"])
        _, messages = self._request("GET", "/api/acquisition/messages?status=accepted&page_size=1", self.admin)
        message_id = messages["data"]["items"][0]["id"]
        status, detail = self._request("GET", f"/api/acquisition/messages/{message_id}", self.admin)
        self.assertEqual(status, 200)
        self.assertTrue(detail["data"]["raw_frame_base64"])
        status, _ = self._request("GET", "/api/acquisition/messages?unexpected=1", self.admin)
        self.assertEqual(status, 422)
        status, _ = self._request("GET", f"/api/acquisition/messages/{message_id}?unexpected=1", self.admin)
        self.assertEqual(status, 422)

    def test_reviewer_can_read_but_cannot_repair_and_admin_body_is_exact(self):
        now = self.server.acquisition.now()
        self.server.acquisition.ingest(encode_frame("D-001", 200, telemetry_payload(), timestamp=now))
        self.server.acquisition.ingest(encode_frame("D-001", 202, telemetry_payload(), timestamp=now))
        event = self.server.database.query_one("SELECT id,version FROM data_quality_events WHERE device_id='D-001' AND kind='sequence_gap' AND missing_from=201")
        path = f"/api/data-quality/{event['id']}/repair"
        self.assertEqual(self._request("POST", path, self.reviewer, {"version": event["version"]})[0], 403)
        self.assertEqual(self._request("POST", path, self.admin, {"version": event["version"], "extra": True})[0], 422)
        self.assertEqual(self._request("POST", path, self.admin, {"version": event["version"] + 1})[0], 409)
        self.assertEqual(self._request("POST", path + "?unexpected=1", self.admin, {"version": event["version"]})[0], 422)
        status, payload = self._request("POST", path, self.admin, {"version": event["version"]})
        self.assertEqual(status, 200)
        self.assertEqual(payload["data"]["status"], "incomplete")


if __name__ == "__main__":
    unittest.main()
