"""Registry HTTP boundary: permissions, framing, origins, and immutable commands."""
from email.message import Message
import http.client
import importlib
import io
import json
from pathlib import Path
import socket
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock
from uuid import uuid4

from backend.auth import AuthError, PermissionDenied, User
from backend.http_server import PlatformRequestHandler, create_server
from backend.registry.errors import RegistryError
from backend.registry.runtime import RegistryRuntime


ROLES = {
    "administrator": {"registry.read", "registry.wells.manage", "registry.devices.manage", "registry.bindings.manage", "registry.maps.manage", "registry.review", "telemetry.read", "acquisition.read", "audit.read"},
    "production_engineer": {"registry.read", "registry.wells.manage", "registry.bindings.manage", "registry.review", "telemetry.read", "acquisition.read"},
    "algorithm_engineer": {"registry.read", "registry.maps.manage", "telemetry.read", "acquisition.read"},
    "maintenance_engineer": {"registry.read", "registry.devices.manage", "registry.bindings.manage", "registry.maps.manage", "telemetry.read", "acquisition.read"},
    "reviewer": {"registry.read", "registry.review", "telemetry.read", "acquisition.read", "audit.read"},
}
D = "00000000-0000-0000-0000-000000000001"
W = "00000000-0000-0000-0000-000000000002"


class Handler:
    _require = PlatformRequestHandler._require
    _token = PlatformRequestHandler._token
    _error = staticmethod(PlatformRequestHandler._error)

    def _send_json(self, status, payload):
        self.response = int(status), payload


class Connection:
    def __init__(self):
        self.timeout = None
        self.changes = []

    def gettimeout(self):
        return self.timeout

    def settimeout(self, value):
        self.timeout = value
        self.changes.append(value)


def api_class(test):
    try:
        return importlib.import_module("backend.registry.api").RegistryApi
    except ModuleNotFoundError as error:
        if error.name != "backend.registry.api":
            raise
        test.fail("Authenticated registry HTTP API has not been implemented")


