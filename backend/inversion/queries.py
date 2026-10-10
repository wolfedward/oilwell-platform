"""Finite bounded read queries and explicit public response projections."""
from collections.abc import Mapping
from dataclasses import fields, is_dataclass
from datetime import datetime
import json
from uuid import UUID
from .contracts import canonical_bytes
from .exports import public_value, public_result
from .transactions import InversionError

MECHANICAL_FIELDS = frozenset(('assembly_id','manufacturer','model','asset_number','mechanism','source_kind',
    'source_organization','valid_from','valid_to','parent_version','installation_evidence'))


def wire(value):
    if isinstance(value, UUID): return str(value)
    if isinstance(value, datetime): return value.isoformat()
    if is_dataclass(value): return {f.name:wire(getattr(value,f.name)) for f in fields(value)}
    if isinstance(value, Mapping): return {k:wire(v) for k,v in value.items()}
    if isinstance(value, (tuple,list,frozenset)): return [wire(v) for v in value]
    return value


def run_view(run, result_id=None):
    return wire(dict(run_id=run.id, request=run.request, predecessor_id=run.predecessor_id,
        version=run.version, state=run.state, reason_code=run.reason_code,
        cancelling=run.state=='running' and run.reason_code=='cancel_requested', result_id=result_id))


def governance_view(record):
    names = ('id','assembly_id','assembly_version','version','content_sha256','installed_id','review_version','review_state','valid_from','valid_to')
    result = {key:wire(getattr(record,key)) for key in names if hasattr(record,key)}
    result['body'] = public_value(wire(record.body))
    return result


def mechanical_view(assembly_id, row):
    version_id,version,content_sha256,body,revision,valid_from,valid_to=row
    known=body.get('valid_from') is not None
    declared=dict(interval_known=known,valid_from=body.get('valid_from') if known else None,
        valid_to=body.get('valid_to') if known else None,
        body={key:value for key,value in body.items() if key in MECHANICAL_FIELDS})
    effective=None if revision is None else dict(revision=revision,valid_from=valid_from,valid_to=valid_to)
    return public_value(wire(dict(assembly_id=assembly_id,version_id=version_id,version=version,
        content_sha256=content_sha256,declared=declared,effective=effective)))


def result_view(view):
    return dict(id=str(view.id),run_id=str(view.run_id),content_sha256=view.content_sha256,
        request_sha256=view.request_sha256,snapshot_sha256=view.snapshot_sha256,execution=view.execution,
        source=view.source,review={key:view.review[key] for key in ('state','version')},
        current_applicability=public_value(wire(view.current_applicability)),
        field_validation=public_value(wire(view.field_validation)),body=public_result(wire(view.body)))


def point_page(view, offset, limit):
    """Page complete persisted rows without numeric conversions or resampling."""
    rows=[]; total=0
    for raw in view.chunks:
        block=json.loads(raw)
        for row in block['rows']:
            if offset <= total < offset+limit: rows.append(dict(kind=block['kind'],columns=block['columns'],values=row))
            total+=1
    return dict(result_id=str(view.id),content_sha256=view.content_sha256,offset=offset,limit=limit,total=total,rows=rows)


