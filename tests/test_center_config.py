"""Central service configuration tests."""

from pathlib import Path
import tempfile
import unittest
import backend.center.config as config_module

from backend.center.config import CenterConfigurationError, load_center_config


CONFIG = """
[center]
topic_prefix = "oilfield"

[mqtt]
host = "127.0.0.1"
port = 1883
client_id = "platform-ingestor"
username = "platform-ingestor"
password_env = "CENTER_MQTT_PASSWORD"
session_expiry_seconds = 604800

[postgres]
host = "127.0.0.1"
port = 5432
database = "oilwell_platform"
user = "oilwell_ingestor"
password_env = "CENTER_POSTGRES_PASSWORD"
sslmode = "prefer"
"""


class CenterConfigTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.path = Path(self.temporary.name) / "center.local.toml"
        self.path.write_text(CONFIG, encoding="utf-8")

    def test_secrets_are_resolved_outside_toml(self):
        config = load_center_config(self.path, env={
            "CENTER_MQTT_PASSWORD": "mqtt-secret",
            "CENTER_POSTGRES_PASSWORD": "pg-secret",
        })
        self.assertEqual(config.mqtt.password, "mqtt-secret")
        self.assertEqual(config.mqtt_session_expiry_seconds, 604800)
        self.assertEqual(config.postgres["password"], "pg-secret")
        self.assertNotIn("pg-secret", repr(config))
        self.assertNotIn("mqtt-secret", repr(config))

    def test_missing_or_inline_secrets_are_rejected(self):
        with self.assertRaises(CenterConfigurationError):
            load_center_config(self.path, env={})
        self.path.write_text(CONFIG.replace(
            'password_env = "CENTER_POSTGRES_PASSWORD"', 'password = "bad"', 1
        ), encoding="utf-8")
        with self.assertRaises(CenterConfigurationError):
            load_center_config(self.path, env={
                "CENTER_MQTT_PASSWORD": "mqtt-secret",
                "CENTER_POSTGRES_PASSWORD": "pg-secret",
            })

    def test_remote_postgres_requires_full_certificate_verification(self):
        self.path.write_text(CONFIG.replace(
            'host = "127.0.0.1"\nport = 5432',
            'host = "db.example.com"\nport = 5432',
        ), encoding="utf-8")
        with self.assertRaises(CenterConfigurationError):
            load_center_config(self.path, env={
                "CENTER_MQTT_PASSWORD": "mqtt-secret",
                "CENTER_POSTGRES_PASSWORD": "pg-secret",
            })

    def test_query_configuration_does_not_require_mqtt_secret(self):
        loader = getattr(config_module, "load_query_postgres_config", None)
        self.assertIsNotNone(loader)
        result = loader(self.path, env={"CENTER_POSTGRES_PASSWORD": "pg-secret"})
        self.assertEqual(result["dbname"], "oilwell_platform")

    def test_query_credentials_override_ingestion_credentials(self):
        loader = getattr(config_module, "load_query_postgres_config", None)
        self.assertIsNotNone(loader)
        self.path.write_text(CONFIG + '''
[postgres_query]
host = "127.0.0.1"
database = "oilwell_platform"
user = "oilwell_reader"
password_env = "CENTER_READER_PASSWORD"
sslmode = "prefer"
''', encoding="utf-8")
        result = loader(self.path, env={"CENTER_READER_PASSWORD": "reader-secret"})
        self.assertEqual((result["user"], result["password"]), ("oilwell_reader", "reader-secret"))


if __name__ == "__main__":
    unittest.main()
