"""Authenticated HTTP contracts for the central PostgreSQL read path."""

from datetime import datetime, timezone
from http import HTTPStatus
from types import SimpleNamespace
import http.client
import json
from pathlib import Path
import os
import tempfile
import threading
import unittest
from unittest.mock import patch

import backend.api as api_module
from backend.center.postgres import PostgresRepositoryError
from backend.http_server import create_server


class FakeCentralRepository:
    def __init__(self, *, failure=None):
        self.failure = failure
        self.sample_calls = []
        self.health_calls = []

    def list_samples(self, **values):
        self.sample_calls.append(values)
        if self.failure:
            raise self.failure
        return {"items": [], "total": 0, "page": values["page"], "page_size": values["page_size"]}

    def health(self, **_values):
        self.health_calls.append(_values)
        if self.failure:
            raise self.failure
        return {"status": "available", "summary": {"samples_24h": 12}, "links": []}


class FakeHandler:
    def __init__(self, repository, allowed=True):
        self.server = SimpleNamespace(central_telemetry=repository)
        self.allowed = allowed
        self.required = []
        self.responses = []

    def _require(self, permission):
        self.required.append(permission)
        return SimpleNamespace(id=1) if self.allowed else None

    def _send_json(self, status, payload):
        self.responses.append((status, payload))

    @staticmethod
    def _error(code, message):
        return {"ok": False, "error": {"code": code, "message": message}}


def api_for(handler):
    api_type = getattr(api_module, "CentralTelemetryApi", None)
    return api_type(handler) if api_type else None


