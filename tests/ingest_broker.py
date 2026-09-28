"""Own-PID, loopback-only Mosquitto harness. Never alters installed services."""
import os
from pathlib import Path
import queue
import secrets
import shutil
import socket
import subprocess
import tempfile
import threading
import time
from uuid import uuid4

import paho.mqtt.client as mqtt
from paho.mqtt.subscribeoptions import SubscribeOptions

ROOT = Path(__file__).resolve().parents[1]
FLAGS = subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0


class Broker:
    def __init__(self, *, message_size_limit=None):
        executable = shutil.which('mosquitto') or 'C:/Program Files/mosquitto/mosquitto.exe'
        self.executable = Path(executable)
        if not self.executable.is_file():
            raise RuntimeError('Mosquitto is required; real broker tests cannot be skipped')
        self.temporary = tempfile.TemporaryDirectory(prefix='wp03-broker-')
        self.root = Path(self.temporary.name)
        self.process = None
        self.log = None
        self.clients = []
        self.password = secrets.token_urlsafe(24)  # Generated only for this loopback fixture.
        self.config = self.root / 'broker.conf'
        with socket.socket() as reserved:
            reserved.bind(('127.0.0.1', 0))
            self.port = reserved.getsockname()[1]
        try:
            users = ('gateway-GW-001', 'gateway-GW-002', 'platform-ingestor', 'platform-receipt-publisher')
            passwords = self.root / 'passwords'
            passwd = self.executable.with_name('mosquitto_passwd.exe' if os.name == 'nt' else 'mosquitto_passwd')
            for index, user in enumerate(users):
                result = subprocess.run([str(passwd), '-b', *(['-c'] if index == 0 else []),
                                         str(passwords), user, self.password],
                                        capture_output=True, timeout=10, creationflags=FLAGS)
                if result.returncode != 0:
                    raise RuntimeError('could not initialize isolated broker credentials')
            acl = (ROOT / 'config/mosquitto.acl.example').read_text(encoding='utf-8')
            # A second provisioned gateway copies the first gateway's actual policy.
            first = acl.split('user gateway-GW-001', 1)[1].split('\nuser ', 1)[0]
            acl += '\nuser gateway-GW-002\n' + first.replace('GW-001', 'GW-002')
            (self.root / 'acl').write_text(acl, encoding='utf-8')
            lines = (ROOT / 'config/mosquitto.example.conf').read_text(encoding='utf-8').splitlines()
            replace_keys = {'listener', 'allow_anonymous', 'password_file', 'acl_file',
                            'cafile', 'certfile', 'keyfile', 'tls_version', 'persistence', 'persistence_location'}
            if message_size_limit is not None:
                replace_keys.add('message_size_limit')
            actual = [line for line in lines if not line.strip() or line.lstrip().startswith('#')
                      or line.split()[0] not in replace_keys]
            actual += [f'listener {self.port} 127.0.0.1', 'allow_anonymous false',
                       f'password_file {passwords.as_posix()}', f'acl_file {(self.root / "acl").as_posix()}',
                       'persistence false']
            if message_size_limit is not None:
                actual.append(f'message_size_limit {message_size_limit}')
            self.config.write_text('\n'.join(actual) + '\n', encoding='utf-8')
            self.start()
        except BaseException:
            self.close()
            raise

    def start(self):
        if self.process is not None and self.process.poll() is None:
            raise RuntimeError('test broker already running')
        self.log = (self.root / 'broker.log').open('ab')
        self.process = subprocess.Popen([str(self.executable), '-c', str(self.config)],
                                        stdout=self.log, stderr=self.log, creationflags=FLAGS)
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if self.process.poll() is not None:
                raise RuntimeError('isolated broker exited: ' + (self.root / 'broker.log').read_text(errors='replace')[-2000:])
            try:
                with socket.create_connection(('127.0.0.1', self.port), timeout=.1):
                    return
            except OSError:
                threading.Event().wait(.02)
        raise RuntimeError('isolated broker startup timeout')

    def client(self, user, *, protocol=mqtt.MQTTv5):
        client = Peer(self, user, protocol=protocol)
        self.clients.append(client)
        return client

    def stop(self, *, kill=False):
        process = self.process
        if process is not None and process.poll() is None:
            (process.kill if kill else process.terminate)()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
        if self.log is not None:
            self.log.close()
            self.log = None

    def close(self):
        for client in self.clients:
            client.close()
        self.clients.clear()
        self.stop()
        self.temporary.cleanup()


