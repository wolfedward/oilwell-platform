"""Optional runtime assembly; workers run only from the explicit worker CLI."""
from dataclasses import dataclass, field
from pathlib import Path
import threading
from .config import ConfigurationError, load_inversion_config
from .contracts import canonical_bytes, strict_json, fields
from .permissions import SessionAuthority


@dataclass
class InversionRuntime:
    status: str
    queries: object = None
    config: object = field(default=None, repr=False)
    auth: object = field(default=None, repr=False)
    code: str | None = None
    allowed_origins: tuple = ()
    streams: set = field(default_factory=set, repr=False)
    lock: object = field(default_factory=threading.Lock, repr=False)
    stopped: bool = False

    def services_for(self, ref):
        if self.status != 'ready' or self.stopped:
            from .transactions import InversionError
            raise InversionError('unavailable')
        return assemble_services(self.config, SessionAuthority(self.auth, ref))

    def close(self):
        with self.lock:
            self.stopped = True
            streams = tuple(self.streams)
        for context, sock in streams:
            try: sock.shutdown(2)
            except OSError: pass


def connections(config):
    import psycopg
    def factory(settings):
        # Every returned connection is owned by the existing service context.
        return lambda: psycopg.connect(**settings)
    return (factory(config.postgres['operator']), factory(config.postgres['reader']),
            factory(config.waveform.postgres['operator']), factory(config.waveform.postgres['reader']))


def verify_configuration(config, auth):
    """Read-only live identity, schema, least-privilege and quota probes."""
    from dataclasses import fields as dc_fields
    from psycopg import sql
    from .capacity import InversionLimits
    from .migrate import _history, _limits
    from .privileges import probe_role
    from .metadata import probe_metadata
    from backend.waveforms.privileges import probe_role as probe_waveform
    from backend.waveforms.config import Quota
    from backend.waveforms.business import check_payer
    if auth.database.path.resolve(strict=True) != config.identity_db_path: raise ConfigurationError()
    operator, reader, wp_operator, wp_reader = connections(config)
    signatures = []
    for factory, settings, role, inversion in (
        (operator,config.postgres['operator'],'operator',True), (reader,config.postgres['reader'],'reader',True),
        (wp_operator,config.waveform.postgres['operator'],'operator',False), (wp_reader,config.waveform.postgres['reader'],'reader',False)):
        if inversion:
            probe_role(factory, role); probe_metadata(factory)
        else: probe_waveform(factory, role)
        with factory() as conn:
            conn.execute('SET TRANSACTION READ ONLY')
            conn.execute("SET LOCAL statement_timeout='3s'")
            conn.execute("SET LOCAL lock_timeout='1s'")
            row = conn.execute('SELECT current_user,current_database(),(SELECT oid FROM pg_database WHERE datname=current_database()),inet_server_addr()::text,inet_server_port(),pg_postmaster_start_time()').fetchone()
            if row[:2] != (settings['user'],settings['dbname']): raise ConfigurationError()
            signatures.append(row[1:])
            durable = conn.execute("SELECT current_setting('fsync'),current_setting('full_page_writes'),current_setting('synchronous_commit')").fetchone()
            if durable[:2] != ('on','on') or durable[2] not in ('on','remote_apply'): raise ConfigurationError()
            if inversion:
                _history(conn, complete=True); _limits(conn, InversionLimits(config.total_storage_bytes))
            elif role == 'reader':
                check_payer(conn.cursor(), config.waveform.shared_accounting_bucket)
                names = [f.name for f in dc_fields(Quota)]
                actual = conn.execute(sql.SQL('SELECT bucket,{} FROM waveforms.capacity_metadata ORDER BY bucket LIMIT 66').format(sql.SQL(',').join(map(sql.Identifier,names)))).fetchall()
                expected = [(k,*(getattr(q,n) for n in names)) for k,q in sorted({'*':config.waveform.limits.global_quota,**config.waveform.limits.gateways}.items())]
                if actual != expected: raise ConfigurationError()
    if any(signature != signatures[0] for signature in signatures): raise ConfigurationError()


def assemble_services(config, authority):
    from .capacity import InversionLimits
    from .parameters import ParameterService
    from .models import ModelService
    from .selections import SelectionService
    from .runs import RunService
    from .results import ResultService
    from .exports import ResultExporter
    from .input_acquisition import InputAcquisition
    from backend.waveforms.member_exports import MemberResearchExporter
    operator, reader, wp_operator, wp_reader = connections(config)
    kwargs = dict(read_connect=reader, authorize=authority.authorize)
    parameters = ParameterService(operator, **kwargs, accounting_bucket=config.waveform.shared_accounting_bucket)
    models = ModelService(operator, **kwargs)
    results = ResultService(operator, **kwargs)
    exporter = MemberResearchExporter(wp_operator, read_connect=wp_reader, limits=config.waveform.limits,
                                     shared_accounting_bucket=config.waveform.shared_accounting_bucket)
    return dict(parameters=parameters, models=models,
        selections=SelectionService(operator, **kwargs, parameters=parameters, models=models),
        runs=RunService(operator, **kwargs, limits=InversionLimits(config.total_storage_bytes), refresh_authority=authority.refresh),
        results=results, exports=ResultExporter(results),
        acquisition=InputAcquisition(operator, **kwargs, exporter=exporter, accounting_bucket=config.waveform.shared_accounting_bucket))


def build_inversion_runtime(config_path: Path | None, *, auth):
    if config_path is None: return InversionRuntime('disabled', code='inversion.disabled')
    try: config = load_inversion_config(config_path)
    except ConfigurationError: return InversionRuntime('misconfigured', code='inversion.configuration_invalid')
    if config is None: return InversionRuntime('disabled', code='inversion.disabled')
    try:
        verify_configuration(config, auth)
        from .queries import InversionQueries
        return InversionRuntime('ready', InversionQueries(connections(config)[1]), config, auth)
    except ConfigurationError: return InversionRuntime('misconfigured', code='inversion.configuration_invalid')
    except Exception: return InversionRuntime('degraded', code='inversion.dependencies_unavailable')


class WorkerAuthority(SessionAuthority):
    """Factory-local binding, set only by the internal persisted run refresh."""
    def authorize(self, actor, capability):
        from backend.auth import AuthError
        from .transactions import InversionError
        try: return super().authorize(actor, capability)
        except AuthError:
            self.ref = None
            raise InversionError('authority_changed', status=403) from None

    def refresh(self, ref, permissions=None):
        from backend.auth import AuthError
        from .permissions import RUN
        from .transactions import InversionError
        try: snapshot = super().refresh(ref, RUN if permissions is None else permissions)
        except AuthError:
            self.ref = None
            raise InversionError('authority_changed', status=403) from None
        self.ref = ref
        return snapshot


def worker_services(configuration):
    from backend.auth import AuthService
    from backend.database import Database
    from .worker import WorkerServices
    value = strict_json(configuration); fields(value, ('config_path',))
    config = load_inversion_config(Path(value['config_path']))
    if config is None: raise ConfigurationError()
    auth = AuthService(Database(config.identity_db_path))
    verify_configuration(config, auth)
    services = assemble_services(config, WorkerAuthority(auth))
    return WorkerServices(services['runs'], services['acquisition'])


def worker_tick(runtime, runs):
    from .worker import run_once
    owner = runs.expired_owner()
    if owner is not None: runs.recover(owner)
    return run_once(runtime)
