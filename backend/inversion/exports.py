"""Durable export intent and one-use, actor-bound audited byte contexts.

Manifest hash binds static create-time identity, never dynamic downloaded bytes.
Access audit is committed before bytes. Finished means server iterator exhaustion,
not client receipt. HTTP must prime before headers and close on disconnect.
"""
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha256
from uuid import UUID, uuid4
import csv
import io
import json
import re
from psycopg.types.json import Jsonb
from .contracts import InversionCommand, canonical_bytes, freeze
from .parameters import actor_key, expected
from .results import ResultService, GUARDS, configure, refuse
from .transactions import Change, MutationRequest, lock

RESULT_FIELDS=frozenset(('schema','input_sha256','cycle','prediction_status','prediction_reason_codes',
    'coverage_fraction','point_count','valid_point_count','metadata','comparison','filled_comparison',
    'reference_sources','source_kind','input_applicability','field_accuracy_certified','external_evidence_admission_required'))
PRIVATE_KEY=re.compile(r'password|passwd|pwd|secret|credential|token|connection|dsn|path|username|email|actor|authority|session|private_key|api_key',re.I)
PRIVATE_TEXT=re.compile(r'(?:[A-Za-z]:[\\/]|\\\\|(?:^|\s)/[^\s]+|(?:postgres(?:ql)?|file)://|(?:password|passwd|pwd|secret|token|dsn|connection_string|api_key|private_key)\s*[:=])',re.I)

def safe_text(value):
    return '[redacted]' if PRIVATE_TEXT.search(value) else value

def public_value(value):
    if isinstance(value,Mapping):
        return {safe_text(k):public_value(v) for k,v in value.items() if not PRIVATE_KEY.search(k)}
    if isinstance(value,(list,tuple)): return [public_value(v) for v in value]
    return safe_text(value) if isinstance(value,str) else value

def public_result(body):
    return public_value({k:v for k,v in body.items() if k in RESULT_FIELDS})

def csv_cell(value):
    if not isinstance(value,str): value=json.dumps(value,ensure_ascii=False,separators=(',',':'))
    value=safe_text(value)
    if value.startswith(('=','+','-','@','\t','\r','\n')) or value.lstrip().startswith(('=','+','-','@')):
        return "'"+value
    return value

def _serialize(body,format):
    if format=='json': return canonical_bytes(body)
    output=io.StringIO(newline=''); writer=csv.writer(output,lineterminator='\r\n')
    writer.writerow(('section','block','row','field','value'))
    def emit(section,block,row,field,value):
        writer.writerow([csv_cell(v) for v in (section,block,row,field,value)])
    def flatten(value,path):
        if isinstance(value,dict):
            for key,item in value.items(): yield from flatten(item,path+'.'+key if path else key)
        elif isinstance(value,list):
            if not value: yield path,[]
            for index,item in enumerate(value): yield from flatten(item,path+'.'+str(index))
        else: yield path,value
    for field,value in flatten({k:v for k,v in body.items() if k!='chunks'},''):
        emit('report','','',field,value)
    for block_index,block in enumerate(body['chunks']):
        for row_index,row in enumerate(block['rows']):
            for column,value in zip(block['columns'],row):
                emit(block['kind'],block_index,row_index,column,value)
    return output.getvalue().encode('utf-8')

@dataclass(frozen=True)
class ExportRecord:
    id: UUID
    result_id: UUID
    result_sha256: str
    intent: str
    format: str
    actor_domain: str
    actor_id: str
    manifest: object
    manifest_sha256: str

