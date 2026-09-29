"""One fresh MQTT5 client per persisted receipt; success means Broker acceptance."""
import math
import threading
import time
from uuid import uuid4
from .consumer import DatabaseConnections, _OwnedPool, _shutdown, create_client, _dispose_client, check_component
from .outbox import WaveOutbox
from .errors import WaveformError


def _failure(code):
    return WaveformError(code,'Receipt delivery remains unconfirmed')


class WavePublisher:
    def __init__(self,outbox,gateway,*,client_factory=None,stop=None,ack_timeout=10):
        if (gateway.gateway_id!=outbox.gateway_id or gateway.session_expiry_seconds!=0
                or type(ack_timeout) not in (int,float) or not math.isfinite(ack_timeout) or not 0<ack_timeout<=20):
            raise ValueError('Publisher requires its gateway and zero session expiry')
        self.outbox,self.gateway=outbox,gateway
        self.client_factory=client_factory or create_client
        self.stop=stop or threading.Event()
        self.ack_timeout=ack_timeout
        self._lock=threading.RLock()
        self._run_lock=threading.Lock()
        self._closed=False
        self._client=None
        self._epoch=0
        self._lease=None
        self._mid=None
        self._early={}
        self._publishing=False
        self._reason=None
        self._error=None
        self._connected=False
        self._worker_id=uuid4()

    def close(self):
        # Cancellation only. The run_once owner disposes sockets after any
        # outstanding connect/database call returns; _shutdown bounds the wait.
        with self._lock:
            self._closed=True
            self._epoch+=1

    def _current(self,client,epoch):
        return not self._closed and not self.stop.is_set() and self._client is client and self._epoch==epoch

    def _check(self,deadline):
        if self._closed or self.stop.is_set(): raise _failure('waveform.stopping')
        if time.monotonic()>=deadline: raise _failure('mqtt.timeout')
        if self._error: raise _failure(self._error)

    def _open(self):
        import paho.mqtt.client as mqtt
        if self._closed or self.stop.is_set(): raise _failure('waveform.stopping')
        client=self.client_factory(self.gateway)
        with self._lock:
            self._client=client; self._epoch+=1; epoch=self._epoch
            self._connected=False; self._error=None
        def connected(source,_userdata,_flags,reason,_properties):
            with self._lock:
                if self._current(source,epoch):
                    self._connected=getattr(reason,'value',reason)==0
                    if not self._connected: self._error='mqtt.connection_rejected'
        def disconnected(source,_userdata,_flags,_reason,_properties):
            with self._lock:
                if self._current(source,epoch): self._error='mqtt.disconnected'
        client.on_connect,client.on_disconnect=connected,disconnected
        properties=mqtt.Properties(mqtt.PacketTypes.CONNECT)
        properties.SessionExpiryInterval=0
        deadline=time.monotonic()+self.gateway.mqtt.connect_timeout_seconds+3
        self._check(deadline)
        if client.connect(self.gateway.mqtt.host,self.gateway.mqtt.port,self.gateway.mqtt.keepalive_seconds,
                          clean_start=True,properties=properties)!=0: raise _failure('mqtt.connect_failed')
        while not self._connected:
            self._check(deadline)
            if client.loop(timeout=.2)!=0: raise _failure('mqtt.connect_failed')
        self._check(deadline)
        return client,epoch

    def _publish(self,client,epoch,lease):
        deadline=min(lease._deadline,time.monotonic()+self.ack_timeout)
        # Closure binds full immutable lease, source identity, and epoch. Only
        # the publish call's brief MID-return window can buffer early callbacks.
        def published(source,_userdata,mid,reason,_properties):
            with self._lock:
                if not self._current(source,epoch) or self._lease is not lease or self._error: return
                value=getattr(reason,'value',reason)
                if type(mid) is not int or not 1<=mid<=65535:
                    self._error='mqtt.invalid_ack'
                elif self._publishing:
                    if len(self._early)>=64 and mid not in self._early:
                        self._error='mqtt.callback_overflow'
                    else: self._early.setdefault(mid,value)
                elif mid==self._mid and self._reason is None:
                    self._reason=value
        with self._lock:
            self._lease=lease; self._reason=None; self._mid=None
            self._early={}; self._publishing=True
            client.on_publish=published
        try:
            self._check(deadline)
            info=client.publish(lease.topic,lease.payload,qos=1,retain=False)
            with self._lock:
                self._publishing=False
                if info.rc!=0: raise _failure('mqtt.publish_rejected')
                if type(info.mid) is not int or not 1<=info.mid<=65535: raise _failure('mqtt.invalid_mid')
                self._mid=info.mid; self._reason=self._early.get(info.mid); self._early.clear()
            while True:
                self._check(deadline)
                with self._lock:
                    if self._reason is not None:
                        if type(self._reason) is int and self._reason in (0,16): return
                        raise _failure('mqtt.publish_rejected')
                if client.loop(timeout=min(.2,max(.001,deadline-time.monotonic())))!=0:
                    raise _failure('mqtt.disconnected')
        finally:
            with self._lock:
                self._lease=None; self._publishing=False; self._early.clear()

    def run_once(self)->int:
        if not self._run_lock.acquire(blocking=False): raise _failure('waveform.publisher_busy')
        lease=None
        try:
            try:
                client,epoch=self._open()
                leases=self.outbox.claim(self._worker_id,limit=1)
                if not leases: return 0
                lease,=leases
                self._publish(client,epoch,lease)
            except OSError:
                if lease is not None and not self._closed and not self.stop.is_set():
                    self.outbox.finish(lease,delivered=False,error_code='mqtt.unavailable')
                elif not self._closed and not self.stop.is_set():
                    self.outbox.record_error('mqtt.unavailable')
                return 0
            except WaveformError as error:
                if error.code=='waveform.stopping' and (self._closed or self.stop.is_set()): return 0
                if not error.code.startswith('mqtt.'): raise
                if lease is not None and not self._closed and not self.stop.is_set():
                    self.outbox.finish(lease,delivered=False,error_code=error.code)
                elif not self._closed and not self.stop.is_set():
                    self.outbox.record_error(error.code)
                return 0
            # Keep uncertain success commit outside delivery-failure recovery.
            if self._closed or self.stop.is_set(): return 0
            return int(self.outbox.finish(lease,delivered=True,error_code=None))
        finally:
            try:
                with self._lock:
                    client=self._client; self._client=None; self._epoch+=1
                    self._lease=None; self._early.clear(); self._publishing=False
                if client is not None:
                    try: _dispose_client(client)
                    except WaveformError:
                        self.close()
                        raise
                    finally: client.on_publish=client.on_disconnect=client.on_connect=None
            finally: self._run_lock.release()