class RegistryApiCase(unittest.TestCase):
    def setUp(self):
        self.api_class = api_class(self)
        self.runtime = RegistryRuntime("ready", Mock(), Mock(), allowed_origins=("http://127.0.0.1:8765",))
        self.runtime.service.execute.return_value = {"entity": {"id": D, "version": "9007199254740993"},
            "registry_revision": "2", "committed_at": "2026-09-23T00:00:00+00:00", "replayed": False}
        self.runtime.query.read.return_value = {"items": [], "page": 1, "page_size": 20, "total": 0, "registry_revision": "1"}
        self.allowed = None
        self.auth_calls = []

    def body(self, **fields):
        return {"reason": "登记核验", "idempotency_key": str(uuid4()), **fields}

    def device(self):
        return self.body(gateway_id="GW", device_id="D", name="现场终端", transport="mqtt_direct", usage="commissioning")

    def request(self, method, path, body=None, *, actor="maintenance_engineer", headers=None):
        permissions = self.allowed if self.allowed is not None else ROLES.get(actor, set())
        raw = body if isinstance(body, bytes) else json.dumps(body or {}, ensure_ascii=False).encode("utf-8")
        defaults = {"Content-Type": "application/json", "Content-Length": str(len(raw)), "Authorization": "Bearer " + str(actor)}
        if headers is not None:
            defaults.update({key: value for key, value in headers.items() if value is not None})
            for key, value in headers.items():
                if value is None:
                    defaults.pop(key, None)
        message = Message()
        for key, value in defaults.items():
            for item in value if isinstance(value, list) else [value]:
                message[key] = item
        def require(token, permission):
            self.auth_calls.append(permission)
            if actor is None or token != actor:
                raise AuthError()
            if permission not in permissions:
                raise PermissionDenied()
            return User(101, actor, "实际操作者", 10, actor, frozenset(permissions), 1)
        self.handler = Handler()
        self.handler.server = SimpleNamespace(registry_runtime=self.runtime, auth=SimpleNamespace(require=require))
        self.handler.headers = message
        self.handler.rfile = io.BytesIO(raw)
        self.handler.connection = Connection()
        self.handler.close_connection = False
        self.handler.response = None
        handled = getattr(self.api_class(self.handler), method.lower())(path)
        if not handled:
            return None, None
        self.assertIsNotNone(self.handler.response)
        return self.handler.response

    def test_five_role_route_matrix_and_real_actor_command(self):
        writes = [
            ("/wells", self.body(area_code="A", well_code="W", name="井"), {"registry.wells.manage"}, 201, "well.create"),
            (f"/wells/{W}/changes", self.body(expected_version="1", name="井名"), {"registry.wells.manage"}, 200, "well.change"),
            ("/devices", self.device(), {"registry.devices.manage"}, 201, "device.create"),
            (f"/devices/{D}/changes", self.body(expected_version="1", name="终端"), {"registry.devices.manage"}, 200, "device.change"),
            (f"/devices/{D}/bindings", self.body(expected_version="1", well_id=W), {"registry.bindings.manage"}, 200, "device.bind"),
            (f"/devices/{D}/unbind", self.body(expected_version="1"), {"registry.bindings.manage"}, 200, "device.unbind"),
            (f"/devices/{D}/point-maps", self.body(expected_version="1", parent_version=None, points=[{"point_code":"load", "unit":"kN"}]), {"registry.maps.manage"}, 201, "map.create"),
            (f"/devices/{D}/point-map-assignment", self.body(expected_version="1", map_version="1"), {"registry.maps.manage"}, 200, "map.assign"),
            (f"/devices/{D}/point-maps/9007199254740993/review", self.body(expected_version="1", sha256="a"*64, decision="approved"), {"registry.review"}, 200, "map.review"),
        ]
        for state in ("active", "suspended", "retired"):
            writes.append((f"/devices/{D}/state", self.body(expected_version="1", status=state, check_note="人工核对依据"),
                           {"registry.devices.manage", "registry.review"} if state == "suspended" else {"registry.review"}, 200, "device.state"))
        reads = [("/wells", "wells", set()), ("/devices", "devices", set()), (f"/devices/{D}", "device", set()),
                 (f"/devices/{D}/state-history", "state_history", set()), (f"/devices/{D}/bindings", "bindings", set()),
                 (f"/devices/{D}/point-maps", "point_maps", set()), (f"/devices/{D}/point-map-assignments", "map_assignments", set()),
                 ("/resolved-telemetry?gateway_id=GW&device_id=D", "resolved_telemetry", {"telemetry.read"}),
                 (f"/devices/{D}/link-status", "link_status", {"acquisition.read"}), ("/audit", "audit", {"audit.read"})]
        for role, permissions in ROLES.items():
            for route, body, required, status, action in writes:
                with self.subTest(role=role, route=route, action=action, state=body.get("status")):
                    before = self.runtime.service.execute.call_count
                    result, payload = self.request("POST", "/api/registry" + route, body, actor=role)
                    allowed = bool(required & permissions)
                    self.assertEqual(result, status if allowed else 403)
                    self.assertEqual(self.runtime.service.execute.call_count - before, int(allowed))
                    if allowed:
                        command = self.runtime.service.execute.call_args.args[0]
                        self.assertEqual(command.action, action)
                        self.assertEqual((command.actor.domain, command.actor.user_id, command.actor.username, command.actor.role_code),
                                         ("platform-local", "101", role, role))
                        if action == "map.review":
                            self.assertEqual(command.body["map_version"], "9007199254740993")
            for route, resource, required in reads:
                with self.subTest(role=role, resource=resource):
                    before = self.runtime.query.read.call_count
                    result, _ = self.request("GET", "/api/registry" + route, actor=role)
                    allowed = not required or bool(required & permissions)
                    self.assertEqual(result, 200 if allowed else 403)
                    self.assertEqual(self.runtime.query.read.call_count - before, int(allowed))
                    if allowed:
                        self.assertEqual(self.runtime.query.read.call_args.args[0], resource)

    def test_revoked_permission_blocks_receipt_replay_before_service(self):
        self.allowed = set(ROLES["maintenance_engineer"])
        body = self.device()
        self.assertEqual(self.request("POST", "/api/registry/devices", body)[0], 201)
        self.allowed.remove("registry.devices.manage")
        self.assertEqual(self.request("POST", "/api/registry/devices", body)[0], 403)
        self.assertEqual(self.runtime.service.execute.call_count, 1)

    def test_authentication_and_registry_read_precede_any_repository_access(self):
        for actor, expected in ((None, 401), ("unprivileged", 403)):
            for method, path in (("GET", "/api/registry/devices"), ("POST", "/api/registry/devices"), ("GET", "/api/registry/unknown")):
                with self.subTest(actor=actor, method=method):
                    self.assertEqual(self.request(method, path, self.device(), actor=actor)[0], expected)
        self.assertEqual(self.runtime.query.read.call_count + self.runtime.service.execute.call_count, 0)

    def test_raw_link_and_audit_cannot_use_only_registry_permission(self):
        self.allowed = {"registry.read"}
        for route in ("/resolved-telemetry?gateway_id=GW&device_id=D", f"/devices/{D}/link-status", "/audit"):
            with self.subTest(route=route):
                self.assertEqual(self.request("GET", "/api/registry" + route)[0], 403)
        self.assertEqual(self.runtime.query.read.call_count, 0)

    def test_origin_exact_allowlist_does_not_trust_host_or_cookie_without_origin(self):
        for headers, expected in (({"Origin":"http://evil.test:8765", "Host":"127.0.0.1:8765"}, 403),
                                  ({"Origin":"null"}, 403), ({"Origin":["http://127.0.0.1:8765", "http://evil.test:8765"]},403),
                                  ({"Origin":"http://127.0.0.1:8765"}, 201), ({}, 201),
                                  ({"Authorization":None,"Cookie":"platform_session=maintenance_engineer"},403),
                                  ({"Authorization":"Basic invalid","Cookie":"platform_session=maintenance_engineer"},403),
                                  ({"Authorization":"Bearer wrong","Cookie":"platform_session=maintenance_engineer"},401)):
            with self.subTest(headers=headers):
                self.assertEqual(self.request("POST", "/api/registry/devices", self.device(), headers=headers)[0], expected)

    def test_body_framing_and_content_type_rejections_close_connection(self):
        for headers in ({"Content-Length":["2","2"]}, {"Content-Length":"-1"}, {"Content-Length":"2.0"},
                        {"Content-Length":None}, {"Content-Length":"65537"}, {"Transfer-Encoding":"chunked"},
                        {"Content-Type":"text/plain"}, {"Content-Type":"application/jsonp"}):
            with self.subTest(headers=headers):
                status, _ = self.request("POST", "/api/registry/devices", self.device(), headers=headers)
                self.assertEqual(status, 422)
                self.assertTrue(self.handler.close_connection)
        self.assertEqual(self.runtime.service.execute.call_count, 0)

    def test_read_timeout_is_finite_and_restored(self):
        self.assertEqual(self.request("POST", "/api/registry/devices", self.device())[0], 201)
        self.assertTrue(any(isinstance(x, (int,float)) and 0 < x <= 5 for x in self.handler.connection.changes))
        self.assertIsNone(self.handler.connection.gettimeout())

    def test_short_json_duplicate_keys_unknown_fields_and_actor_are_rejected(self):
        for body in (b'{"name":"a","name":"b"}', b'\xff', b'[]', self.device() | {"actor":{"user_id":"1"}},
                     self.device() | {"sql":"DROP TABLE wells"}):
            with self.subTest(body=body):
                self.assertEqual(self.request("POST", "/api/registry/devices", body)[0], 422)
        self.assertEqual(self.request("POST", "/api/registry/devices", b'{}', headers={"Content-Length":"3"})[0],422)
        self.assertEqual(self.runtime.service.execute.call_count, 0)

    def test_path_uuid_and_version_cannot_be_overridden_in_body(self):
        body = self.body(expected_version="1", decision="approved", sha256="a"*64)
        for route, supplied in ((f"/devices/{D}/point-maps/01/review",body),
                                (f"/devices/{D}/point-maps/1/review", body | {"map_version":"2"}),
                                ("/devices/not-a-uuid/changes",self.body(expected_version="1",name="终端"))):
            with self.subTest(route=route):
                self.assertEqual(self.request("POST", "/api/registry" + route, supplied, actor="administrator")[0],422)
        self.assertEqual(self.runtime.service.execute.call_count, 0)

    def test_get_rejects_repeated_unknown_and_path_bound_identity_parameters(self):
        for path in ("/devices?page=1&page=2", "/devices?sql=SELECT", f"/devices/{D}?id={W}",
                     f"/devices/{D}/bindings?device_id=D", "/devices?%GG=1", "/devices?page=%FF"):
            with self.subTest(path=path):
                self.assertEqual(self.request("GET", "/api/registry" + path)[0],422)
        self.assertEqual(self.runtime.query.read.call_count, 0)

    def test_resolved_paging_preserves_query_total_and_transport_pair(self):
        path = "/api/registry/resolved-telemetry?gateway_id=GW&device_id=D&page=2&revision=9007199254740993&received_before=2026-09-23T00%3A00%3A00Z&query_total=12"
        self.assertEqual(self.request("GET", path)[0],200)
        resource, filters = self.runtime.query.read.call_args.args
        self.assertEqual(resource,"resolved_telemetry")
        self.assertEqual((filters["device_id"],filters["revision"],filters["query_total"]),("D","9007199254740993","12"))
        self.assertEqual(self.request("GET", f"/api/registry/devices/{D}/bindings")[0],200)
        self.assertEqual(self.runtime.query.read.call_args.args[1]["id"],D)

    def test_replay_creation_is_200_and_admin_receives_version_conflicts(self):
        self.runtime.service.execute.return_value["replayed"] = True
        self.assertEqual(self.request("POST", "/api/registry/devices", self.device(), actor="administrator")[0],200)
        self.runtime.service.execute.side_effect = RegistryError("registry.version_conflict","版本已变化",409)
        self.assertEqual(self.request("POST", f"/api/registry/devices/{D}/changes", self.body(expected_version="1",name="终端"), actor="administrator")[0],409)

    def test_disabled_and_failed_registry_return_explicit_unavailability(self):
        for state, code in (("disabled","registry.disabled"),("unavailable","registry.query_identity_required")):
            self.runtime = RegistryRuntime(state,code=code)
            status,payload = self.request("GET", "/api/registry/devices")
            self.assertEqual((status,payload["error"]["code"]),(503,code))
        self.assertEqual(self.request("GET", "/api/registry/devices",actor=None)[0],401)

    def test_internal_errors_do_not_expose_secrets_or_sql(self):
        self.runtime.query.read.side_effect = RuntimeError("password=private-test-secret SELECT FROM hidden")
        status,payload = self.request("GET", "/api/registry/devices")
        self.assertEqual(status,503)
        self.assertNotIn("private-test-secret",json.dumps(payload))
        self.assertNotIn("SELECT",json.dumps(payload))

    def test_non_registry_paths_fall_through_but_owned_unknown_paths_are_404(self):
        self.assertEqual(self.request("GET","/api/registry-other"),(None,None))
        self.assertEqual(self.request("GET","/api/registry/not-found")[0],404)
        self.assertEqual(self.request("POST","/api/registry/devices/unknown",self.device())[0],404)