class Peer:
    def __init__(self, broker, user, *, protocol=mqtt.MQTTv5):
        self.messages = queue.Queue()
        self.published = queue.Queue()
        self.subscribed = queue.Queue()
        self.connected = threading.Event()
        self.disconnected = threading.Event()
        self.reason = None
        self.properties = None
        self.client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id='wp03-test-' + uuid4().hex,
                                  protocol=protocol, reconnect_on_failure=False)
        self.client.username_pw_set(user, broker.password)
        self.client.on_connect = self._connected
        self.client.on_subscribe = lambda _c, _u, mid, codes, _p: self.subscribed.put((mid, codes))
        self.client.on_publish = lambda _c, _u, mid, reason, _p: self.published.put((mid, reason.value))
        self.client.on_message = lambda _c, _u, message: self.messages.put(message)
        self.client.on_disconnect = lambda *_a: self.disconnected.set()
        try:
            self.client.connect('127.0.0.1', broker.port, keepalive=20)
            self.client.loop_start()
            if not self.connected.wait(5) or self.reason != 0:
                raise RuntimeError('test MQTT connection not accepted')
        except BaseException:
            self.close()
            raise

    def _connected(self, _client, _userdata, _flags, reason, properties):
        self.reason, self.properties = reason.value, properties
        self.connected.set()

    def subscribe(self, topic, *, retain_as_published=True):
        result, mid = self.client.subscribe(topic, options=SubscribeOptions(qos=1, retainAsPublished=retain_as_published))
        if result != mqtt.MQTT_ERR_SUCCESS:
            raise RuntimeError('subscription send failed')
        received, reasons = self.subscribed.get(timeout=5)
        if received != mid:
            raise RuntimeError('unexpected subscription response')
        return [reason.value for reason in reasons]

    def publish(self, topic, payload, *, retain=False):
        info = self.client.publish(topic, payload, qos=1, retain=retain)
        if info.rc != mqtt.MQTT_ERR_SUCCESS:
            raise RuntimeError('publish not queued')
        mid, reason = self.published.get(timeout=5)
        if mid != info.mid:
            raise RuntimeError('unexpected publication response')
        return reason

    def close(self):
        self.client.disconnect()
        self.client.loop_stop()


def raw_publish_response(broker, topic, payload):
    """Capture one actual v5 PUBACK without a SDK interpreting its reason code."""
    def variable(value):
        data = bytearray()
        while True:
            digit = value % 128
            value //= 128
            data.append(digit | (128 if value else 0))
            if not value:
                return bytes(data)

    def string(value):
        value = value.encode('utf-8')
        return len(value).to_bytes(2, 'big') + value

    def packet(stream):
        def exact(size):
            result = b''
            while len(result) < size:
                chunk = stream.recv(size - len(result))
                if not chunk:
                    raise RuntimeError('MQTT socket closed before full response')
                result += chunk
            return result
        header, size, shift = exact(1)[0], 0, 0
        for _ in range(4):
            digit = exact(1)[0]
            size += (digit & 127) << shift
            if not digit & 128:
                if size > 65536:
                    raise RuntimeError('unexpected large MQTT response')
                return header, exact(size)
            shift += 7
        raise RuntimeError('malformed MQTT remaining length')

    with socket.create_connection(('127.0.0.1', broker.port), timeout=5) as stream:
        connect = (string('MQTT') + b'\x05\xc2\x00\x14\x00' + string('raw-' + uuid4().hex)
                   + string('gateway-GW-001') + string(broker.password))
        stream.sendall(b'\x10' + variable(len(connect)) + connect)
        header, body = packet(stream)
        if header != 0x20 or len(body) < 2 or body[1] != 0:
            raise RuntimeError('raw MQTT fixture connection not accepted')
        data = string(topic) + b'\x00\x01\x00' + payload
        stream.sendall(b'\x32' + variable(len(data)) + data)
        return packet(stream)