class ResultExporter:
    def __init__(self,results):
        if type(results) is not ResultService: raise ValueError('Configured ResultService required')
        self.results=results
        self._contexts=set()

    def _auth(self,actor,intent):
        capabilities={'inversion.read','inversion.export','waveform.read','waveform.raw.read','waveform.export'}
        if intent=='evidence': capabilities.add('inversion.audit.read')
        return self.results._auth(actor,capabilities)

    @staticmethod
    def _eligibility(view,intent):
        reasons=list(view.current_applicability['reasons'])
        if view.review['state']!='approved': reasons.append('result_'+view.review['state'])
        if intent=='quantitative' and reasons: refuse('quantitative_export_restricted')
        if intent=='evidence': reasons.append('evidence_only')
        return sorted(set(reasons))

    def create(self,result_id,intent,format,command):
        if intent not in ('quantitative','evidence') or format not in ('json','csv'): refuse('export_request_invalid')
        if type(command) is not InversionCommand: refuse('command_invalid')
        authority=self._auth(command.actor,intent) # before receipt lookup, including replay
        stored=self.results._load(result_id)
        def apply(cur,cmd):
            expected(cmd,0); view=self.results._view(cur,stored); self._eligibility(view,intent)
            identifier=uuid4()
            # No private actor/review reasons or raw input/result bytes duplicated here.
            historical=dict(execution=view.execution,source=view.source,
                review=dict(state=view.review['state'],version=view.review['version']),
                current_applicability=public_value(view.current_applicability),
                field_validation=public_value(view.field_validation))
            manifest=dict(schema='inversion.export-manifest/1',id=str(identifier),result_id=str(result_id),
                result_sha256=view.content_sha256,request_sha256=view.request_sha256,snapshot_sha256=view.snapshot_sha256,
                intent=intent,format=format,historical=historical)
            digest=sha256(canonical_bytes(manifest)).hexdigest()
            cur.execute('INSERT INTO inversion.result_exports(id,result_id,result_sha256,intent,format,actor_domain,actor_id,manifest,manifest_sha256) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s)',
                (identifier,result_id,view.content_sha256,intent,format,cmd.actor.domain,cmd.actor.user_id,Jsonb(manifest),digest))
            return Change('export.create',identifier,1,digest,dict(export_id=str(identifier),manifest_sha256=digest,authority=authority))
        receipt=self.results.transactions.run(command,apply,request=MutationRequest('export.create',str(result_id),canonical_bytes(dict(intent=intent,format=format))),dependencies=GUARDS)
        return self._record(receipt.subject_id)

    def _record(self,id):
        if type(id) is not UUID: refuse('export_identity_invalid')
        with self.results.read_connect() as conn:
            cur=conn.cursor(); configure(cur)
            cur.execute('SELECT id,result_id,result_sha256,intent,format,actor_domain,actor_id,manifest,manifest_sha256 FROM inversion.result_exports WHERE id=%s',(id,))
            row=cur.fetchone()
        if row is None: refuse('export_missing',404)
        if sha256(canonical_bytes(row[7])).hexdigest()!=row[8]: refuse('export_manifest_corrupt')
        for key,value in dict(id=str(row[0]),result_id=str(row[1]),result_sha256=row[2],intent=row[3],format=row[4]).items():
            if row[7].get(key)!=value: refuse('export_manifest_corrupt')
        return ExportRecord(*row[:7],freeze(row[7]),row[8])

    @staticmethod
    def _bound(record,actor):
        if (record.actor_domain,record.actor_id)!=actor_key(actor): refuse('export_actor_mismatch',403)

    def open(self,id,*,actor):
        self.results._auth(actor,{'inversion.read','inversion.export'})
        record=self._record(id); self._bound(record,actor); self._auth(actor,record.intent)
        context=ResultExportContext(self,record.id,actor,format=record.format)
        self._contexts.add(context)
        return context

    @staticmethod
    def _audit(cur,id,actor,action,response,*,audit_id=None):
        # Private account/session evidence stays only in controlled audit storage.
        from dataclasses import asdict
        identifier=audit_id or uuid4()
        cur.execute('INSERT INTO inversion.audit(id,actor,action,subject_id,reason,request_key,response) VALUES(%s,%s,%s,%s,%s,%s,%s)',
            (identifier,Jsonb(asdict(actor)),action,id,'Audited result export server event',identifier,Jsonb(response)))

    def _start(self,context):
        record=self._record(context.export_id); self._bound(record,context.actor)
        self._auth(context.actor,record.intent)
        stored=self.results._load(record.result_id)
        if stored.prepared.content_sha256!=record.result_sha256: refuse('export_result_changed')
        # Decode and scrub all bounded computation points outside business locks.
        result=public_result(stored.prepared.body)
        chunks=[public_value(json.loads(chunk)) for chunk in stored.prepared.chunks]
        with self.results.connect() as conn:
            cur=conn.cursor(); configure(cur); lock(cur,GUARDS)
            authority=self._auth(context.actor,record.intent)
            view=self.results._view(cur,stored); reasons=self._eligibility(view,record.intent)
            access_id=uuid4(); checked_at=datetime.now(timezone.utc).isoformat()
            access=dict(access_id=str(access_id),checked_at=checked_at,restricted=bool(reasons),restriction_reasons=reasons,
                        current_review=dict(state=view.review['state'],version=view.review['version']))
            self._audit(cur,record.id,context.actor,'export.access_authorized',
                dict(**access,authority=authority,manifest_sha256=record.manifest_sha256,result_sha256=record.result_sha256),audit_id=access_id)
        # Commit success above is required; even an ambiguous commit yields zero bytes.
        context.access_id=access_id
        body=public_value(record.manifest) | dict(schema='inversion.export/1',manifest_sha256=record.manifest_sha256,
            **access,result=result,chunks=chunks,delivery_meaning='Server stream only; no client receipt claim')
        return _serialize(body,record.format)

    def _outcome(self,context,action):
        with self.results.connect() as conn:
            cur=conn.cursor(); configure(cur)
            self._audit(cur,context.export_id,context.actor,action,
                dict(access_id=str(context.access_id),bytes_yielded=context.bytes_yielded,
                     meaning='Server iterator exhaustion' if action=='export.stream_finished' else 'Server stream closed before exhaustion'))

class ResultExportContext:
    """One-use server-issued context. close() is mandatory on adapter disconnect."""
    def __init__(self,exporter,export_id,actor,*,format=None):
        self._exporter,self._export_id,self._actor=exporter,export_id,actor
        self._format=format
        self.access_id=None; self.bytes_yielded=0; self.state='new'; self._iterator=None

    @property
    def exporter(self): return self._exporter
    @property
    def export_id(self): return self._export_id
    @property
    def actor(self): return self._actor
    @property
    def format(self): return self._format

    def __enter__(self): return self
    def __exit__(self,*args): self.close()

    def __iter__(self):
        if self not in self.exporter._contexts or self.state!='new': refuse('export_context_used')
        self.exporter._contexts.remove(self); self.state='started'
        self._iterator=self._stream()
        return self._iterator

    def _stream(self):
        try:
            payload=self.exporter._start(self)
            for offset in range(0,len(payload),65536):
                chunk=payload[offset:offset+65536]; self.bytes_yielded+=len(chunk)
                yield chunk
            self.exporter._outcome(self,'export.stream_finished')
            self.state='finished'
        finally:
            if self.state!='finished':
                self.state='aborted'
                if self.access_id is not None:
                    # A lost audit acknowledgment cannot be presented as success.
                    try: self.exporter._outcome(self,'export.stream_aborted')
                    except Exception: self.state='aborted_audit_unconfirmed'

    def close(self):
        self.exporter._contexts.discard(self)
        if self._iterator is not None: self._iterator.close()
        if self.state in ('new','started'): self.state='closed'