class CentralTelemetryApiTests(unittest.TestCase):
    def test_resolved_window_can_be_reused_without_moving_receipt_cutoff(self):
        repository = FakeCentralRepository()
        handler = FakeHandler(repository)
        handler.server.central_clock = lambda: datetime(2026, 9, 23, 4, tzinfo=timezone.utc)
        api_for(handler).get("/api/central/telemetry")
        window = handler.responses[-1][1]["data"].get("window")
        self.assertEqual(window, {"from":"2026-09-22T04:00:00+00:00",
                                 "to":"2026-09-23T04:00:00+00:00",
                                 "received_before":"2026-09-23T04:00:00+00:00"})
        handler.server.central_clock = lambda: datetime(2026, 9, 23, 5, tzinfo=timezone.utc)
        from urllib.parse import urlencode
        api_for(handler).get("/api/central/telemetry?"+urlencode(window | {"page":2}))
        self.assertEqual(repository.sample_calls[-1]["received_before"], datetime(2026, 9, 23, 4, tzinfo=timezone.utc))

    def test_each_route_enforces_its_permission_before_repository_access(self):
        for route, permission in (
            ("health", "acquisition.read"),
            ("quality", "data_quality.read"),
            ("telemetry", "telemetry.read"),
        ):
            with self.subTest(route=route):
                repository = FakeCentralRepository()
                handler = FakeHandler(repository, allowed=False)
                self.assertTrue(api_for(handler).get(f"/api/central/{route}"))
                self.assertEqual(handler.required, [permission])
                self.assertEqual(repository.sample_calls, [])
                self.assertEqual(repository.health_calls, [])
                self.assertEqual(handler.responses, [])

    def test_gateway_and_quality_filters_keep_wire_identity_and_empty_fields_are_optional(self):
        repository = FakeCentralRepository()
        handler = FakeHandler(repository)
        api_for(handler).get(
            "/api/central/telemetry?gateway_id=GW.Site:01&device_id="
            "&point_code=Power.A&quality=uncertain"
        )
        self.assertEqual(handler.responses[0][0], HTTPStatus.OK)
        values = repository.sample_calls[0]
        self.assertEqual(values["gateway_id"], "GW.Site:01")
        self.assertIsNone(values["device_id"])
        self.assertEqual(values["point_code"], "Power.A")
        self.assertEqual(values["quality"], "uncertain")
        self.assertEqual((values["to_time"]-values["from_time"]).total_seconds(), 86400)

    def test_filtered_query_parses_utc_window_and_uses_telemetry_permission(self):
        repository = FakeCentralRepository()
        handler = FakeHandler(repository)
        api = api_for(handler)
        self.assertIsNotNone(api, "CentralTelemetryApi must expose the production read route")

        matched = api.get(
            "/api/central/telemetry?device_id=D-001&point_code=active_power_kw"
            "&from=2026-09-14T03%3A00%3A00Z&to=2026-09-14T04%3A00%3A00Z"
            "&page=2&page_size=20"
        )

        self.assertTrue(matched)
        self.assertEqual(handler.required, ["telemetry.read"])
        self.assertEqual(handler.responses[0][0], HTTPStatus.OK)
        call = repository.sample_calls[0]
        self.assertEqual(call["device_id"], "D-001")
        self.assertEqual(call["point_code"], "active_power_kw")
        self.assertEqual(call["from_time"], datetime(2026, 9, 14, 3, 0, tzinfo=timezone.utc))
        self.assertEqual(call["to_time"], datetime(2026, 9, 14, 4, 0, tzinfo=timezone.utc))
        self.assertEqual((call["page"], call["page_size"]), (2, 20))

    def test_invalid_or_overlong_window_is_rejected_before_database_access(self):
        for query in (
            "device_id=bad%2Fdevice",
            "point_code=Power%20KW",
            "from=2026-01-01T00%3A00%3A00Z&to=2026-09-14T00%3A00%3A00Z",
            "unknown=value",
            "to=0001-01-01T00:00:00%2B14:00",
            "to=9999-12-31T23:59:59-14:00",
            "to=0001-01-01T00:00:00Z",
            "from=2026-09-23T12:00:00",
            "page=1&page=2",
            "page_size=201",
            "page=0",
            "quality=invalid",
        ):
            with self.subTest(query=query):
                repository = FakeCentralRepository()
                handler = FakeHandler(repository)
                api = api_for(handler)
                self.assertIsNotNone(api)

                self.assertTrue(api.get(f"/api/central/telemetry?{query}"))

                self.assertEqual(handler.responses[0][0], HTTPStatus.UNPROCESSABLE_ENTITY)
                self.assertEqual(handler.responses[0][1]["error"]["code"], "validation_error")
                self.assertEqual(repository.sample_calls, [])

    def test_disabled_and_failed_center_are_explicit_without_leaking_connection_details(self):
        disabled_handler = FakeHandler(None)
        disabled_api = api_for(disabled_handler)
        self.assertIsNotNone(disabled_api)
        self.assertTrue(disabled_api.get("/api/central/health"))
        self.assertEqual(disabled_handler.required, ["acquisition.read"])
        self.assertEqual(
            disabled_handler.responses[0],
            (HTTPStatus.OK, {"ok": True, "data": {"status": "not_configured", "summary": {}, "links": []}}),
        )

        failure = PostgresRepositoryError("password=secret host=private.example")
        failed_handler = FakeHandler(FakeCentralRepository(failure=failure))
        failed_api = api_for(failed_handler)
        self.assertTrue(failed_api.get("/api/central/health"))
        status, payload = failed_handler.responses[0]
        self.assertEqual(status, HTTPStatus.OK)
        self.assertEqual(payload["data"]["status"], "unavailable")
        self.assertNotIn("secret", str(payload))
        self.assertNotIn("private.example", str(payload))

        query_handler = FakeHandler(FakeCentralRepository(failure=failure))
        query_api = api_for(query_handler)
        self.assertTrue(query_api.get("/api/central/telemetry"))
        status, payload = query_handler.responses[0]
        self.assertEqual(status, HTTPStatus.SERVICE_UNAVAILABLE)
        self.assertEqual(payload["error"]["code"], "central_unavailable")
        self.assertNotIn("secret", str(payload))


