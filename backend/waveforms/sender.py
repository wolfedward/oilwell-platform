"""Local waveform sender entry point; requires no PostgreSQL runtime or account."""
import argparse
import json
import signal
import ssl
import threading
import time

from .config import load_waveform_config
from .errors import ContractError, WaveformError
from .sender_paths import failure
from .sender_spool import WaveSenderSpool


class _TransportUnavailable(Exception):
    pass


def _client(gateway):
    import paho.mqtt.client as mqtt
    config = gateway.mqtt
    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=config.client_id,
        protocol=mqtt.MQTTv5, reconnect_on_failure=False, manual_ack=True)
    client.connect_timeout = config.connect_timeout_seconds
    if config.username:
        client.username_pw_set(config.username, config.password)
    if config.tls_enabled:
        client.tls_set(ca_certs=config.ca_cert, certfile=config.client_cert, keyfile=config.client_key,
            cert_reqs=ssl.CERT_REQUIRED, tls_version=ssl.PROTOCOL_TLS_CLIENT)
    return client


def _dispose(client):
    """Paho 2.1 has no public close; verify all three owned descriptors."""
    sockets = (client.socket(), client._sockpairR, client._sockpairW)
    try:
        try:
            client.disconnect()
        finally:
            client._reset_sockets()
        if (any(sock is not None and sock.fileno() != -1 for sock in sockets)
                or client.socket() is not None or client._sockpairR is not None or client._sockpairW is not None):
            raise failure('shutdown_incomplete')
    except Exception:
        raise failure('shutdown_incomplete') from None
    finally:
        client.on_connect = client.on_disconnect = client.on_subscribe = client.on_message = client.on_publish = None


class WaveSender:
    """One owner drives Paho's loop synchronously; no background DB/network thread.

    Each attempt uses a fresh client with the same positive-expiry session. Retry
    scheduling uses monotonic time; wire receipts have no local age/session epoch.
    """
    def __init__(self, spool, gateway, *, stop=None, client_factory=None, ack_timeout=10):
        if gateway.gateway_id != spool.gateway_id or not 3600 <= gateway.session_expiry_seconds <= 2592000:
            raise failure('binding_mismatch')
        if not 0 < ack_timeout <= 30:
            raise ValueError('invalid acknowledgement timeout')
        self.spool, self.gateway = spool, gateway
        self.stop = stop if stop is not None else threading.Event()
        self._factory = client_factory or _client
        self._ack_timeout = ack_timeout
        self._run_lock = threading.Lock()
        self._active = None
        self._closed = False

    def close(self):
        self._closed = True
        self._active = None
        self.stop.set()  # The run owner disposes its own socket in finally.

    def run_once(self):
        import paho.mqtt.client as mqtt
        from paho.mqtt.packettypes import PacketTypes
        from paho.mqtt.properties import Properties
        from paho.mqtt.subscribeoptions import SubscribeOptions
        if not self._run_lock.acquire(blocking=False):
            raise failure('busy')
        client, accepted = None, 0
        token = object()
        try:
            if self._closed or self.stop.is_set():
                return 0
            self._active = token
            client = self._factory(self.gateway)
            state = {'ready': False, 'error': False, 'submid': None,
                     'mid': None, 'publishing': False, 'early': {}, 'ack': None}

            def current(callback_client):
                return callback_client is client and self._active is token and not self.stop.is_set()

            def on_connect(c, _u, _f, reason, _p):
                if not current(c): return
                if reason.value != 0:
                    state['error'] = True
                    return
                result, mid = c.subscribe(f'{self.spool.topic_prefix}/v2/{self.gateway.gateway_id}/+/waveform-ack',
                    options=SubscribeOptions(qos=1, retainAsPublished=True, retainHandling=2))
                state['submid'] = mid
                state['error'] = result != mqtt.MQTT_ERR_SUCCESS

            def on_subscribe(c, _u, mid, reasons, _p):
                if current(c) and mid == state['submid']:
                    state['ready'] = len(reasons) == 1 and reasons[0].value == 1
                    state['error'] = not state['ready']

            def on_disconnect(c, *_args):
                if current(c): state['error'] = True

            def on_message(c, _u, message):
                if not current(c): return
                if message.qos == 1 and not message.retain:
                    self.spool.apply_receipt(message.topic, message.payload)
                # Persist first. A storage failure propagates and leaves this MQTT
                # delivery unacknowledged for the persistent session to replay.
                if message.qos == 1:
                    if c.ack(message.mid, 1) != mqtt.MQTT_ERR_SUCCESS:
                        state['error'] = True

            def on_publish(c, _u, mid, reason, _p):
                if not current(c): return
                if state['publishing']:
                    if len(state['early']) >= 16:
                        state['error'] = True
                    else:
                        state['early'][mid] = reason.value
                elif mid == state['mid']:
                    state['ack'] = reason.value

            client.on_connect, client.on_subscribe = on_connect, on_subscribe
            client.on_disconnect, client.on_message, client.on_publish = on_disconnect, on_message, on_publish
            properties = Properties(PacketTypes.CONNECT)
            properties.SessionExpiryInterval = self.gateway.session_expiry_seconds
            properties.ReceiveMaximum = 1
            properties.MaximumPacketSize = 8192

            def pump_until(predicate, timeout):
                deadline = time.monotonic() + timeout
                while not predicate():
                    if self.stop.is_set() or self._active is not token or state['error'] or time.monotonic() >= deadline:
                        raise _TransportUnavailable()
                    if client.loop(timeout=min(.1, max(0, deadline - time.monotonic()))) != mqtt.MQTT_ERR_SUCCESS:
                        raise _TransportUnavailable()

            config = self.gateway.mqtt
            if client.connect(config.host, config.port, config.keepalive_seconds,
                    clean_start=False, properties=properties) != mqtt.MQTT_ERR_SUCCESS:
                raise _TransportUnavailable()
            pump_until(lambda: state['ready'], config.connect_timeout_seconds)
            # Drain queued receipts before preparing a replay; complete can arrive
            # in any message order and remains valid across reconnects.
            client.loop(timeout=.05)
            if self.stop.is_set() or state['error'] or self._active is not token:
                raise _TransportUnavailable()
            for message in self.spool.prepare_batch():
                if self.stop.is_set() or state['error'] or self._active is not token:
                    raise _TransportUnavailable()
                state.update(mid=None, ack=None, publishing=True, early={})
                info = client.publish(message.topic, message.payload, qos=1, retain=False)
                state['publishing'] = False
                if info.rc != mqtt.MQTT_ERR_SUCCESS:
                    raise _TransportUnavailable()
                state['mid'] = info.mid
                state['ack'] = state['early'].get(info.mid)
                state['early'].clear()
                pump_until(lambda: state['ack'] is not None, self._ack_timeout)
                if state['ack'] in (0, 16) and not state['error'] and self._active is token:
                    accepted += int(self.spool.record_broker_ack(message, state['ack']))
                else:
                    raise _TransportUnavailable()
            client.loop(timeout=.1)
            return accepted
        except (OSError, _TransportUnavailable):
            return accepted
        finally:
            self._active = None
            try:
                if client is not None:
                    try:
                        _dispose(client)
                    except WaveformError:
                        self._closed = True
                        raise
            finally:
                self._run_lock.release()