def run_publish(config,stop,*,on_event=None):
    emit=on_event or (lambda *_args,**_kwargs:None)
    connections=DatabaseConnections(config,stop)
    check_component(config,connections)
    publishers=[WavePublisher(WaveOutbox(connections.factory('receipt_publisher'),gateway_id=g.gateway_id,
                    limits=config.limits),g,stop=stop) for g in config.gateways]
    def process(publisher):
        while not stop.is_set():
            try:
                count=publisher.run_once()
                emit('broker_accepted' if count else 'idle',gateway=publisher.gateway.gateway_id,count=count)
            except WaveformError as error:
                if error.code=='waveform.stopping' and stop.is_set(): return
                if error.code!='waveform.unavailable': raise
                emit('retry',gateway=publisher.gateway.gateway_id,code=error.code)
                count=0
            if not count: stop.wait(1)
    pool=_OwnedPool(len(publishers),'waveform-publish'); futures=[]
    try:
        futures=[pool.submit(process,publisher) for publisher in publishers]
        while not stop.wait(.2):
            for future in futures:
                if future.done():
                    future.result()
                    raise _failure('waveform.worker_stopped')
    finally:
        for publisher in publishers: publisher.close()
        _shutdown(pool,futures,connections,stop)
    for future in futures:
        if not future.cancelled(): future.result()