class RegistryHTTPTests(unittest.TestCase):
    def setUp(self):
        api_class(self)
        self.temp = tempfile.TemporaryDirectory(prefix="registry-http-")
        self.addCleanup(self.temp.cleanup)
        self.runtime = RegistryRuntime("ready",Mock(),Mock(),allowed_origins=("http://127.0.0.1:8765",))
        self.runtime.service.execute.return_value = {"entity":{"id":D,"version":"1"},"registry_revision":"2","replayed":False}
        self.runtime.query.read.return_value = {"items":[],"total":0,"registry_revision":"1"}
        self.server = create_server("127.0.0.1",0,Path(self.temp.name)/"local.sqlite3",registry_runtime=self.runtime)
        self.thread = threading.Thread(target=self.server.serve_forever,daemon=True)
        self.thread.start()
        self.addCleanup(self.close)
        self.token = self.server.auth.login("maintenance_engineer","Maintenance@2026").token

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)

    def request(self,method,path,body=None,headers=None):
        conn = http.client.HTTPConnection("127.0.0.1",self.server.server_port,timeout=5)
        try:
            conn.request(method,path,json.dumps(body) if body is not None else None,
                         {"Authorization":"Bearer "+self.token,"Content-Type":"application/json",**(headers or {})})
            result=conn.getresponse()
            return result.status,json.loads(result.read())
        finally:
            conn.close()

    def test_real_http_retry_and_permission_revocation(self):
        body={"gateway_id":"GW","device_id":"D","name":"终端","transport":"mqtt_direct","usage":"commissioning","reason":"登记核验","idempotency_key":str(uuid4())}
        self.assertEqual(self.request("POST","/api/registry/devices",body)[0],201)
        original=self.runtime.service.execute.call_args.args[0]
        self.runtime.service.execute.return_value["replayed"]=True
        self.assertEqual(self.request("POST","/api/registry/devices",body)[0],200)
        retry=self.runtime.service.execute.call_args.args[0]
        self.assertEqual((original.key,original.request_hash),(retry.key,retry.request_hash))
        with self.server.database.connection() as db:
            db.execute("DELETE FROM role_permissions WHERE role_id=(SELECT id FROM roles WHERE code='maintenance_engineer') AND permission_id=(SELECT id FROM permissions WHERE code='registry.devices.manage')")
        self.assertEqual(self.request("POST","/api/registry/devices",body)[0],403)
        self.assertEqual(self.runtime.service.execute.call_count,2)

    def test_browser_default_port_origins_work_without_accepting_other_origins(self):
        from dataclasses import replace
        from backend.registry.config import _origin
        body={"gateway_id":"GW","device_id":"D","name":"终端","transport":"mqtt_direct","usage":"commissioning","reason":"登记核验","idempotency_key":str(uuid4())}
        for configured, browser_origin, wrong in (
            ("http://localhost:80", "http://localhost", "http://localhost:8765"),
            ("https://localhost:443", "https://localhost", "http://localhost"),
            ("https://localhost", "https://localhost", "https://evil.test"),
            ("https://localhost:8443", "https://localhost:8443", "https://localhost"),
            ("https://[0:0:0:0:0:0:0:1]:443", "https://[::1]", "https://[::1]:8443"),
        ):
            with self.subTest(configured=configured):
                self.server.registry_runtime=replace(self.runtime,allowed_origins=(_origin(configured),))
                self.assertEqual(self.request("POST","/api/registry/devices",body,{"Origin":browser_origin})[0],201)
                before=self.runtime.service.execute.call_count
                for rejected in (wrong, "null", browser_origin + "/"):
                    self.assertEqual(self.request("POST","/api/registry/devices",body,{"Origin":rejected})[0],403)
                self.assertEqual(self.runtime.service.execute.call_count,before)

    def test_actual_session_revocation_blocks_registry_and_old_health_still_works(self):
        self.assertEqual(self.request("GET","/api/registry/devices")[0],200)
        self.server.auth.logout(self.token)
        self.assertEqual(self.request("GET","/api/registry/devices")[0],401)
        self.assertEqual(self.request("GET","/api/health")[0],200)
        self.assertEqual(self.runtime.query.read.call_count,1)

    def test_unsupported_methods_do_not_open_mutation_or_cors(self):
        for method in ("PATCH","DELETE","OPTIONS","PUT"):
            with self.subTest(method=method):
                self.assertEqual(self.request(method,"/api/registry/devices",{})[0],404)
        self.assertEqual(self.runtime.service.execute.call_count,0)

    def test_head_is_authenticated_controlled_and_sends_no_json_body(self):
        with socket.create_connection(("127.0.0.1",self.server.server_port),timeout=5) as connection:
            request=("HEAD /api/registry/devices HTTP/1.0\r\nHost: localhost\r\nAuthorization: Bearer "+self.token+"\r\n\r\n")
            connection.sendall(request.encode("ascii"))
            chunks=[]
            while chunk:=connection.recv(8192):
                chunks.append(chunk)
        headers,body=b"".join(chunks).split(b"\r\n\r\n",1)
        self.assertIn(b" 404 ",headers.split(b"\r\n")[0])
        self.assertEqual(body,b"")
        self.assertNotIn(b"Access-Control-Allow-Origin",headers)
        self.assertEqual(self.runtime.service.execute.call_count+self.runtime.query.read.call_count,0)
