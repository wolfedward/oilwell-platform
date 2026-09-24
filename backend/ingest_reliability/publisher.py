"""Independent receipt publishing; a PUBACK is Broker confirmation only."""
import math
import ssl
import threading
import time

from backend.gateway.mqtt_uplink import MqttConfig
from .errors import ReliabilityError


def _failure(code):
    return ReliabilityError(code, '回执发布尚未确认完成；持久记录已保留，请检查发送器状态')


def _client(config):
    try:
        import paho.mqtt.client as mqtt
    except ImportError:
        raise _failure('mqtt.dependency_missing') from None
    return mqtt.Client(callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
                       client_id=config.client_id, protocol=mqtt.MQTTv5, reconnect_on_failure=False)


def _reason(code):
    value = getattr(code, 'value', code)
    return value if type(value) is int else None


class ReceiptPublisher:
    def __init__(self, repository, mqtt_config, *, client_factory=None, batch_size=20, lease_seconds=30):
        if not isinstance(mqtt_config, MqttConfig) or any(not callable(getattr(repository, name, None))
                                                       for name in ('claim', 'finish', 'heartbeat')):
            raise ValueError('publisher requires configured MQTT and a durable outbox boundary')
        if (type(batch_size) is not int or not 1 <= batch_size <= 200 or type(lease_seconds) is not int
                or not 1 <= lease_seconds <= 300):
            raise _failure('ingest.publisher_config_invalid')
        for value in (mqtt_config.connect_timeout_seconds, mqtt_config.publish_timeout_seconds):
            if type(value) not in (int, float) or not math.isfinite(value) or not 0 < value <= 60:
                raise _failure('ingest.publisher_config_invalid')
        if lease_seconds <= mqtt_config.publish_timeout_seconds + 3:
            raise _failure('ingest.publisher_config_invalid')
        self.repository = repository
        self.config = mqtt_config
        self.client_factory = client_factory or _client
        self.batch_size, self.lease_seconds = batch_size, lease_seconds
        self._condition = threading.Condition()
        self._run_lock = threading.Lock()
        self._connack = threading.Event()
        self._closed = False
        self._epoch = 0
        self._client = None
        self._loop_started = False
        self._connected = False
        self._network_error = None
        self._active_publish = False
        self._results = {}

    def _current(self, client, epoch):
        return not self._closed and self._client is client and self._epoch == epoch

    def _open(self):
        client = self.client_factory(self.config)
        with self._condition:
            if self._closed:
                raise _failure('ingest.publisher_closed')
            self._epoch += 1
            epoch = self._epoch
            self._client = client
            self._connected = False
            self._network_error = None
            self._connack.clear()
            self._results.clear()

        def connected(source, _userdata, _flags, reason, _properties=None):
            with self._condition:
                if self._current(source, epoch):
                    self._connected = _reason(reason) == 0
                    if not self._connected:
                        self._network_error = 'mqtt.connection_rejected'
                    self._connack.set()
                    self._condition.notify_all()

        def disconnected(source, _userdata, _flags, _reason_code, _properties=None):
            with self._condition:
                if self._current(source, epoch):
                    self._connected = False
                    self._network_error = 'mqtt.disconnected'
                    self._connack.set()
                    self._condition.notify_all()

        def published(source, _userdata, mid, reason, _properties=None):
            with self._condition:
                if not self._current(source, epoch) or not self._active_publish or self._network_error:
                    return
                if type(mid) is not int or not 1 <= mid <= 65535:
                    self._network_error = 'mqtt.invalid_ack'
                elif len(self._results) >= 64 and (epoch, mid) not in self._results:
                    self._network_error = 'mqtt.callback_overflow'
                else:
                    self._results[(epoch, mid)] = _reason(reason)
                self._condition.notify_all()

        client.on_connect, client.on_disconnect, client.on_publish = connected, disconnected, published
        client.connect_timeout = self.config.connect_timeout_seconds
        if self.config.username:
            client.username_pw_set(self.config.username, self.config.password)
        if self.config.tls_enabled:
            try:
                client.tls_set(ca_certs=self.config.ca_cert, certfile=self.config.client_cert,
                               keyfile=self.config.client_key, cert_reqs=ssl.CERT_REQUIRED,
                               tls_version=ssl.PROTOCOL_TLS_CLIENT)
            except (OSError, ValueError):
                raise _failure('mqtt.tls_invalid') from None
        deadline = time.monotonic() + self.config.connect_timeout_seconds
        try:
            if client.connect(self.config.host, self.config.port, self.config.keepalive_seconds,
                              clean_start=True) not in (0, None):
                raise _failure('mqtt.connect_failed')
            # close() can race the blocking connect call. Dispose the local
            # late connection even if close already detached it from self.
            with self._condition:
                cancelled = not self._current(client, epoch)
                if not cancelled:
                    self._loop_started = True
                    if client.loop_start() not in (0, None):
                        raise _failure('mqtt.loop_failed')
            if cancelled:
                client.disconnect()
                raise _failure('ingest.publisher_closed')
        except OSError:
            raise _failure('mqtt.connect_failed') from None
        remaining = deadline - time.monotonic()
        if remaining <= 0 or not self._connack.wait(remaining):
            raise _failure('mqtt.connack_timeout')
        with self._condition:
            if self._closed:
                raise _failure('ingest.publisher_closed')
            if not self._connected or self._network_error:
                raise _failure(self._network_error or 'mqtt.connection_rejected')

    def _publish_and_wait(self, lease):
        deadline = time.monotonic() + self.config.publish_timeout_seconds
        with self._condition:
            if self._closed:
                raise _failure('ingest.publisher_closed')
            client, epoch = self._client, self._epoch
            if not self._connected or self._network_error:
                raise _failure(self._network_error or 'mqtt.disconnected')
            self._results.clear()
            self._active_publish = True
        try:
            try:
                info = client.publish(lease.topic, lease.payload, qos=1, retain=False)
            except OSError:
                raise _failure('mqtt.publish_failed') from None
            if getattr(info, 'rc', None) != 0:
                raise _failure('mqtt.publish_rejected')
            mid = getattr(info, 'mid', None)
            if type(mid) is not int or not 1 <= mid <= 65535:
                raise _failure('mqtt.invalid_publish_result')
            with self._condition:
                while True:
                    if self._closed or self._epoch != epoch:
                        raise _failure('ingest.publisher_closed')
                    if self._network_error:
                        raise _failure(self._network_error)
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise _failure('mqtt.puback_timeout')
                    if (epoch, mid) in self._results:
                        code = self._results.pop((epoch, mid))
                        if code == 0:
                            return
                        raise _failure('mqtt.no_matching_subscribers' if code == 16 else 'mqtt.publish_rejected')
                    self._condition.wait(remaining)
        finally:
            with self._condition:
                self._active_publish = False
                self._results.clear()

    def _cleanup(self):
        with self._condition:
            client, looping = self._client, self._loop_started
            self._client, self._loop_started = None, False
            self._epoch += 1
            self._connected = self._active_publish = False
            self._results.clear()
            self._condition.notify_all()
        if client is not None:
            try:
                client.disconnect()
            except OSError:
                pass
            finally:
                if looping:
                    client.loop_stop()

    def run_once(self):
        if not self._run_lock.acquire(blocking=False):
            raise _failure('ingest.publisher_busy')
        counts = {'claimed': 0, 'published': 0, 'failed': 0}
        try:
            if self._closed:
                raise _failure('ingest.publisher_closed')
            self.repository.heartbeat()
            try:
                self._open()
            except ReliabilityError as error:
                if not self._closed:
                    self.repository.heartbeat(success=False, error_code=error.code)
                counts['failed'] += 1
                return counts
            for _index in range(self.batch_size):
                if self._closed:
                    break
                rows = self.repository.claim(limit=1, lease_seconds=self.lease_seconds)
                if not rows:
                    self.repository.heartbeat(success=True)
                    break
                lease = rows[0]
                counts['claimed'] += 1
                try:
                    self._publish_and_wait(lease)
                except ReliabilityError as error:
                    counts['failed'] += 1
                    if not self._closed:
                        self.repository.finish(lease, success=False, error_code=error.code)
                        self.repository.heartbeat(success=False, error_code=error.code)
                    break
                if self._closed:
                    counts['failed'] += 1
                    break
                # Do not turn a commit-uncertain success write into a second
                # failure write. Let the caller report it; the lease recovers.
                if not self.repository.finish(lease, success=True):
                    counts['failed'] += 1
                    self.repository.heartbeat(success=False, error_code='ingest.lease_stale')
                    break
                counts['published'] += 1
                self.repository.heartbeat(success=True)
            return counts
        finally:
            try:
                self._cleanup()
            finally:
                self._run_lock.release()

    def close(self):
        with self._condition:
            self._closed = True
            self._connack.set()
            self._condition.notify_all()
        self._cleanup()
