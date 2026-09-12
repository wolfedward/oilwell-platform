"""Gateway TOML loading and secret-resolution tests."""

from pathlib import Path
import tempfile
import unittest

from backend.gateway.config import GatewayConfigurationError, load_gateway_config


VALID_CONFIG = """
[gateway]
id = "GW-001"
spool_database = "data/uplink.db"
poll_interval_seconds = 5

[mqtt]
host = "127.0.0.1"
port = 1883
client_id = "GW-001"
username = "gateway"
password_env = "TEST_MQTT_PASSWORD"
topic_prefix = "oilfield"

[[modbus.devices]]
device_id = "D-001"
transport = "tcp"
host = "192.0.2.10"
port = 502

[[modbus.devices.points]]
point_code = "motor.current"
unit_id = 1
function_code = 4
address = 10
data_type = "float32"
unit = "A"
"""


class GatewayConfigTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.path = self.root / "gateway.local.toml"
        self.path.write_text(VALID_CONFIG, encoding="utf-8")

    def test_relative_paths_resolve_from_project_root_and_secret_from_environment(self):
        config = load_gateway_config(
            self.path, env={"TEST_MQTT_PASSWORD": "not-in-source"}, project_root=self.root
        )
        self.assertEqual(config.gateway_id, "GW-001")
        self.assertEqual(config.spool_database, self.root / "data" / "uplink.db")
        self.assertEqual(config.mqtt.password, "not-in-source")
        self.assertEqual(config.devices[0].points[0].register_width, 2)

    def test_missing_secret_and_inline_password_are_rejected(self):
        with self.assertRaises(GatewayConfigurationError):
            load_gateway_config(self.path, env={}, project_root=self.root)
        self.path.write_text(VALID_CONFIG.replace(
            'password_env = "TEST_MQTT_PASSWORD"', 'password = "bad"'
        ), encoding="utf-8")
        with self.assertRaises(GatewayConfigurationError):
            load_gateway_config(self.path, env={}, project_root=self.root)

    def test_windows_credential_target_can_replace_environment_secret(self):
        from backend.secret_store import StoredCredential

        self.path.write_text(VALID_CONFIG.replace(
            'password_env = "TEST_MQTT_PASSWORD"',
            'password_credential = "OilWell.MQTT.Gateway.GW-001"',
        ), encoding="utf-8")
        config = load_gateway_config(
            self.path,
            env={},
            project_root=self.root,
            credential_reader=lambda target: StoredCredential("gateway", "credential-secret"),
        )
        self.assertEqual(config.mqtt.password, "credential-secret")
        self.assertNotIn("credential-secret", repr(config))

    def test_duplicate_device_ids_and_point_codes_are_rejected(self):
        duplicated_device = VALID_CONFIG + VALID_CONFIG.split("[[modbus.devices]]", 1)[1].join(("\n[[modbus.devices]]", ""))
        self.path.write_text(duplicated_device, encoding="utf-8")
        with self.assertRaises(GatewayConfigurationError):
            load_gateway_config(
                self.path, env={"TEST_MQTT_PASSWORD": "secret"}, project_root=self.root
            )


if __name__ == "__main__":
    unittest.main()
