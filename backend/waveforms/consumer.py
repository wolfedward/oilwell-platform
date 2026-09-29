"""Independent gateway MQTT lifecycles and bounded shared assembly execution."""
from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED
from contextlib import contextmanager
from dataclasses import fields
import ssl
import threading
import time
from functools import partial
from uuid import UUID, uuid4, uuid5, NAMESPACE_URL

import psycopg
from psycopg import sql

from .contracts import Observation
from .errors import WaveformError
from .config import Quota
from .capacity import prepare_transaction, lock_gateway


class WaveformConsumer:
    def __init__(self, repository, *, source_ref=None):
        self.repository = repository
        self.source_ref = source_ref if source_ref is not None else uuid4()
        if type(self.source_ref) is not UUID:
            raise ValueError("source_ref must be an explicitly controlled UUID")

    def handle_message(self, client, message):
        """Called only by the connection owner; it cannot reconnect during ingest."""
        result = self.repository.ingest(Observation("mqtt", message.topic, bytes(message.payload),
            datetime.now(timezone.utc), self.source_ref, message.qos, message.retain, message.dup))
        if message.qos == 1:
            if not client.is_connected() or client.ack(message.mid, message.qos) != 0:
                raise WaveformError("waveform.mqtt_unavailable", "Durable input awaits MQTT replay")
        return result


class _StopCursor(psycopg.Cursor):
    def __init__(self, *args, stop, **kwargs):
        super().__init__(*args, **kwargs)
        self.stop = stop

    def execute(self, *args, **kwargs):
        if self.stop.is_set():
            raise WaveformError("waveform.stopping", "Waveform component is stopping")
        return super().execute(*args, **kwargs)


class DatabaseConnections:
    """A process-local bound shared by all startup, intake and health operations."""
    def __init__(self, config, stop=None):
        self.settings = config.postgres
        self.slots = threading.BoundedSemaphore(config.max_db_connections)
        self.stop = stop or threading.Event()
        self.active = set()
        self.lock = threading.Lock()

    @property
    def active_count(self):
        with self.lock:
            return len(self.active)

    def cancel_active(self, deadline):
        if not psycopg.capabilities.has_cancel_safe():
            return  # Legacy cancel() cannot offer a bounded network wait.
        with self.lock:
            connections = tuple(self.active)
        for connection in connections:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            try:
                connection.cancel_safe(timeout=min(3, remaining))
            except psycopg.Error:
                pass  # Only actual owner termination proves cleanup, not cancel().

    def factory(self, role):
        @contextmanager
        def connect():
            with self.slots:
                if self.stop.is_set():
                    raise WaveformError("waveform.stopping", "Waveform component is stopping")
                connection = psycopg.connect(**self.settings[role], cursor_factory=partial(_StopCursor, stop=self.stop))
                try:
                    # A connect already in flight can complete after stop. The
                    # owner closes it without beginning work or publishing readiness.
                    with self.lock:
                        if self.stop.is_set():
                            raise WaveformError("waveform.stopping", "Waveform component is stopping")
                        self.active.add(connection)
                    with connection:
                        yield connection
                finally:
                    # Context commit/rollback finishes before close and removal.
                    # Only the owner closes; cancellation never closes this object.
                    connection.close()
                    with self.lock:
                        self.active.discard(connection)
        return connect


class _OwnedPool(ThreadPoolExecutor):
    def __init__(self, count, name):
        self.owners, self.owner_lock = set(), threading.Lock()
        self.owner_prefix = name + "-" + uuid4().hex + "_"
        super().__init__(max_workers=count, thread_name_prefix=self.owner_prefix[:-1])

    def owned_threads(self):
        # enumerate also sees idle/starting/cancelled-before-callable workers;
        # the per-pool random exact prefix cannot match another component pool.
        with self.owner_lock:
            self.owners.update(thread for thread in threading.enumerate() if thread.name.startswith(self.owner_prefix))
            return tuple(self.owners)

    def submit(self, function, *args, **kwargs):
        def owned():
            with self.owner_lock:
                self.owners.add(threading.current_thread())
            return function(*args, **kwargs)
        future = super().submit(owned)
        self.owned_threads()
        return future


