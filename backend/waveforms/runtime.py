"""Optional management runtime and atomic shared multi-role admission."""
from contextlib import contextmanager
from dataclasses import dataclass,field
import threading
import time
try:
    import psycopg
except ImportError:
    psycopg=None
from .errors import WaveformError


class WindowOperation:
    """Own computation and response cleanup; FIN is not receive abandonment."""
    def __init__(self,runtime,sock):
        self.runtime=runtime; self.sock=sock; self.deadline=time.monotonic()+120
        self.cancelled=False; self.registration=(self,sock)

    def __enter__(self):
        with self.runtime.lock:
            if self.runtime.connections.stopped: raise WaveformError('waveform.stopping','Waveform service is stopping')
            self.runtime.streams.add(self.registration)
        return self

    def close(self): self.cancelled=True

    def check(self):
        import select
        import socket
        if self.cancelled or self.runtime.connections.stopped:
            raise WaveformError('waveform.stopping','Waveform service is stopping')
        if time.monotonic()>=self.deadline: raise WaveformError('waveform.window_timeout','Window operation deadline exceeded')
        # Readiness + MSG_PEEK never consumes a subsequent request. An orderly
        # EOF may be a valid SHUT_WR client still reading our response.
        try:
            readable,_,exceptional=select.select([self.sock],[],[self.sock],0)
            if exceptional: raise ConnectionAbortedError()
            if readable: self.sock.recv(1,socket.MSG_PEEK)
        except (BlockingIOError,InterruptedError): pass

    def __exit__(self,*args):
        with self.runtime.lock:
            self.runtime.streams.discard(self.registration); self.runtime.lock.notify_all()


def _cursor_type(pool):
    class Cursor(psycopg.Cursor):
        def execute(self,*args,**kwargs):
            if pool.stopped: raise WaveformError('waveform.stopping','Waveform service is stopping')
            return super().execute(*args,**kwargs)
    return Cursor


class ManagementConnections:
    def __init__(self,settings,budget,*,admission_timeout=2):
        if psycopg is None: raise WaveformError('waveform.dependency_unavailable','PostgreSQL driver unavailable')
        self.settings=settings; self.budget=budget; self.available=budget
        self.admission_timeout=admission_timeout; self.condition=threading.Condition()
        self.local=threading.local(); self.stopped=False; self.active=set()

    @contextmanager
    def reserve(self,count):
        if getattr(self.local,'reservation',None) is not None:
            raise WaveformError('waveform.topology','Unexpected nested admission')
        deadline=time.monotonic()+self.admission_timeout
        with self.condition:
            if count>self.budget: raise WaveformError('waveform.budget_insufficient','Operation needs a larger configured connection budget')
            while self.available<count and not self.stopped:
                remaining=deadline-time.monotonic()
                if remaining<=0: raise WaveformError('waveform.busy','Waveform connection capacity is busy')
                self.condition.wait(remaining)
            if self.stopped: raise WaveformError('waveform.stopping','Waveform service is stopping')
            self.available-=count
        reservation=[count,0]; self.local.reservation=reservation
        try: yield
        finally:
            self.local.reservation=None
            with self.condition:
                self.available+=count; self.condition.notify_all()

    def factory(self,role):
        @contextmanager
        def connect():
            if getattr(self.local,'reservation',None) is None:
                with self.reserve(1):
                    with connect() as conn: yield conn
                return
            reservation=self.local.reservation
            if reservation[1]>=reservation[0]: raise WaveformError('waveform.topology','Connection topology exceeds reservation')
            reservation[1]+=1; conn=None
            try:
                if self.stopped: raise WaveformError('waveform.stopping','Waveform service is stopping')
                conn=psycopg.connect(**self.settings[role],cursor_factory=_cursor_type(self))
                with self.condition:
                    if self.stopped: raise WaveformError('waveform.stopping','Waveform service is stopping')
                    self.active.add(conn)
                with conn: yield conn
            finally:
                if conn is not None:
                    if not conn.closed: conn.close()
                    with self.condition: self.active.discard(conn); self.condition.notify_all()
                reservation[1]-=1
        return connect

    def stop(self):
        with self.condition: self.stopped=True; self.condition.notify_all()

    def close(self,deadline=None):
        deadline=deadline or time.monotonic()+20; self.stop()
        with self.condition: active=tuple(self.active)
        if psycopg.capabilities.has_cancel_safe():
            for conn in active:
                remaining=deadline-time.monotonic()
                if remaining<=0: break
                try: conn.cancel_safe(timeout=min(3,remaining))
                except psycopg.Error: pass
        with self.condition:
            while self.active or self.available!=self.budget:
                remaining=deadline-time.monotonic()
                if remaining<=0: raise WaveformError('waveform.shutdown_incomplete','Waveform owners have not finished')
                self.condition.wait(remaining)