def run_sender(spool, gateway, stop):
    sender = WaveSender(spool, gateway, stop=stop)
    try:
        while not stop.is_set():
            sender.run_once()
            stop.wait(2)
    finally:
        sender.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description='Dedicated Windows local NTFS waveform sender (explicit init required)')
    parser.add_argument('command', choices=('init', 'enqueue', 'run', 'status'))
    parser.add_argument('--config', required=True)
    parser.add_argument('--input')
    args = parser.parse_args(argv)
    if (args.command == 'enqueue') != (args.input is not None):
        parser.error('--input is required only for enqueue')
    try:
        config = load_waveform_config(args.config, component='sender')
        if config is None:
            print(json.dumps({'status': 'disabled'}))
            return 0
        local, gateway = config.sender_spool, config.gateways[0]
        if args.command == 'init':
            with WaveSenderSpool.initialize(local.path, max_bytes=local.max_bytes) as spool:
                spool.bind(gateway.gateway_id, config.topic_prefix)
                result = {'status': 'initialized'}
        else:
            with WaveSenderSpool.open(local.path, max_bytes=local.max_bytes,
                    gateway_id=gateway.gateway_id, topic_prefix=config.topic_prefix) as spool:
                if args.command == 'enqueue':
                    with open(args.input, 'rb') as source:
                        result = {'queue_id': str(spool.enqueue(source))}
                elif args.command == 'status':
                    result = spool.status()
                else:
                    stop = threading.Event()
                    previous = {}
                    try:
                        for name in ('SIGINT', 'SIGTERM'):
                            number = getattr(signal, name)
                            previous[number] = signal.signal(number, lambda *_args: stop.set())
                        run_sender(spool, gateway, stop)
                        result = {'status': 'stopped'}
                    finally:
                        for number, handler in previous.items(): signal.signal(number, handler)
        print(json.dumps(result, sort_keys=True))
        return 0
    except (WaveformError, ContractError) as error:
        output = {'code': error.code}
        if error.code == 'waveform.sender_path_unsupported':
            output['storage'] = 'Windows fixed local NTFS; one file link; no network or device paths'
        print(json.dumps(output))
        return 3 if error.code == 'waveform.sender_busy' else 2
    except Exception:
        print(json.dumps({'code': 'waveform.sender_unavailable'}))
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