def _shutdown(pool, futures, connections, stop, *, timeout=20):
    """One total deadline, including cancellation, futures and actual thread joins."""
    deadline = time.monotonic() + timeout
    stop.set()
    pool.shutdown(wait=False, cancel_futures=True)
    connections.cancel_active(deadline)
    # Cancelled-before-running futures need not be waited on by wait().
    running = [future for future in futures if not future.cancelled()]
    pending = wait(running, timeout=max(0, deadline - time.monotonic()))[1] if running else set()
    owners = pool.owned_threads()
    for owner in owners:
        owner.join(timeout=max(0, deadline - time.monotonic()))
    if pending or any(owner.is_alive() for owner in owners) or connections.active_count:
        raise WaveformError("waveform.shutdown_incomplete", "Owned work has not stopped; supervisor intervention is required")


def check_component(config, connections=None):
    """Read-only readiness gates; never migrations, grants, or business repair."""
    from .privileges import probe_role
    if config is None:
        return
    if config.component in ("publish", "management"):
        raise WaveformError("waveform.component_unavailable", "Component implementation is not yet available")
    connections = connections or DatabaseConnections(config)
    try:
        for role, settings in config.postgres.items():
            connect = connections.factory(role)
            probe_role(connect, role)
            with connect() as connection:
                connection.execute("SET TRANSACTION READ ONLY")
                connection.execute("SET LOCAL statement_timeout='3s'")
                if connection.execute("SELECT current_database()").fetchone()[0] != settings["dbname"]:
                    raise WaveformError("waveform.configuration_invalid", "Unexpected waveform database")
                durable = connection.execute("SELECT current_setting('fsync'),current_setting('full_page_writes'),current_setting('synchronous_commit')").fetchone()
                if durable[:2] != ("on", "on") or durable[2] not in ("on", "remote_apply"):
                    raise WaveformError("waveform.durability_unavailable", "Durable WAL settings are unavailable")
                # Already-safe settings cannot be upgraded here. Reuse the shared
                # logged-relation inventory while remaining in READ ONLY mode.
                prepare_transaction(connection.cursor())
                names = [item.name for item in fields(Quota)]
                query = sql.SQL("SELECT bucket,{} FROM waveforms.capacity ORDER BY bucket LIMIT 66").format(sql.SQL(",").join(map(sql.Identifier, names)))
                expected = [(key, *(getattr(quota, name) for name in names))
                    for key, quota in sorted({"*": config.limits.global_quota, **config.limits.gateways}.items())]
                if connection.execute(query).fetchall() != expected:
                    raise WaveformError("waveform.capacity_configuration", "Configured capacities differ from storage")
        if config.component in ("receive", "sender"):
            # Construct clients to check installed API and certificates, without sockets.
            for gateway in config.gateways:
                create_client(gateway)
    except psycopg.Error:
        raise WaveformError("waveform.unavailable", "Waveform readiness check unavailable") from None


def create_client(gateway):
    import paho.mqtt.client as mqtt
    config = gateway.mqtt
    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=config.client_id,
                         protocol=mqtt.MQTTv5, manual_ack=True, reconnect_on_failure=False)
    client.connect_timeout = config.connect_timeout_seconds
    client.username_pw_set(config.username, config.password)
    if config.tls_enabled:
        try:
            client.tls_set(ca_certs=config.ca_cert, certfile=config.client_cert,
                           keyfile=config.client_key, cert_reqs=ssl.CERT_REQUIRED,
                           tls_version=ssl.PROTOCOL_TLS_CLIENT)
        except (OSError, ssl.SSLError):
            raise WaveformError("waveform.configuration_invalid", "MQTT TLS configuration unavailable") from None
    return client


class _DeliverySession:
    """Never reused across connections; owned exclusively by its gateway thread."""
    def __init__(self, client, stop):
        self.client, self.stop, self.active = client, stop, True

    def is_connected(self):
        return self.active and not self.stop.is_set() and self.client.is_connected()

    def ack(self, mid, qos):
        if not self.is_connected():
            return 4  # MQTT_ERR_NO_CONN
        return self.client.ack(mid, qos)


