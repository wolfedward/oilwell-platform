"""Read-only PostgreSQL telemetry query contracts for the production UI."""

from datetime import datetime, timedelta, timezone
import unittest

import backend.center.postgres as postgres


UTC = timezone.utc
NOW = datetime(2026, 9, 14, 4, 0, tzinfo=UTC)


class ScriptedCursor:
    def __init__(self, scripts, calls):
        self.scripts = scripts
        self.calls = calls
        self.rows = []

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def execute(self, sql, values=()):
        self.calls.append((" ".join(sql.split()), tuple(values)))
        if not self.scripts:
            raise AssertionError("unexpected query")
        self.rows = self.scripts.pop(0)
        return self

    def fetchone(self):
        return self.rows[0] if self.rows else None

    def fetchall(self):
        return list(self.rows)


class ScriptedConnection:
    def __init__(self, scripts, calls):
        self.scripts = scripts
        self.calls = calls

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def cursor(self):
        return ScriptedCursor(self.scripts, self.calls)

    def execute(self, _sql):
        # Session policy statements do not yield business rows.
        return None


def repository_for(*scripts):
    calls = []

    def connect_factory(_dsn, _kwargs):
        return ScriptedConnection(list(scripts), calls)

    repository_type = getattr(postgres, "PostgresTelemetryQueryRepository", None)
    if repository_type is None:
        return None, calls
    return repository_type(connect_kwargs={"host": "127.0.0.1"}, connect_factory=connect_factory), calls


class PostgresTelemetryQueryTests(unittest.TestCase):
    def test_connection_failures_are_redacted_as_repository_unavailable(self):
        repository_type = getattr(postgres, "PostgresTelemetryQueryRepository", None)
        self.assertIsNotNone(repository_type)

        def unavailable(_dsn, _kwargs):
            raise OSError("password=secret host=private.example")

        repository = repository_type(
            connect_kwargs={"host": "127.0.0.1"},
            connect_factory=unavailable,
        )
        try:
            repository.list_samples(
                from_time=NOW - timedelta(hours=1), to_time=NOW, page=1, page_size=20,
            )
        except Exception as error:
            caught = error
        else:
            self.fail("an unavailable database must fail closed")
        self.assertIsInstance(caught, postgres.PostgresRepositoryError)
        self.assertEqual(str(caught), "central PostgreSQL query failed")
        self.assertNotIn("secret", str(caught))

    def test_filtered_sample_page_is_json_safe_and_keeps_measurement_lineage(self):
        measured = NOW - timedelta(minutes=2)
        received = measured + timedelta(milliseconds=240)
        repository, calls = repository_for(
            [(2,)],
            [
                {
                    "message_id": "message-002",
                    "sample_index": 1,
                    "gateway_id": "GW-001",
                    "device_id": "D-001",
                    "point_code": "active_power_kw",
                    "measured_at": measured,
                    "sequence": 42,
                    "value": 6.125,
                    "quality": "good",
                    "unit": "kW",
                    "source": "modbus-tcp",
                    "received_at": received,
                },
            ],
        )
        self.assertIsNotNone(
            repository,
            "PostgresTelemetryQueryRepository must implement the production read path",
        )

        result = repository.list_samples(
            device_id="D-001",
            point_code="active_power_kw",
            from_time=NOW - timedelta(hours=1),
            to_time=NOW,
            page=2,
            page_size=20,
        )

        self.assertEqual((result["total"], result["page"], result["page_size"]), (2, 2, 20))
        self.assertEqual(
            result["items"][0],
            {
                "message_id": "message-002",
                "sample_index": 1,
                "gateway_id": "GW-001",
                "device_id": "D-001",
                "point_code": "active_power_kw",
                "measured_at": "2026-09-14T03:58:00+00:00",
                "sequence": "42",
                "value": 6.125,
                "quality": "good",
                "unit": "kW",
                "source": "modbus-tcp",
                "received_at": "2026-09-14T03:58:00.240000+00:00",
            },
        )
        self.assertEqual(len(calls), 2)
        for _sql, values in calls:
            self.assertIn("D-001", values)
            self.assertIn("active_power_kw", values)
            self.assertIn(NOW, values)
        self.assertEqual(calls[1][1][-2:], (20, 20))

    def test_health_snapshot_classifies_links_and_reports_quality_and_sequence_gaps(self):
        repository, _calls = repository_for(
            [
                {
                    "gateway_count": 1,
                    "device_count": 3,
                    "point_count": 8,
                    "samples_24h": 1440,
                    "bad_samples_24h": 5,
                    "uncertain_samples_24h": 7,
                    "p95_delivery_ms": 382.4,
                }
            ],
            [
                {"gap_count": 2, "missing_sequences": 4},
            ],
            [
                {
                    "gateway_id": "GW-001", "device_id": "D-ONLINE", "boot_id": "boot-a",
                    "last_sequence": 81, "last_measured_at": NOW - timedelta(seconds=15),
                    "last_received_at": NOW - timedelta(seconds=10), "sample_count_24h": 480,
                    "bad_sample_count_24h": 0,
                },
                {
                    "gateway_id": "GW-001", "device_id": "D-DELAYED", "boot_id": "boot-b",
                    "last_sequence": 51, "last_measured_at": NOW - timedelta(seconds=160),
                    "last_received_at": NOW - timedelta(seconds=150), "sample_count_24h": 470,
                    "bad_sample_count_24h": 2,
                },
                {
                    "gateway_id": "GW-001", "device_id": "D-OFFLINE", "boot_id": "boot-c",
                    "last_sequence": 21, "last_measured_at": NOW - timedelta(seconds=650),
                    "last_received_at": NOW - timedelta(seconds=640), "sample_count_24h": 460,
                    "bad_sample_count_24h": 3,
                },
            ],
        )
        self.assertIsNotNone(repository)

        result = repository.health(now=NOW, stale_after_seconds=120)

        self.assertEqual(result["status"], "available")
        self.assertEqual(result["summary"]["online_devices"], 1)
        self.assertEqual(result["summary"]["delayed_devices"], 1)
        self.assertEqual(result["summary"]["offline_devices"], 1)
        self.assertEqual(result["summary"]["sequence_gap_count_24h"], 2)
        self.assertEqual(result["summary"]["missing_sequences_24h"], "4")
        self.assertEqual(result["summary"]["p95_delivery_ms"], 382.4)
        self.assertEqual([item["status"] for item in result["links"]], ["online", "delayed", "offline"])
        self.assertEqual(result["links"][0]["last_received_at"], "2026-09-14T03:59:50+00:00")


if __name__ == "__main__":
    unittest.main()