class InversionQueries:
    def __init__(self, read_connect): self.read_connect=read_connect

    @staticmethod
    def configure(conn):
        conn.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY')
        conn.execute("SET LOCAL statement_timeout='3s'")
        conn.execute("SET LOCAL lock_timeout='1s'")

    def health(self):
        from .capacity import usage
        with self.read_connect() as conn:
            self.configure(conn)
            return dict(status='ready',capacity=usage(conn.cursor()),field_accuracy_certified=False)

    def fetch(self, resource, identifier=None, *, offset=0, limit=50, **filters):
        action=resource
        # SQL fragments are fixed server-owned statements; only values vary.
        statements = {
            'assemblies.list': ('SELECT a.id,v.version,v.content_sha256,v.body,s.valid_from,s.valid_to FROM inversion.assemblies a JOIN LATERAL (SELECT * FROM inversion.assembly_versions WHERE assembly_id=a.id ORDER BY version DESC LIMIT 1) v ON true LEFT JOIN inversion.mechanical_state s ON s.version_id=v.id ORDER BY a.id LIMIT %s OFFSET %s', ('id','version','content_sha256','body','valid_from','valid_to')),
            'parameters.list': ('SELECT p.id,p.assembly_id,p.assembly_version,p.version,p.content_sha256,r.version,r.action FROM inversion.parameter_versions p LEFT JOIN LATERAL (SELECT version,action FROM inversion.reviews WHERE subject_id=p.id ORDER BY version DESC LIMIT 1) r ON true ORDER BY p.id LIMIT %s OFFSET %s', ('id','assembly_id','assembly_version','version','content_sha256','review_version','review_action')),
            'models.list': ('SELECT m.id,m.model_key,m.version,m.content_sha256,r.version,r.action FROM inversion.model_versions m LEFT JOIN LATERAL (SELECT version,action FROM inversion.model_reviews WHERE subject_id=m.id ORDER BY version DESC LIMIT 1) r ON true ORDER BY m.id LIMIT %s OFFSET %s', ('id','installed_id','version','content_sha256','review_version','review_action')),
            'runs.list': ('SELECT r.id,s.version,s.state,s.reason_code,r.predecessor_id,result.id FROM inversion.run_requests r JOIN inversion.run_state s ON s.run_id=r.id LEFT JOIN inversion.results result ON result.run_id=r.id ORDER BY r.created_at DESC,r.id LIMIT %s OFFSET %s', ('run_id','version','state','reason_code','predecessor_id','result_id')),
            'selections.list': ('SELECT id,version,parameter_id,model_id,body FROM inversion.selections WHERE assembly_id=%s ORDER BY version DESC LIMIT %s OFFSET %s', ('id','version','parameter_id','model_id','body')),
        }
        with self.read_connect() as conn:
            self.configure(conn)
            if action == 'assembly_versions.list':
                if conn.execute('SELECT 1 FROM inversion.assemblies WHERE id=%s',(identifier,)).fetchone() is None:
                    raise InversionError('assembly_missing',status=404)
                rows=conn.execute('SELECT v.id,v.version,v.content_sha256,v.body,s.revision,s.valid_from,s.valid_to FROM inversion.assembly_versions v LEFT JOIN inversion.mechanical_state s ON s.version_id=v.id WHERE v.assembly_id=%s ORDER BY v.version DESC LIMIT %s OFFSET %s',
                    (identifier,limit,offset)).fetchall()
                return dict(assembly_id=str(identifier),offset=offset,limit=limit,
                    items=[mechanical_view(identifier,row) for row in rows])
            if action == 'assemblies.get':
                rows=conn.execute('SELECT a.id,v.version,v.content_sha256,v.body,s.valid_from,s.valid_to FROM inversion.assemblies a JOIN LATERAL (SELECT * FROM inversion.assembly_versions WHERE assembly_id=a.id ORDER BY version DESC LIMIT 1) v ON true LEFT JOIN inversion.mechanical_state s ON s.version_id=v.id WHERE a.id=%s',(identifier,)).fetchall()
                if not rows: raise InversionError('assembly_missing',status=404)
                return public_value(wire(dict(zip(statements['assemblies.list'][1],rows[0]))))
            if action == 'audit':
                rows=conn.execute('SELECT id,action,subject_id,reason,created_at FROM inversion.audit WHERE (%s::text IS NULL OR action=%s) AND (%s::uuid IS NULL OR subject_id=%s) ORDER BY created_at DESC,id LIMIT %s OFFSET %s',
                    (filters.get('action'),filters.get('action'),filters.get('subject_id'),filters.get('subject_id'),limit,offset)).fetchall()
                columns=('id','action','subject_id','reason','created_at')
            elif action == 'parameters.list' and filters.get('assembly_id') is not None:
                assembly_id=filters['assembly_id']
                statement=('SELECT p.id,p.assembly_id,p.assembly_version,p.version,p.content_sha256,r.version,r.action '
                    'FROM inversion.parameter_versions p LEFT JOIN LATERAL '
                    '(SELECT version,action FROM inversion.reviews WHERE subject_id=p.id ORDER BY version DESC LIMIT 1) r ON true '
                    'WHERE p.assembly_id=%s ORDER BY p.version DESC LIMIT %s OFFSET %s')
                columns=statements['parameters.list'][1]
                rows=conn.execute(statement,(assembly_id,limit,offset)).fetchall()
            else:
                statement,columns=statements[action]
                args=(identifier,limit,offset) if action=='selections.list' else (limit,offset)
                rows=conn.execute(statement,args).fetchall()
            return dict(offset=offset,limit=limit,items=[public_value(wire(dict(zip(columns,row)))) for row in rows])

    def recover(self, key, action, actor, *, intent=None):
        from .routes import RECOVERY_ACTIONS
        with self.read_connect() as conn:
            self.configure(conn)
            receipt=conn.execute('SELECT subject_id,version,content_sha256 FROM inversion.receipts WHERE actor_domain=%s AND actor_id=%s AND key=%s',
                (actor.domain,actor.user_id,key)).fetchone()
            if receipt is None: return {'state':'unknown'}
            # Indexed actor/key correlation, bounded even if corrupt duplicate audit rows exist.
            audit=conn.execute("SELECT action,subject_id FROM inversion.audit WHERE actor->>'domain'=%s AND actor->>'user_id'=%s AND request_key=%s LIMIT 2",
                (actor.domain,actor.user_id,key)).fetchall()
            if len(audit)!=1 or audit[0][0] not in RECOVERY_ACTIONS[action] or audit[0][1]!=receipt[0]:
                raise InversionError('request_binding_conflict',status=409)
            if action=='exports.create':
                export=conn.execute('SELECT intent FROM inversion.result_exports WHERE id=%s',(receipt[0],)).fetchone()
                if export!=(intent,): raise InversionError('request_binding_conflict',status=409)
            return wire(dict(state='committed',subject_id=receipt[0],version=receipt[1],content_sha256=receipt[2]))

    def result_id(self, run_id):
        with self.read_connect() as conn:
            self.configure(conn)
            row=conn.execute('SELECT id FROM inversion.results WHERE run_id=%s',(run_id,)).fetchone()
            return str(row[0]) if row else None

    def preflight(self, request):
        from .runs import RunService
        missing=[]
        try:
            with self.read_connect() as conn:
                self.configure(conn)
                RunService._pins(conn.cursor(),request)
        except InversionError as error:
            if error.status!=409: raise
            missing.append(error.code)
        return dict(allowed=not missing,missing=missing,provisional=True,
                    scope='fixed_input_metadata',calculation_performed=False)