@dataclass
class WaveformRuntime:
    status:str
    queries:object=None
    services:dict=field(default_factory=dict)
    code:str|None=None
    allowed_origins:tuple=()
    connections:object=None
    streams:set=field(default_factory=set,repr=False)
    lock:object=field(default_factory=threading.Condition,repr=False)

    def close(self):
        deadline=time.monotonic()+20
        if self.connections: self.connections.stop()
        with self.lock: streams=tuple(self.streams)
        for context,sock in streams:
            if context: context.close()
            try: sock.shutdown(2)
            except OSError: pass
        if self.connections: self.connections.close(deadline)
        with self.lock:
            while self.streams:
                remaining=deadline-time.monotonic()
                if remaining<=0: raise WaveformError('waveform.shutdown_incomplete','Waveform stream owners have not finished')
                self.lock.wait(remaining)


def build_waveform_runtime(config_path):
    if config_path is None: return WaveformRuntime('disabled',code='waveform.disabled')
    from .config import load_waveform_config
    pool=None
    try:
        config=load_waveform_config(config_path,component='management')
        if config is None: return WaveformRuntime('disabled',code='waveform.disabled')
        pool=ManagementConnections(config.postgres,config.max_db_connections)
        from dataclasses import fields
        from psycopg import sql
        from .config import Quota
        from .capacity import prepare_transaction
        from .privileges import probe_role
        from .business import check_payer
        for role,settings in config.postgres.items():
            connect=pool.factory(role); probe_role(connect,role)
            with connect() as conn:
                conn.execute('SET TRANSACTION READ ONLY')
                conn.execute("SET LOCAL statement_timeout='3s'")
                conn.execute("SET LOCAL lock_timeout='1s'")
                if conn.execute('SELECT current_database()').fetchone()[0]!=settings['dbname']: raise WaveformError('waveform.configuration_invalid','Unexpected database')
                durable=conn.execute("SELECT current_setting('fsync'),current_setting('full_page_writes'),current_setting('synchronous_commit')").fetchone()
                if durable[:2]!=('on','on') or durable[2] not in ('on','remote_apply'): raise WaveformError('waveform.durability_unavailable','Durable storage required')
                prepare_transaction(conn.cursor())
                if role=='reader':
                    check_payer(conn.cursor(),config.shared_accounting_bucket)
                    names=[f.name for f in fields(Quota)]
                    rows=conn.execute(sql.SQL('SELECT bucket,{} FROM waveforms.capacity_metadata ORDER BY bucket LIMIT 66').format(sql.SQL(',').join(map(sql.Identifier,names)))).fetchall()
                    expected=[(k,*(getattr(q,n) for n in names)) for k,q in sorted({'*':config.limits.global_quota,**config.limits.gateways}.items())]
                    if rows!=expected: raise WaveformError('waveform.capacity_configuration','Configured capacities differ')
        from .imports import ImportService
        from .evidence import EvidenceService
        from .quality_service import QualityService
        from .pairing import PairService
        from .datasets import DatasetService
        from .export import DatasetExporter
        from .queries import WaveformQueries
        reader=pool.factory('reader'); operator=pool.factory('operator')
        kw=dict(limits=config.limits,shared_accounting_bucket=config.shared_accounting_bucket,read_connect=reader)
        services={name:cls(operator,**kw) for name,cls in (('evidence',EvidenceService),('quality',QualityService),('pairs',PairService),('datasets',DatasetService),('exports',DatasetExporter))}
        services['imports']=ImportService(pool.factory('receiver'),limits=config.limits,prefix=config.topic_prefix)
        return WaveformRuntime('ready',WaveformQueries(reader),services,connections=pool)
    except Exception as error:
        if pool: pool.close()
        return WaveformRuntime('unavailable',code=error.code if isinstance(error,WaveformError) else 'waveform.unavailable')
