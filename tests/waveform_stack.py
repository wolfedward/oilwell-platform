"""Owned loopback broker and component processes for synthetic WP04 tests only."""
from dataclasses import asdict
import json
import os
from pathlib import Path
import secrets
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from uuid import uuid4
from unittest.mock import patch

from tests.ingest_broker import Peer
from tests.test_waveform_config import config_sections, toml_text
from tests.waveform_support import LIMITS

ROOT = Path(__file__).resolve().parents[1]
FLAGS = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def sections_for(fixture, *, limits=LIMITS, port=1883):
    sections = config_sections()
    sections = {key: value for key, value in sections.items() if not key.startswith("gateways.")}
    sections["capacity.global"] = asdict(limits.global_quota)
    sections["waveforms"]["max_db_connections"] = max(4, len(limits.gateways))
    for role in ("receiver", "assembler", "receipt_publisher", "reader", "operator", "migration_owner"):
        sections["postgres_" + role] = dict(fixture.raw_config["postgres_" + role])
    for gateway, quota in limits.gateways.items():
        prefix = "gateways." + gateway
        sections[prefix] = {"source_ref": str(uuid4())}
        sections[prefix + ".quota"] = asdict(quota)
        for role in ("receive", "publish", "sender"):
            sections[prefix + ".mqtt_" + role] = {"host": "127.0.0.1", "port": port,
                "client_id": "wp04-" + gateway + "-" + role + "-" + uuid4().hex,
                "username": "waveform-" + gateway + "-" + role, "password_env": "TEST_WAVEFORM_MQTT",
                "session_expiry_seconds": 0 if role == 'publish' else 3600, "keepalive_seconds": 10, "connect_timeout_seconds": 1}
    return sections


def runtime_config(fixture, component):
    from backend.waveforms.config import load_waveform_config
    with tempfile.TemporaryDirectory(prefix="waveform-runtime-") as directory:
        path = Path(directory) / "config.toml"
        path.write_text(toml_text(sections_for(fixture)), encoding="utf-8")
        with patch.dict(os.environ, {"TEST_WAVEFORM_MQTT": "synthetic-local"}):
            return load_waveform_config(path, component=component)


class Broker:
    def __init__(self, root, gateways):
        self.root, self.clients, self.process, self.log = root, [], None, None
        self.password = secrets.token_urlsafe(24)
        self.executable = Path(shutil.which("mosquitto") or "C:/Program Files/mosquitto/mosquitto.exe")
        if not self.executable.is_file():
            raise RuntimeError("Owned Mosquitto is required")
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            self.port = sock.getsockname()[1]
        users = ["waveform-" + gateway + "-" + role for gateway in gateways for role in ("receive", "sender", "publish")]
        password_file = root / "passwords"
        passwd = self.executable.with_name("mosquitto_passwd.exe" if os.name == "nt" else "mosquitto_passwd")
        for index, user in enumerate(users):
            result = subprocess.run([str(passwd), "-b", *(["-c"] if index == 0 else []),
                str(password_file), user, self.password], capture_output=True, timeout=10, creationflags=FLAGS)
            if result.returncode:
                raise RuntimeError("Owned broker credential setup failed")
        acl = root / "acl"
        acl.write_text("\n".join(f"user waveform-{gateway}-{role}\ntopic {access} oil/v2/{gateway}/+/waveform\n"
            + (f"topic read oil/v2/{gateway}/+/waveform-ack\n" if role == 'sender' else '')
            for gateway in gateways for role, access in (("receive", "read"), ("sender", "write")))
            + '\n' + '\n'.join(f'user waveform-{gateway}-publish\ntopic write oil/v2/{gateway}/+/waveform-ack\n' for gateway in gateways), encoding="utf-8")
        self.config = root / "mosquitto.conf"
        self.config.write_text(f"listener {self.port} 127.0.0.1\nallow_anonymous false\n"
            f"password_file {password_file.as_posix()}\nacl_file {acl.as_posix()}\npersistence false\n"
            "max_packet_size 270336\nmessage_size_limit 262144\nmax_inflight_messages 4\nlog_dest stderr\nlog_type all\n", encoding="utf-8")
        try:
            self.start()
        except BaseException:
            self.stop()
            raise

    def start(self):
        if self.process and self.process.poll() is None:
            raise AssertionError("Owned broker already running")
        self.log = (self.root / "broker.log").open("ab")
        self.process = subprocess.Popen([str(self.executable), "-c", str(self.config)],
            stdout=self.log, stderr=self.log, creationflags=FLAGS)
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if self.process.poll() is not None:
                raise AssertionError("Owned broker exited")
            try:
                with socket.create_connection(("127.0.0.1", self.port), timeout=.1): return
            except OSError: time.sleep(.02)
        raise AssertionError("Owned broker startup timeout")

    def stop(self):
        if self.process and self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=5)
        if self.log: self.log.close()

    def close(self):
        try:
            for peer in self.clients: peer.close()
        finally:
            self.clients.clear()
            self.stop()