def _dispose_client(client):
    """Owner-only disposal: a queued DISCONNECT is not socket closure.

    Paho 2.1 has no public close API. Its private reset closes both the network
    socket and loop()'s wakeup pair. Keep the original objects to verify closure,
    even when Paho detaches them. A blocked owner remains under _shutdown's one
    total deadline; failed disposal must never allow a replacement client.
    """
    sockets = (client.socket(), client._sockpairR, client._sockpairW)
    try:
        try:
            client.disconnect()
        finally:
            client._reset_sockets()
        if (any(sock is not None and sock.fileno() != -1 for sock in sockets)
                or client.socket() is not None or client._sockpairR is not None
                or client._sockpairW is not None):
            raise RuntimeError("MQTT socket disposal incomplete")
    except Exception:
        raise WaveformError("waveform.shutdown_incomplete", "MQTT client disposal incomplete; supervisor intervention is required") from None
    finally:
        client.on_connect = client.on_subscribe = client.on_message = None


def _receiver_health(connect, config, gateway, *, success=False, error=None):
    # Never touches the assembler row or its clock high-water / rollback alert.
    try:
        with connect() as connection:
            cursor = connection.cursor()
            prepare_transaction(cursor)
            lock_gateway(cursor, gateway, config.limits)
            cursor.execute("""INSERT INTO waveforms.worker_health
                (worker_id,component,gateway_id,last_seen,last_success,last_failure,error_code)
                VALUES(%s,'receiver',%s,clock_timestamp(),CASE WHEN %s THEN clock_timestamp() END,
                       CASE WHEN %s::text IS NOT NULL THEN clock_timestamp() END,%s)
                ON CONFLICT(component,gateway_id) DO UPDATE SET
                last_seen=greatest(waveforms.worker_health.last_seen,excluded.last_seen),
                last_success=coalesce(excluded.last_success,waveforms.worker_health.last_success),
                last_failure=coalesce(excluded.last_failure,waveforms.worker_health.last_failure),
                error_code=excluded.error_code""", (uuid5(NAMESPACE_URL, "waveforms/receiver/" + gateway),
                    gateway, success, error, error))
            cursor.execute("UPDATE waveforms.state_head SET version=version+1 WHERE singleton=1")
    except psycopg.Error:
        raise WaveformError("waveform.unavailable", "Receiver health transaction unavailable") from None


def _receive_gateway(config, gateway, connect, stop, emit):
    import paho.mqtt.client as mqtt
    from paho.mqtt.subscribeoptions import SubscribeOptions
    from .repository import WaveformRepository
    consumer = WaveformConsumer(WaveformRepository(connect, limits=config.limits, prefix=config.topic_prefix,
        ingress_gateway_id=gateway.gateway_id), source_ref=gateway.source_ref)
    delay = 1
    while not stop.is_set():
        client = create_client(gateway)
        session = _DeliverySession(client, stop)
        subscribed = False
        connected_at = time.monotonic()
        last_health = connected_at
        def connected(paho_client, _userdata, _flags, reason, _properties):
            if reason.is_failure:
                raise WaveformError("waveform.mqtt_unavailable", "MQTT connection refused")
            result, _mid = paho_client.subscribe(f"{config.topic_prefix}/v2/{gateway.gateway_id}/+/waveform",
                                                options=SubscribeOptions(qos=1, retainAsPublished=True))
            if result != mqtt.MQTT_ERR_SUCCESS:
                raise WaveformError("waveform.mqtt_unavailable", "MQTT subscription unavailable")
        def subscription(_client, _userdata, _mid, reasons, _properties):
            nonlocal subscribed
            if len(reasons) != 1 or reasons[0].value != 1:
                raise WaveformError("waveform.mqtt_unavailable", "MQTT QoS 1 subscription refused")
            subscribed = True
            _receiver_health(connect, config, gateway.gateway_id)
            emit("subscribed", gateway=gateway.gateway_id)
        def receive(_client, _userdata, message):
            result = consumer.handle_message(session, message)
            _receiver_health(connect, config, gateway.gateway_id, success=True)
            emit("stored", gateway=gateway.gateway_id, status=result.status, mid=message.mid)
        client.on_connect, client.on_subscribe, client.on_message = connected, subscription, receive
        error = None
        try:
            properties = mqtt.Properties(mqtt.PacketTypes.CONNECT)
            properties.SessionExpiryInterval = gateway.session_expiry_seconds
            properties.ReceiveMaximum = 4
            # Wire packet allowance includes topic/header/properties; payload policy
            # remains the repository's exact 256 KiB. Broker ACL/packet controls
            # are still required (Paho buffers packets; QoS 0 bypasses ReceiveMaximum).
            properties.MaximumPacketSize = 270336
            if client.connect(gateway.mqtt.host, gateway.mqtt.port, gateway.mqtt.keepalive_seconds,
                              clean_start=False, properties=properties) != mqtt.MQTT_ERR_SUCCESS:
                raise WaveformError("waveform.mqtt_unavailable", "MQTT connection unavailable")
            while not stop.is_set():
                if client.loop(timeout=0.2) != mqtt.MQTT_ERR_SUCCESS:
                    raise WaveformError("waveform.mqtt_unavailable", "MQTT session interrupted")
                if not subscribed and time.monotonic() - connected_at > gateway.mqtt.connect_timeout_seconds + 3:
                    raise WaveformError("waveform.mqtt_unavailable", "MQTT subscription timed out")
                if subscribed and time.monotonic() - last_health >= 10:
                    _receiver_health(connect, config, gateway.gateway_id)
                    last_health = time.monotonic()
        except (WaveformError, OSError) as failure:
            error = failure.code if isinstance(failure, WaveformError) else "waveform.mqtt_unavailable"
        finally:
            # A failed old socket may still look live during DB work. Even then its
            # ACK targets only this object/socket, never a replacement lifecycle.
            session.active = False
            _dispose_client(client)
        if stop.is_set():
            break
        if time.monotonic() - connected_at >= 60:
            delay = 1
        try:
            _receiver_health(connect, config, gateway.gateway_id, error=error)
        except WaveformError:
            pass  # DB failure must not suppress local retry or affect another owner.
        emit("retry", gateway=gateway.gateway_id, code=error, delay=delay)
        stop.wait(delay)
        delay = min(60, delay * 2)


