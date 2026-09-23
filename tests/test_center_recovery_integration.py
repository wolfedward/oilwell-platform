"""Opt-in fault injection against isolated Mosquitto + PostgreSQL TEMP tables.

Never stops the installed broker or modifies public PostgreSQL data.
Enable with OILWELL_POSTGRES_TEST_CONFIG=config/center.local.toml.
"""

from contextlib import contextmanager
from datetime import datetime, timezone
import os
from pathlib import Path
import shutil
import socket
import subprocess
import tempfile
import threading
import time
import unittest

from backend.center.config import load_query_postgres_config
from backend.center.ingest import CentralIngestService
from backend.center.mqtt_consumer import MqttConsumer, create_paho_client, run_persistent_session
from backend.center.postgres import PostgresTelemetryRepository
from backend.gateway.contracts import TelemetrySample, UplinkBatch
from backend.gateway.mqtt_uplink import MqttConfig


@unittest.skipUnless(os.environ.get("OILWELL_POSTGRES_TEST_CONFIG"), "live fault injection is opt-in")
class CentralRecoveryIntegrationTests(unittest.TestCase):
    def test_failed_write_then_lost_ack_recovers_with_one_committed_message(self):
        import paho.mqtt.client as mqtt
        import psycopg

        broker_path = shutil.which("mosquitto") or "C:/Program Files/Mosquitto/mosquitto.exe"
        if not Path(broker_path).is_file():
            self.skipTest("Mosquitto executable is not installed")
        with tempfile.TemporaryDirectory(prefix="oilwell-recovery-") as temporary:
            root = Path(temporary)
            with socket.socket() as reserved:
                reserved.bind(("127.0.0.1", 0))
                port = reserved.getsockname()[1]
            password_file = root / "passwords"
            flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
            subprocess.run([str(Path(broker_path).with_name("mosquitto_passwd.exe" if os.name == "nt" else "mosquitto_passwd")),
                            "-b", "-c", str(password_file), "test", "test-only-password"],
                           check=True, capture_output=True, creationflags=flags)
            config_path = root / "broker.conf"
            config_path.write_text(
                f"listener {port} 127.0.0.1\nallow_anonymous false\npassword_file {password_file.as_posix()}\npersistence false\n",
                encoding="utf-8",
            )
            broker = subprocess.Popen([broker_path, "-c", str(config_path)], stdout=subprocess.DEVNULL,
                                      stderr=subprocess.DEVNULL, creationflags=flags)
            self.addCleanup(lambda: broker.poll() is None and broker.kill())
            deadline = time.monotonic() + 5
            while True:
                self.assertIsNone(broker.poll(), "isolated broker exited during startup")
                try:
                    with socket.create_connection(("127.0.0.1", port), timeout=.2):
                        break
                except OSError:
                    self.assertLess(time.monotonic(), deadline, "isolated broker did not start")
                    threading.Event().wait(.05)

            settings = load_query_postgres_config(os.environ["OILWELL_POSTGRES_TEST_CONFIG"])
            with psycopg.connect(**settings, autocommit=True) as database:
                database.execute("CREATE TEMP TABLE ingest_messages (LIKE public.ingest_messages INCLUDING ALL)")
                database.execute("CREATE TEMP TABLE telemetry_samples (LIKE public.telemetry_samples INCLUDING ALL)")

                @contextmanager
                def connection_factory(_dsn, _kwargs):
                    with database.transaction():
                        yield database

                repository = PostgresTelemetryRepository(connect_kwargs=settings, connect_factory=connection_factory)
                real_store = repository.store
                attempts, stored_results, acks, errors = [], [], [], []
                subscribed, finished = threading.Event(), threading.Event()
                stop = threading.Event()

                def flaky_store(*args, **kwargs):
                    attempts.append(kwargs["payload_sha256"])
                    if len(attempts) == 1:
                        raise psycopg.OperationalError("injected transient database outage")
                    result = real_store(*args, **kwargs)
                    stored_results.append(result)
                    return result

                repository.store = flaky_store
                config = MqttConfig(host="127.0.0.1", port=port, client_id="isolated-center",
                                    username="test", password="test-only-password")
                consumer = MqttConsumer(CentralIngestService(repository))
                client = create_paho_client(config, consumer, on_error=lambda error, _message: errors.append(type(error).__name__))
                client.on_connect = lambda c, *_args: c.subscribe("oilfield/v1/+/+/telemetry", qos=1)
                client.on_subscribe = lambda *_args: subscribed.set()
                real_ack = client.ack

                def lost_ack(mid, qos):
                    acks.append((mid, qos))
                    if len(acks) == 1:
                        return mqtt.MQTT_ERR_NO_CONN  # Commit happened, PUBACK did not.
                    result = real_ack(mid, qos)
                    stop.set()
                    client.disconnect()
                    return result

                client.ack = lost_ack

                def retry(_delay, _error):
                    if stop.is_set():
                        raise KeyboardInterrupt

                thread_errors = []

                def receive():
                    try:
                        run_persistent_session(client, config, 3600, on_retry=retry)
                    except KeyboardInterrupt:
                        pass
                    except BaseException as error:
                        thread_errors.append(error)
                    finally:
                        finished.set()

                worker = threading.Thread(target=receive, daemon=True)
                worker.start()
                publisher = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="isolated-publisher", protocol=mqtt.MQTTv5)
                publisher.username_pw_set("test", "test-only-password")
                try:
                    self.assertTrue(subscribed.wait(5), "consumer subscription missing")
                    now = datetime.now(timezone.utc)
                    batch = UplinkBatch.create("GW-TEST", "D-TEST", "fault-injection",
                        [TelemetrySample("GW-TEST", "D-TEST", "motor.active_power", now, 1, 5.8, unit="kW")],
                        message_id="fault-injection-message", created_at=now)
                    publisher.connect("127.0.0.1", port)
                    publisher.loop_start()
                    info = publisher.publish(batch.topic("oilfield"), batch.to_json(), qos=1)
                    info.wait_for_publish(timeout=5)
                    self.assertTrue(info.is_published())
                    self.assertTrue(finished.wait(12), "ingestor did not recover after transient faults")
                    self.assertEqual(thread_errors, [])
                    self.assertEqual(len(attempts), 3)
                    self.assertEqual(len(set(attempts)), 1, "redelivery must preserve the exact payload")
                    self.assertEqual(stored_results, [True, False], "second commit must be recognized as duplicate")
                    self.assertEqual(errors, ["OperationalError", "MqttConsumerError"])
                    self.assertEqual(database.execute("SELECT count(*) FROM ingest_messages").fetchone()[0], 1)
                    self.assertEqual(database.execute("SELECT count(*) FROM telemetry_samples").fetchone()[0], 1)
                finally:
                    stop.set()
                    client.disconnect()
                    publisher.disconnect()
                    publisher.loop_stop()
                    worker.join(timeout=5)
                    broker.terminate()
                    broker.wait(timeout=5)


if __name__ == "__main__":
    unittest.main()