class Stack:
    def __init__(self, fixture, *, limits=LIMITS):
        self.fixture, self.limits = fixture, limits
        self.temp = tempfile.TemporaryDirectory(prefix="wp04-stack-")
        self.root = Path(self.temp.name)
        self.processes, self.handles, self.peers = {}, [], {}
        self.broker = None

    def __enter__(self):
        try:
            self.broker = Broker(self.root, self.limits.gateways)
            self.sections = sections_for(self.fixture, limits=self.limits, port=self.broker.port)
            self.config = self.root / "waveforms.toml"
            self.config.write_text(toml_text(self.sections), encoding="utf-8")
            return self
        except BaseException:
            self.close()
            raise

    def __exit__(self, kind, *_args):
        try:
            if kind is not None: self.preserve_evidence()
        finally:
            self.close()

    def preserve_evidence(self):
        destination = Path(os.environ.get("OILWELL_WAVEFORM_EVIDENCE_DIR",
            str(ROOT / ".superpowers/sdd/2026-09-29-synchronized-waveform-paired-dataset/task-6-failures"))) / uuid4().hex
        destination.mkdir(parents=True, exist_ok=False)
        for path in self.root.glob("*.log"):
            shutil.copyfile(path, destination / path.name)
        return destination

    def close(self):
        failures = []
        try:
            for name in self.processes:
                try: self.stop(name)
                except AssertionError as error: failures.append(error)
        finally:
            for handle in self.handles: handle.close()
            if self.broker: self.broker.close()
            self.temp.cleanup()
        if failures: raise failures[0]

    def launch(self, component, *, fault="", check=False):
        prior = self.processes.get(component)
        if prior and prior.poll() is None: raise AssertionError("Duplicate owned worker")
        output = (self.root / (component + ".log")).open("ab", buffering=0)
        self.handles.append(output)
        env = dict(os.environ, PYTHONUTF8="1", TEST_WAVEFORM_MQTT=self.broker.password)
        executable = sys.executable
        base = getattr(sys, "_base_executable", executable)
        if os.name == "nt" and os.path.normcase(executable) != os.path.normcase(base):
            env["__PYVENV_LAUNCHER__"] = executable
            executable = base
        args = [executable, "-u", "-m", "tests.waveform_worker", component, "--config", str(self.config), "--fault", fault]
        if check: args.append("--check")
        self.processes[component] = subprocess.Popen(args, cwd=ROOT, env=env, stdin=subprocess.PIPE,
            stdout=output, stderr=output, creationflags=FLAGS)
        return self.processes[component]

    def stop(self, name):
        process = self.processes[name]
        if process.poll() is None:
            try:
                process.stdin.write(b"stop\n")
                process.stdin.flush()
                process.wait(timeout=25)  # Product has one total 20s graceful budget.
            except (OSError, subprocess.TimeoutExpired):
                try:
                    self.preserve_evidence()
                finally:
                    process.kill()
                    process.wait(timeout=5)
                    if process.stdin and not process.stdin.closed: process.stdin.close()
                raise AssertionError("Owned waveform worker did not stop cleanly")
        if process.stdin and not process.stdin.closed: process.stdin.close()

    def events(self, component, event=None, **matches):
        path = self.root / (component + ".log")
        result = []
        for line in path.read_text(encoding="utf-8").splitlines() if path.exists() else []:
            try: item = json.loads(line)
            except ValueError: continue
            if (event is None or item.get("event") == event) and all(item.get(k) == v for k, v in matches.items()):
                result.append(item)
        return result

    def await_condition(self, predicate, label, timeout=20):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            value = predicate()
            if value: return value
            for component, process in self.processes.items():
                if process.poll() not in (None, 0):
                    raise AssertionError(f"{component} exited: {self.events(component)}")
            time.sleep(.05)
        raise AssertionError("Owned stack timeout: " + label + "; events=" + str({name: self.events(name)[-5:] for name in self.processes}))

    def await_event(self, component, event, **matches):
        return self.await_condition(lambda: self.events(component, event, **matches), component + ":" + event)[-1]

    def await_count(self, table, count, role="receiver"):
        return self.await_condition(lambda: self.fixture.scalar("SELECT count(*) FROM waveforms." + table, role=role) == count,
                                    table + ":" + str(count))

    def publish(self, gateway, payload, *, retain=False):
        if gateway not in self.peers:
            peer = Peer(self.broker, "waveform-" + gateway + "-sender")
            self.broker.clients.append(peer)
            self.peers[gateway] = peer
        return self.peers[gateway].publish(f"oil/v2/{gateway}/PUMP-001/waveform", payload, retain=retain)

    def command(self, component, value):
        self.processes[component].stdin.write((value + "\n").encode())
        self.processes[component].stdin.flush()

    def reset_peer(self, gateway):
        peer = self.peers.pop(gateway)
        peer.close()
        self.broker.clients.remove(peer)

    def save_config(self):
        self.config.write_text(toml_text(self.sections), encoding="utf-8")
