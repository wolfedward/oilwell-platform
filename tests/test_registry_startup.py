"""Unavailable optional registry must not break health, login, or valid old configuration."""
from contextlib import contextmanager
from http.client import HTTPConnection
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

from backend.http_server import create_server
from backend.registry.runtime import RegistryRuntime


class RegistryStartupTests(unittest.TestCase):
    @contextmanager
    def platform(self,**kwargs):
        with tempfile.TemporaryDirectory(prefix="registry-startup-") as directory:
            server=create_server("127.0.0.1",0,Path(directory)/"platform.sqlite3",**kwargs)
            token=server.auth.login("administrator","Admin@2026").token
            thread=threading.Thread(target=server.serve_forever,daemon=True)
            thread.start()
            def http(path,*,authorized=True):
                connection=HTTPConnection("127.0.0.1",server.server_port,timeout=5)
                try:
                    connection.request("GET",path,headers={"Authorization":"Bearer "+token} if authorized else {})
                    response=connection.getresponse()
                    return response.status,json.loads(response.read())
                finally:
                    connection.close()
            try:
                yield server,http
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)

    def test_no_config_has_disabled_capability_and_no_implicit_registry_connection(self):
        with self.platform() as (server,http):
            self.assertEqual(server.registry_runtime.status,"disabled")
            self.assertIsNone(server.registry_runtime.service)
            self.assertEqual(http("/api/health")[0],200)
            status,me=http("/api/auth/me")
            self.assertEqual(status,200)
            self.assertEqual(me["data"].get("capabilities",{}).get("registry"),{"enabled":False,"status":"disabled"})
            self.assertEqual(http("/api/registry/devices")[0],503)

    def test_unavailable_registry_keeps_health_login_and_old_repository_available(self):
        runtime=RegistryRuntime("unavailable",code="registry.schema_missing")
        repository=Mock()
        with self.platform(registry_runtime=runtime,central_repository=repository) as (server,http):
            self.assertIs(server.central_telemetry,repository)
            self.assertEqual(http("/api/health")[0],200)
            status,payload=http("/api/registry/devices")
            self.assertEqual((status,payload["error"]["code"]),(503,"registry.schema_missing"))
            capability=http("/api/auth/me")[1]["data"].get("capabilities",{}).get("registry")
            self.assertEqual(capability,{"enabled":True,"status":"unavailable"})

    def test_authenticated_capability_is_small_and_never_exposes_connection_fields(self):
        runtime=RegistryRuntime("ready",Mock(),Mock(),allowed_origins=("http://127.0.0.1:8765",))
        with self.platform(registry_runtime=runtime) as (_,http):
            status,payload=http("/api/auth/me",authorized=False)
            self.assertEqual(status,401)
            self.assertNotIn("capabilities",payload)
            capability=http("/api/auth/me")[1]["data"].get("capabilities",{}).get("registry")
            self.assertEqual(capability,{"enabled":True,"status":"ready"})

    def test_bad_registry_config_is_isolated_from_valid_old_center_query_configuration(self):
        with tempfile.TemporaryDirectory(prefix="registry-config-isolation-") as directory:
            path=Path(directory)/"center.toml"
            path.write_text('[registry]\nenabled=true\nallowed_origins=["http://127.0.0.1:8765"]\n'
                '[postgres]\nhost="127.0.0.1"\nport=3822\ndatabase="oilwell_registry_test"\nuser="test_ingest"\npassword_env="TEST_INGEST_UNUSED"\nsslmode="disable"\n'
                '[postgres_query]\nhost="127.0.0.1"\nport=3822\ndatabase="oilwell_registry_test"\nuser="test_reader"\npassword_env="TEST_READER_ONLY"\nsslmode="disable"\n',encoding="utf-8")
            with patch.dict(os.environ,{"TEST_READER_ONLY":"not-a-real-connection-secret"}):
                with self.platform(center_config_path=path) as (server,http):
                    self.assertIsNotNone(server.central_telemetry)
                    self.assertEqual(server.registry_runtime.status,"unavailable")
                    self.assertIsNone(server.registry_runtime.service)
                    self.assertEqual(http("/api/health")[0],200)
                    status,payload=http("/api/registry/devices")
                    self.assertEqual((status,payload["error"]["code"]),(503,"registry.writer_identity_required"))
                    self.assertNotIn("not-a-real-connection-secret",json.dumps(payload))