def run_receive(config, stop, *, on_event=None):
    """One owner and one active message per gateway, zero queued application work."""
    emit = on_event or (lambda *_args, **_kwargs: None)
    connections = DatabaseConnections(config, stop)
    check_component(config, connections)
    pool = _OwnedPool(len(config.gateways), "waveform-receive")
    futures = []
    try:
        for gateway in config.gateways:
            futures.append(pool.submit(_receive_gateway, config, gateway, connections.factory("receiver"), stop, emit))
        while not stop.wait(.2):
            for future in futures:
                if future.done():
                    future.result()
                    raise WaveformError("waveform.worker_stopped", "Gateway worker stopped unexpectedly")
    finally:
        _shutdown(pool, futures, connections, stop)
    for future in futures:
        if not future.cancelled():
            future.result()


def run_assemble(config, stop, *, on_event=None):
    """A bounded shared pool; each round claims at most once per configured gateway."""
    from .assembly import Assembler
    emit = on_event or (lambda *_args, **_kwargs: None)
    connections = DatabaseConnections(config, stop)
    check_component(config, connections)
    assembler = Assembler(connections.factory("assembler"), clock=None, limits=config.limits)
    def process(gateway):
        try:
            lease = assembler.claim(uuid4(), gateway_id=gateway.gateway_id)
            if lease is None:
                emit("idle", gateway=gateway.gateway_id)
                return False
            result = assembler.run(lease)
            emit("completed" if result is not None else "lease_lost", gateway=gateway.gateway_id)
            return result is not None
        except WaveformError as error:
            if error.code == "waveform.stopping" and stop.is_set():
                return False
            if error.code != "waveform.unavailable":
                raise
            emit("retry", gateway=gateway.gateway_id, code=error.code)
            return False
    pool = _OwnedPool(config.assembly_workers, "waveform-assemble")
    futures = []
    try:
        while not stop.is_set():
            worked = False
            # Batches bound submitted work too; no unfiltered claim/drain and no
            # caller-written assembler heartbeat that could clear clock rollback.
            for offset in range(0, len(config.gateways), config.assembly_workers):
                if stop.is_set():
                    break
                futures = [pool.submit(process, gateway) for gateway in
                           config.gateways[offset:offset + config.assembly_workers]]
                pending = set(futures)
                while pending and not stop.is_set():
                    finished, pending = wait(pending, timeout=.2, return_when=FIRST_COMPLETED)
                    for future in finished:
                        worked = future.result() or worked
            if not worked:
                stop.wait(1)
    finally:
        _shutdown(pool, futures, connections, stop)
    for future in futures:
        if not future.cancelled():
            future.result()