class CentralTelemetryHttpIntegrationTests(unittest.TestCase):
    def test_server_builds_query_repository_from_external_center_config(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config_path = root / "center.local.toml"
            config_path.write_text(
                """[center]
topic_prefix = "oilfield"
[mqtt]
host = "127.0.0.1"
port = 18884
client_id = "platform-ingestor"
username = "center"
password_env = "TEST_CENTER_MQTT_PASSWORD"
session_expiry_seconds = 604800
[postgres]
host = "127.0.0.1"
port = 5432
database = "oilwell_platform"
user = "oilwell_reader"
password_env = "TEST_CENTER_POSTGRES_PASSWORD"
sslmode = "prefer"
""",
                encoding="utf-8",
            )
            with patch.dict(
                os.environ,
                {
                    "TEST_CENTER_MQTT_PASSWORD": "mqtt-external-secret",
                    "TEST_CENTER_POSTGRES_PASSWORD": "postgres-external-secret",
                },
            ):
                try:
                    server = create_server(
                        "127.0.0.1",
                        0,
                        database_path=root / "platform.db",
                        center_config_path=config_path,
                    )
                except TypeError as error:
                    self.fail(f"create_server must accept an external center config: {error}")
            try:
                self.assertEqual(server.central_telemetry.connect_kwargs["user"], "oilwell_reader")
                self.assertEqual(server.central_telemetry.connect_kwargs["password"], "postgres-external-secret")
                self.assertNotIn("postgres-external-secret", repr(server.central_telemetry))
            finally:
                server.server_close()

    def test_real_server_routes_use_the_injected_central_repository(self):
        repository = FakeCentralRepository()
        with tempfile.TemporaryDirectory() as temporary:
            try:
                server = create_server(
                    "127.0.0.1",
                    0,
                    database_path=Path(temporary) / "platform.db",
                    central_repository=repository,
                )
            except TypeError as error:
                self.fail(f"create_server must accept the production repository: {error}")
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                for route in ("health", "quality", "telemetry"):
                    status, payload = self._request(server, "GET", f"/api/central/{route}")
                    self.assertEqual(status, HTTPStatus.UNAUTHORIZED)
                self.assertEqual(repository.sample_calls, [])
                self.assertEqual(repository.health_calls, [])
                login_status, login = self._request(
                    server,
                    "POST",
                    "/api/auth/login",
                    body={"username": "administrator", "password": "Admin@2026"},
                )
                self.assertEqual(login_status, HTTPStatus.OK)
                token = login["data"]["token"]

                status, health = self._request(server, "GET", "/api/central/health", token=token)
                self.assertEqual(status, HTTPStatus.OK)
                self.assertEqual(health["data"]["summary"]["samples_24h"], 12)

                status, quality = self._request(server, "GET", "/api/central/quality", token=token)
                self.assertEqual(status, HTTPStatus.OK)
                self.assertEqual(quality["data"]["summary"]["samples_24h"], 12)

                status, telemetry = self._request(
                    server,
                    "GET",
                    "/api/central/telemetry?from=2026-09-14T03%3A00%3A00Z"
                    "&to=2026-09-14T04%3A00%3A00Z&page_size=10",
                    token=token,
                )
                self.assertEqual(status, HTTPStatus.OK)
                self.assertEqual(telemetry["data"]["total"], 0)

                # Revocation is exercised through real authentication on a disposable DB.
                with server.database.connection() as conn:
                    conn.execute("DELETE FROM role_permissions WHERE role_id=(SELECT id FROM roles WHERE code='reviewer')")
                status, limited = self._request(server, "POST", "/api/auth/login",
                                                body={"username":"reviewer", "password":"Review@2026"})
                self.assertEqual(status, HTTPStatus.OK)
                before = (len(repository.sample_calls), len(repository.health_calls))
                for route in ("health", "quality", "telemetry"):
                    status, _payload = self._request(server, "GET", f"/api/central/{route}", token=limited["data"]["token"])
                    self.assertEqual(status, HTTPStatus.FORBIDDEN)
                self.assertEqual((len(repository.sample_calls), len(repository.health_calls)), before)
            finally:
                server.shutdown()
                thread.join(timeout=5)
                server.server_close()

    @staticmethod
    def _request(server, method, path, *, token=None, body=None):
        connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=5)
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        encoded = None
        if body is not None:
            encoded = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        connection.request(method, path, body=encoded, headers=headers)
        response = connection.getresponse()
        payload = json.loads(response.read().decode("utf-8"))
        connection.close()
        return response.status, payload


if __name__ == "__main__":
    unittest.main()
