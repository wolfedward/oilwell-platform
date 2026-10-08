"""Owned loopback broker and component processes for synthetic WP04 tests only."""
from dataclasses import asdict
from hashlib import sha256
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
    def __init__(self, root, gateways, *, _created=None):
        self.root, self.clients, self.process, self.log = root, [], None, None
        self.instances=[]; self._created=_created
        self.password = secrets.token_urlsafe(24)
        self.executable = Path(shutil.which("mosquitto") or "C:/Program Files/mosquitto/mosquitto.exe")
        if not self.executable.is_file():
            raise RuntimeError("Owned Mosquitto is required")
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            self.port = sock.getsockname()[1]
        if self.port in (1883,5432,3822,8765): raise ValueError('Protected listener port')
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
        self.start()

    def start(self):
        if self.process and self.process.poll() is None:
            raise AssertionError("Owned broker already running")
        instance_id=uuid4().hex
        try:
            self.log = (self.root / "broker.log").open("ab")
            self.process = subprocess.Popen([str(self.executable), "-c", str(self.config)],
                stdout=self.log, stderr=self.log, creationflags=FLAGS)
            self.instances.append(dict(instance_id=instance_id,pid=self.process.pid,started_monotonic=time.monotonic(),
                process=self.process,component='broker',executable=str(self.executable),port=self.port))
            if self._created is not None: self._created(self)
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                if self.process.poll() is not None:
                    raise AssertionError("Owned broker exited")
                try:
                    with socket.create_connection(("127.0.0.1", self.port), timeout=.1): return
                except OSError: time.sleep(.02)
            raise AssertionError("Owned broker startup timeout")
        except BaseException as error:
            try: self.stop()
            except BaseException as cleanup: error.add_note('Broker startup cleanup failure: '+type(cleanup).__name__)
            raise

    def stop(self):
        first=None
        try:
            if self.process and self.process.poll() is None:
                self.process.terminate()
                try: self.process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    self.process.kill(); self.process.wait(timeout=5)
        except BaseException as error:
            first=error
            try:
                if self.process and self.process.poll() is None:
                    self.process.kill(); self.process.wait(timeout=5)
            except BaseException as cleanup: first.add_note('Broker kill fallback failure: '+type(cleanup).__name__)
        try:
            if self.log: self.log.close()
        except BaseException as error:
            if first is None: first=error
            else: first.add_note('Broker log close failure: '+type(error).__name__)
        if first is not None: raise first

    def close(self):
        failures=[]
        for peer in self.clients:
            try: peer.close()
            except BaseException as error: failures.append(error)
        self.clients.clear()
        try: self.stop()
        except BaseException as error: failures.append(error)
        if failures:
            for error in failures[1:]: failures[0].add_note('Later broker cleanup failure: '+type(error).__name__)
            raise failures[0]


