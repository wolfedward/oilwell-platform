"""Asynchronous gateway delivery, independent of the Modbus polling loop."""
import math
import ssl
import threading
import time

from backend.ingest_reliability.contracts import valid_name, valid_prefix
from backend.ingest_reliability.errors import ReliabilityError
from .mqtt_uplink import MqttConfig


def _error(code):
    return ReliabilityError(code, '网关上行尚未确认完成；本地原文继续保留')


def _number(value):
    number = getattr(value, 'value', value)
    return number if type(number) is int else None


def _client(config):
    import paho.mqtt.client as mqtt
    return mqtt.Client(callback_api_version=mqtt.CallbackAPIVersion.VERSION2, client_id=config.client_id,
                       protocol=mqtt.MQTTv5, reconnect_on_failure=False, manual_ack=True)


class ReliableUplink:
    def __init__(self, mqtt_config, spool, *, gateway_id, prefix, client_factory=None):
        if (not isinstance(mqtt_config, MqttConfig) or not valid_name(gateway_id) or not valid_prefix(prefix)
                or any(not callable(getattr(spool, name, None)) for name in ('claim', 'broker_ack', 'fail', 'apply_receipt', 'counts'))):
            raise _error('ingest.uplink_config_invalid')
        for value in (mqtt_config.connect_timeout_seconds, mqtt_config.publish_timeout_seconds):
            if type(value) not in (int, float) or not .001 <= value <= 60 or not math.isfinite(value):
                raise _error('ingest.uplink_config_invalid')
        self.config = mqtt_config
        self.spool = spool
        self.gateway_id, self.prefix = gateway_id, prefix
        self.client_factory = client_factory or _client
        self.lease_seconds = max(60, math.ceil(mqtt_config.publish_timeout_seconds + 3))
        self._condition = threading.Condition()
        self._wake, self._stop = threading.Event(), threading.Event()
        self._thread = None
        self._client = None
        self._epoch = self._passes = self._published = 0
        self._in_pass = False
        self._looping = self._connected = self._ready = False
        self._publishing = self._subscribing = False
        self._net_error = self._error_code = None
        self._pub_results, self._sub_results = {}, {}

    def start(self):
        with self._condition:
            if self._stop.is_set():
                raise _error('ingest.uplink_closed')
            if self._thread is not None:
                return
            thread = threading.Thread(target=self._run, name='oilwell-uplink-' + self.gateway_id, daemon=True)
            thread.start()
            self._thread = thread

    def wake(self):
        self._wake.set()

    def snapshot(self):
        status = self.spool.status()
        with self._condition:
            result = {'running': bool(self._thread and self._thread.is_alive()), 'ready': self._ready,
                      'error_code': self._error_code, 'broker_published_since_last_snapshot': self._published,
                      'spool': status.pop('counts'), 'queue_status': status}
            self._published = 0
            return result

    def wait_once(self, *, timeout=None):
        timeout = self._timeout(timeout)
        with self._condition:
            # A pass already in progress may have read an empty queue before
            # the caller enqueued new data. Require one fresh pass after it.
            target = self._passes + 1 + int(self._in_pass)
            self.wake()
            deadline = time.monotonic() + timeout
            while self._passes < target:
                if self._stop.is_set() or self._thread is None:
                    return False
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self._condition.wait(remaining)
            return True

    def close(self, *, timeout=None):
        timeout = self._timeout(timeout)
        self._stop.set()
        self.wake()
        with self._condition:
            self._condition.notify_all()
            thread = self._thread
        if thread is not None:
            if threading.current_thread() is thread:
                raise _error('ingest.uplink_close_from_worker')
            thread.join(timeout)
            if thread.is_alive():
                raise _error('ingest.uplink_close_timeout')

    def _timeout(self, timeout):
        if timeout is None:
            return 2 * self.config.connect_timeout_seconds + self.config.publish_timeout_seconds + 2
        if type(timeout) not in (float, int) or not 0 < timeout <= 300 or not math.isfinite(timeout):
            raise _error('ingest.uplink_input_invalid')
        return float(timeout)

    def _current(self, client, epoch):
        return not self._stop.is_set() and self._client is client and self._epoch == epoch

    def _network_failure(self, client, epoch, code):
        with self._condition:
            if not self._current(client, epoch):
                return
            if self._net_error is None:
                self._net_error = code
            self._error_code, self._ready = self._net_error, False
            self._condition.notify_all()
        try:
            client.disconnect()
        except OSError:
            pass
        self.wake()

    def _wait(self, predicate, timeout):
        deadline = time.monotonic() + timeout
        with self._condition:
            while True:
                if self._stop.is_set():
                    raise _error('ingest.uplink_closed')
                if self._net_error:
                    raise _error(self._net_error)
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise _error('mqtt.response_timeout')
                if predicate():
                    return
                self._condition.wait(remaining)

    def _open(self):
        import paho.mqtt.client as mqtt
        client = self.client_factory(self.config)
        with self._condition:
            self._epoch += 1
            epoch = self._epoch
            self._client = client
            self._connected = self._ready = False
            self._net_error = None
            self._pub_results.clear()
            self._sub_results.clear()

        def connected(source, _data, _flags, code, _properties=None):
            with self._condition:
                if self._current(source, epoch):
                    self._connected = _number(code) == 0
                    if not self._connected:
                        self._net_error = 'mqtt.connection_rejected'
                    self._condition.notify_all()

        def disconnected(source, _data, _flags, _code, _properties=None):
            with self._condition:
                if self._current(source, epoch):
                    self._ready = self._connected = False
                    self._net_error = self._net_error or 'mqtt.disconnected'
                    self._error_code = self._net_error
                    self._condition.notify_all()
            self.wake()

        def subscribed(source, _data, mid, codes, _properties=None):
            with self._condition:
                if not self._current(source, epoch) or not self._subscribing:
                    return
                if type(mid) is not int or not 1 <= mid <= 65535 or not isinstance(codes, (list, tuple)):
                    self._net_error = 'mqtt.invalid_suback'
                elif len(self._sub_results) >= 8 and (epoch, mid) not in self._sub_results:
                    self._net_error = 'mqtt.callback_overflow'
                else:
                    self._sub_results[(epoch, mid)] = tuple(_number(code) for code in codes)
                self._condition.notify_all()

        def published(source, _data, mid, code, _properties=None):
            with self._condition:
                if not self._current(source, epoch) or not self._publishing:
                    return
                if type(mid) is not int or not 1 <= mid <= 65535:
                    self._net_error = 'mqtt.invalid_puback'
                elif len(self._pub_results) >= 64 and (epoch, mid) not in self._pub_results:
                    self._net_error = 'mqtt.callback_overflow'
                else:
                    self._pub_results[(epoch, mid)] = _number(code)
                self._condition.notify_all()

        def received(source, _data, message):
            with self._condition:
                if not self._current(source, epoch):
                    return
            try:
                if (type(message.qos) is not int or message.qos not in (0, 1)
                        or (message.qos == 1 and (type(message.mid) is not int or not 1 <= message.mid <= 65535))):
                    raise _error('mqtt.receipt_transport_invalid')
                self.spool.apply_receipt(message.topic, message.payload, retain=message.retain)
                with self._condition:
                    if not self._current(source, epoch):
                        return
                if message.qos == 1 and source.ack(message.mid, message.qos) not in (0, None):
                    raise _error('mqtt.receipt_ack_failed')
            except Exception as error:
                code = error.code if isinstance(error, ReliabilityError) else 'ingest.receipt_internal_error'
                self._network_failure(source, epoch, code)
            self.wake()

        client.on_connect, client.on_disconnect = connected, disconnected
        client.on_subscribe, client.on_publish, client.on_message = subscribed, published, received
        client.connect_timeout = self.config.connect_timeout_seconds
        client.manual_ack_set(True)
        client.max_inflight_messages_set(20)
        client.max_queued_messages_set(20)
        if self.config.username:
            client.username_pw_set(self.config.username, self.config.password)
        if self.config.tls_enabled:
            client.tls_set(ca_certs=self.config.ca_cert, certfile=self.config.client_cert, keyfile=self.config.client_key,
                           cert_reqs=ssl.CERT_REQUIRED, tls_version=ssl.PROTOCOL_TLS_CLIENT)
        properties = mqtt.Properties(mqtt.PacketTypes.CONNECT)
        properties.ReceiveMaximum = 20
        properties.MaximumPacketSize = 8192
        deadline = time.monotonic() + self.config.connect_timeout_seconds
        try:
            if client.connect(self.config.host, self.config.port, self.config.keepalive_seconds,
                              clean_start=True, properties=properties) not in (0, None):
                raise _error('mqtt.connect_failed')
            with self._condition:
                if not self._current(client, epoch):
                    raise _error('ingest.uplink_closed')
                self._looping = True
                if client.loop_start() not in (0, None):
                    raise _error('mqtt.loop_failed')
        except OSError:
            raise _error('mqtt.connect_failed') from None
        self._wait(lambda: self._connected, deadline - time.monotonic())
        with self._condition:
            self._subscribing = True
        deadline = time.monotonic() + self.config.connect_timeout_seconds
        result, mid = client.subscribe(f'{self.prefix}/v2/{self.gateway_id}/+/ingest-ack',
                                       options=mqtt.SubscribeOptions(qos=1, retainAsPublished=True))
        if result != 0 or type(mid) is not int or not 1 <= mid <= 65535:
            raise _error('mqtt.subscription_rejected')
        self._wait(lambda: (epoch, mid) in self._sub_results, deadline - time.monotonic())
        with self._condition:
            codes = self._sub_results.pop((epoch, mid))
            self._subscribing = False
            self._sub_results.clear()
            if codes != (1,):
                raise _error('mqtt.subscription_rejected')
            self._ready = True

    def _publish(self, lease):
        with self._condition:
            client, epoch = self._client, self._epoch
            self._pub_results.clear()
            self._publishing = True
        try:
            deadline = time.monotonic() + self.config.publish_timeout_seconds
            try:
                info = client.publish(lease.topic, lease.payload, qos=lease.qos, retain=False)
            except OSError:
                raise _error('mqtt.publish_failed') from None
            mid = getattr(info, 'mid', None)
            if getattr(info, 'rc', None) != 0 or type(mid) is not int or not 1 <= mid <= 65535:
                raise _error('mqtt.publish_rejected')
            self._wait(lambda: (epoch, mid) in self._pub_results, deadline - time.monotonic())
            with self._condition:
                code = self._pub_results.pop((epoch, mid))
            if code != 0:
                raise _error('mqtt.no_matching_subscribers' if code == 16 else 'mqtt.publish_rejected')
        except ReliabilityError as error:
            if not self._stop.is_set():
                self.spool.fail(lease, error.code)
            raise
        finally:
            with self._condition:
                self._publishing = False
                self._pub_results.clear()
        if self._stop.is_set():
            return
        # A failed/uncertain local ACK commit must not be followed by a second
        # failure write. A matching early application receipt may already have
        # finalized this same record before its transport callback arrived.
        if not self.spool.broker_ack(lease):
            row = self.spool.get(lease.message_id)
            if row is None or row['confirmation_mode'] != 'application' or row['state'] not in ('stored', 'quarantined'):
                raise _error('ingest.lease_stale')
        with self._condition:
            self._published += 1
            self._error_code = None

    def _detach(self):
        with self._condition:
            client, looping = self._client, self._looping
            self._epoch += 1
            self._client = None
            self._looping = self._ready = self._connected = False
            self._publishing = self._subscribing = False
            self._pub_results.clear()
            self._sub_results.clear()
            self._condition.notify_all()
        if client is not None:
            try:
                client.disconnect()
            except OSError:
                pass
            finally:
                if looping:
                    client.loop_stop()

    def _run(self):
        failures = 0
        try:
            while not self._stop.is_set():
                with self._condition:
                    self._in_pass = True
                active = False
                try:
                    if not self._ready:
                        if self._client is not None:
                            self._detach()
                        self._open()
                    leases = self.spool.claim(limit=1, lease_seconds=self.lease_seconds)
                    if leases:
                        active = True
                        self._publish(leases[0])
                    failures = 0
                except Exception as error:
                    if self._stop.is_set():
                        break
                    code = error.code if isinstance(error, ReliabilityError) else 'ingest.uplink_internal_error'
                    with self._condition:
                        self._error_code = code
                    self._detach()
                    failures = min(failures + 1, 16)
                finally:
                    with self._condition:
                        self._passes += 1
                        self._in_pass = False
                        self._condition.notify_all()
                if failures:
                    delay = min(self.spool.max_backoff_seconds, self.spool.base_backoff_seconds * 2 ** (failures - 1))
                    self._stop.wait(delay)
                elif not active:
                    self._wake.wait(.1)
                    self._wake.clear()
        finally:
            self._detach()
