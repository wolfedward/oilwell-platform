"""One readonly registry revision proves an entire exact physical window."""
from dataclasses import dataclass
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from fractions import Fraction
from math import floor, ceil
import psycopg
from psycopg.rows import dict_row

from .contracts import CaptureKey, require_count, MAX_I64, require_utc
from .errors import ContractError
from .evidence_contracts import freeze

EPOCH = datetime(1970,1,1,tzinfo=timezone.utc)


def _exact(value):
    if type(value) not in (int,Fraction): raise ContractError('contract_invalid')
    return Fraction(value)


def rational_text(value): return str(_exact(value))


def physical_window(start_ns, end_ns, uncertainty_ns):
    start,end,uncertainty=map(_exact,(start_ns,end_ns,uncertainty_ns))
    if start>end or uncertainty<0: raise ContractError('contract_invalid')
    return start-uncertainty,end+uncertainty


def contains_window(valid_from, valid_to, lower, upper):
    return lower>=valid_from and (valid_to is None or upper<valid_to)


def datetime_ns(value):
    delta=value.astimezone(timezone.utc)-EPOCH
    return (delta.days*86400+delta.seconds)*1000000000+delta.microseconds*1000


def utc_text(value):
    return value.astimezone(timezone.utc).strftime('%Y-%m-%dT%H:%M:%S.%fZ')


def _public(record):
    return {k:utc_text(v) if isinstance(v,datetime) else str(v) if hasattr(v,'hex') else v
            for k,v in record.items()}


@dataclass(frozen=True)
class RegistrySnapshot:
    status: str
    resolution_reasons: tuple
    revision: int | None
    time_basis: object
    utc_start: str | None
    start_ns: str
    end_ns: str
    uncertainty_ns: str | None
    lower_ns: str | None
    upper_ns: str | None
    sources: tuple
    well: object | None


class RegistryContext:
    def __init__(self, connect):
        if not callable(connect): raise ValueError('Explicit reader connection required')
        self._connect=connect

    def resolve(self, sources, *, utc_start, start_ns, end_ns, uncertainty_ns, revision, _cursor=None):
        if type(sources) is not tuple or not 1<=len(sources)<=32 or any(type(s) is not CaptureKey for s in sources):
            raise ContractError('contract_invalid')
        if len(set(sources))!=len(sources): raise ContractError('contract_invalid')
        start,end=_exact(start_ns),_exact(end_ns)
        if start>end: raise ContractError('contract_invalid')
        if revision is not None: require_count(revision,minimum=1,maximum=MAX_I64)
        if utc_start is not None:
            if type(utc_start) is str:
                require_utc(utc_start); anchor=datetime.fromisoformat(utc_start.replace('Z','+00:00'))
            elif type(utc_start) is datetime and utc_start.tzinfo is not None: anchor=utc_start
            else: raise ContractError('contract_invalid')
        else: anchor=None
        window=physical_window(start,end,uncertainty_ns) if uncertainty_ns is not None else None
        def result(status,reasons,rev=None,rows=(),well=None):
            return RegistrySnapshot(status,tuple(reasons),rev,
                freeze({'unit':'ns','reference':'utc_start','support':'inclusive_corrected_physical'}),
                utc_text(anchor) if anchor is not None else None,str(start),str(end),
                rational_text(uncertainty_ns) if uncertainty_ns is not None else None,
                str(window[0]) if window else None,str(window[1]) if window else None,tuple(rows),freeze(well))
        if anchor is None or window is None: return result('pending',('unknown_utc',))
        lower,upper=(Fraction(datetime_ns(anchor))+offset for offset in window)
        try:
            low_dt=EPOCH+timedelta(microseconds=floor(lower/1000))
            high_dt=EPOCH+timedelta(microseconds=ceil(upper/1000))
        except OverflowError: return result('pending',('evidence_unavailable',))
        try:
            with self._cursor(_cursor) as cur:
                cur.execute('SELECT revision FROM waveforms.registry_head WHERE singleton=1')
                head=cur.fetchone()
                if head is None: return result('pending',('evidence_unavailable',))
                rev=head['revision']
                if revision is not None and revision!=rev: return result('pending',('registry_revision_changed',),rev)
                snapshots=[]; reasons=[]; wells=[]
                for source in sources:
                    cur.execute('SELECT id,gateway_id,device_id,created_at FROM waveforms.registry_devices WHERE gateway_id=%s AND device_id=%s',
                                (source.gateway_id,source.device_id))
                    device=cur.fetchone()
                    snapshot={'capture':{'gateway_id':source.gateway_id,'device_id':source.device_id,'capture_id':str(source.capture_id)},
                              'device':_public(device) if device else None,'states':[],'bindings':[],'well':None}
                    if device is None or datetime_ns(device['created_at'])>lower:
                        reasons.append('unregistered'); snapshots.append(freeze(snapshot)); continue
                    for view,key in (('registry_device_states','states'),('registry_well_bindings','bindings')):
                        # Fixed server names. Outward candidate envelope is NOT containment proof.
                        cur.execute('SELECT * FROM waveforms.'+view+' WHERE device_id=%s AND valid_from<=%s AND (valid_to IS NULL OR valid_to>%s) ORDER BY valid_from,id LIMIT 65',
                                    (device['id'],high_dt,low_dt))
                        candidates=cur.fetchall()
                        snapshot[key]=[_public(r) for r in candidates]
                        if len(candidates)>64: return result('pending',('evidence_unavailable',),rev)
                        eligible=[r for r in candidates if contains_window(datetime_ns(r['valid_from']),
                            datetime_ns(r['valid_to']) if r['valid_to'] is not None else None,lower,upper)]
                        if key=='states':
                            if len(eligible)!=1 or eligible[0]['status']!='active': reasons.append('inactive_source')
                        elif len(eligible)!=1:
                            reasons.append('binding_boundary' if candidates else 'unbound')
                        else:
                            cur.execute('SELECT * FROM waveforms.registry_wells WHERE id=%s',(eligible[0]['well_id'],))
                            well=cur.fetchone()
                            if well is None or datetime_ns(well['created_at'])>lower: reasons.append('unbound')
                            else: snapshot['well']=_public(well); wells.append(_public(well))
                    snapshots.append(freeze(snapshot))
                if wells and len({w['id'] for w in wells})!=1: reasons.append('different_wells')
                return result('ineligible' if reasons else 'resolved',tuple(dict.fromkeys(reasons)),rev,
                              snapshots,wells[0] if not reasons and wells else None)
        except psycopg.Error:
            return result('pending',('evidence_unavailable',))

    @contextmanager
    def _cursor(self, supplied):
        if supplied is not None:
            factory=supplied.row_factory
            supplied.row_factory=dict_row
            try: yield supplied
            finally: supplied.row_factory=factory
        else:
            with self._connect() as conn:
                with conn.transaction():
                    conn.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY')
                    conn.execute("SET LOCAL statement_timeout='3s'")
                    conn.execute("SET LOCAL lock_timeout='1s'")
                    yield conn.cursor(row_factory=dict_row)


def resolve_with_cursor(cur, sources, *, utc_start, start_ns, end_ns, uncertainty_ns):
    """Resolve metadata on the caller's guarded transaction; never open a connection."""
    return RegistryContext(lambda: None).resolve(sources,utc_start=utc_start,start_ns=start_ns,
        end_ns=end_ns,uncertainty_ns=uncertainty_ns,revision=None,_cursor=cur)