class Stack:
    def __init__(self, fixture, *, limits=LIMITS, evidence_root=None, run_id=None, _created=None):
        self.fixture, self.limits = fixture, limits
        self.temp = tempfile.TemporaryDirectory(prefix="wp04-stack-")
        self.root = Path(self.temp.name)
        self.processes, self.handles, self.peers = {}, [], {}
        self.broker = None; self._created=_created
        self.run_id=run_id or getattr(fixture,'task17_run_id',None) or uuid4().hex
        self.evidence_root=Path(evidence_root) if evidence_root is not None else None
        self.instances=[]; self.event_offsets={}
        self.failed=False

    def __enter__(self):
        try:
            self.broker = Broker(self.root, self.limits.gateways,_created=self._broker_created)
            self.sections = sections_for(self.fixture, limits=self.limits, port=self.broker.port)
            self.config = self.root / "waveforms.toml"
            self.config.write_text(toml_text(self.sections), encoding="utf-8")
            return self
        except BaseException as error:
            self.__exit__(type(error),error,error.__traceback__)
            raise

    def _broker_created(self,broker):
        # Publish ownership before a registration refusal can unwind its constructor.
        self.broker=broker
        if self._created is not None: self._created(broker.process)

    def __exit__(self, kind, error, _traceback):
        self.failed=getattr(self,'failed',False) or kind is not None
        try:
            self.close()
        except BaseException as cleanup:
            if error is None: raise
            error.add_note('Owned cleanup failure: '+type(cleanup).__name__)
            for note in getattr(cleanup,'__notes__',()): error.add_note(note)
        if error is not None and getattr(self,'evidence_path',None) is not None:
            error.add_note('Owned failure evidence: '+str(self.evidence_path))

    def preserve_evidence(self,*,failure=False):
        supplied=self.evidence_root
        if failure and supplied is not None and not supplied.resolve().is_relative_to(ROOT): supplied=None
        destination = (supplied or Path(os.environ.get("OILWELL_WAVEFORM_EVIDENCE_DIR",
            str(ROOT / ".superpowers/sdd/2026-09-29-synchronized-waveform-paired-dataset/task-6-failures")))) / uuid4().hex
        destination.mkdir(parents=True, exist_ok=False)
        for path in self.root.glob("*.log"):
            shutil.copyfile(path, destination / path.name)
        processes=[]
        for item in [*self.instances,*(self.broker.instances if self.broker else [])]:
            value={key:data for key,data in item.items() if key!='process'}
            value['run_id']=self.run_id; value['exit_code']=item['process'].poll(); processes.append(value)
        (destination/'processes.json').write_text(json.dumps(processes,indent=2),encoding='utf-8')
        inventory=[]
        for path in destination.iterdir():
            digest=sha256()
            with path.open('rb') as source:
                for block in iter(lambda:source.read(1024*1024),b''): digest.update(block)
            inventory.append(dict(path=path.name,size=path.stat().st_size,sha256=digest.hexdigest()))
        (destination/'evidence-hashes.json').write_text(json.dumps(inventory,indent=2),encoding='utf-8')
        return destination

    def close(self):
        failures=[]; self.cleanup_failures=[]; self.evidence_path=None
        def attempt(stage,action):
            try: return action()
            except BaseException as error:
                failures.append(error); self.cleanup_failures.append(dict(stage=stage,error_type=type(error).__name__))
        for name in self.processes: attempt('worker:'+name,lambda name=name:self.stop(name))
        for index,handle in enumerate(self.handles): attempt('handle:'+str(index),handle.close)
        if self.broker: attempt('broker',self.broker.close)
        if self.failed or failures or self.evidence_root is not None:
            self.evidence_path=attempt('evidence',lambda:self.preserve_evidence(failure=self.failed or bool(failures)))
        attempt('temporary_files',self.temp.cleanup)
        if failures:
            for value in self.cleanup_failures: failures[0].add_note('Owned cleanup '+value['stage']+': '+value['error_type'])
            raise failures[0]

    def launch(self, component, *, fault="", check=False, config=None):
        prior = self.processes.get(component)
        if prior and prior.poll() is None: raise AssertionError("Duplicate owned worker")
        output = (self.root / (component + ".log")).open("ab", buffering=0)
        self.event_offsets[component]=output.tell()
        self.handles.append(output)
        instance_id=uuid4().hex
        env = dict(os.environ, PYTHONUTF8="1", TEST_WAVEFORM_MQTT=self.broker.password,PGAPPNAME='wp04-'+self.run_id+'-'+instance_id)
        executable = sys.executable
        base = getattr(sys, "_base_executable", executable)
        if os.name == "nt" and os.path.normcase(executable) != os.path.normcase(base):
            env["__PYVENV_LAUNCHER__"] = executable
            executable = base
        args = [executable, "-u", "-m", "tests.waveform_worker", component, "--config", str(config or self.config), "--fault", fault]
        if check: args.append("--check")
        self.processes[component] = subprocess.Popen(args, cwd=ROOT, env=env, stdin=subprocess.PIPE,
            stdout=output, stderr=output, creationflags=FLAGS)
        self.instances.append(dict(instance_id=instance_id,run_id=self.run_id,component=component,executable=executable,
            args=args,application_name=env['PGAPPNAME'],
            pid=self.processes[component].pid,started_monotonic=time.monotonic(),process=self.processes[component]))
        if self._created is not None:
            try: self._created(self.processes[component])
            except BaseException as error:
                try:
                    if self.processes[component].poll() is None: self.kill_owned(component,reason='registration_refused')
                except BaseException as cleanup: error.add_note('Worker registration cleanup failure: '+type(cleanup).__name__)
                try:
                    pipe=self.processes[component].stdin
                    if pipe and not pipe.closed: pipe.close()
                except BaseException as cleanup: error.add_note('Worker stdin close failure: '+type(cleanup).__name__)
                try: output.close()
                except BaseException as cleanup: error.add_note('Worker log close failure: '+type(cleanup).__name__)
                raise
        return self.processes[component]

    def kill_owned(self,name,*,reason):
        process=self.processes[name]
        instance=next(item for item in reversed(self.instances) if item['process'] is process)
        if process.poll() is not None: raise AssertionError('Owned fault process already exited')
        began=time.monotonic(); process.kill(); process.wait(timeout=5)
        if process.stdin and not process.stdin.closed: process.stdin.close()
        return dict(instance_id=instance['instance_id'],pid=process.pid,reason=reason,
                    exit_code=process.returncode,duration_seconds=time.monotonic()-began)

    def stop(self, name):
        process = self.processes[name]
        if process.poll() is None:
            try:
                process.stdin.write(b"stop\n")
                process.stdin.flush()
                process.wait(timeout=25)  # Product has one total 20s graceful budget.
            except (OSError, subprocess.TimeoutExpired):
                self.failed=True
                process.kill()
                process.wait(timeout=5)
                if process.stdin and not process.stdin.closed: process.stdin.close()
                raise AssertionError("Owned waveform worker did not stop cleanly")
        if process.stdin and not process.stdin.closed: process.stdin.close()

    def events(self, component, event=None, *, current=False, **matches):
        path = self.root / (component + ".log")
        result = []
        content=path.read_bytes()[self.event_offsets.get(component,0) if current else 0:] if path.exists() else b''
        # The serialized emitter writes a complete line. Ignore only an in-flight
        # final fragment; malformed completed records are evidence failure.
        lines=content.split(b'\n')[:-1]
        for raw in lines:
            line=raw.decode('utf-8').strip()
            if not line: continue
            try: item = json.loads(line)
            except ValueError: raise AssertionError('Malformed completed owned worker event') from None
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
        return self.await_condition(lambda: self.events(component, event, current=True, **matches), component + ":" + event)[-1]

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

    def sender_config(self,gateway='GW'):
        prefix='gateways.'+gateway
        sections={key:value for key,value in self.sections.items()
            if not key.startswith('postgres_') and (not key.startswith('gateways.') or key==prefix or key.startswith(prefix+'.'))}
        path=self.root/('sender-'+gateway+'.toml'); path.write_text(toml_text(sections),encoding='utf-8')
        return path
