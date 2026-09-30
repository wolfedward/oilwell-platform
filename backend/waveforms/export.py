"""Audited, reproducible dataset downloads through actor-bound contexts."""
from dataclasses import dataclass,asdict
from hashlib import sha256
from uuid import UUID,uuid4
import psycopg
from psycopg.types.json import Jsonb
from . import business
from .capacity import prepare_transaction,row
from .commands import Actor,export_request_hash
from .contracts import CaptureKey,require_fields,require_uuid,require_count
from .dataset_contracts import fail,identity
from .datasets import DatasetService
from .evidence_contracts import freeze
from .errors import ContractError,WaveformError
from .pair_contracts import capture_key


@dataclass(frozen=True)
class ExportRecord:
    id: UUID
    version_id: UUID
    dataset_content_sha256: str
    dataset_purpose: str
    intent: str
    actor: Actor


@dataclass(frozen=True)
class ExportManifest:
    version_id: UUID
    dataset_content_sha256: str
    manifest_bytes: int
    dataset_purpose: str
    intent: str
    restriction_state: str
    restriction_reasons: tuple
    allowed: bool
    denial_reasons: tuple
    independent_split: bool
    file_count: int
    source_kinds: tuple


def intent_denials(value,intent,reasons):
    if intent not in ('research','training','evidence'): raise ContractError('contract_invalid')
    denials=[]
    if intent!='evidence':
        if value['purpose']=='raw_archive': denials.append('raw_archive_only')
        if reasons: denials.append('currently_restricted')
        if intent=='training' and value['metadata']['split_plan']['kind']=='none': denials.append('independent_split_required')
    return tuple(denials)


class DatasetExporter(DatasetService):
    def __init__(self,*args,**kwargs):
        super().__init__(*args,**kwargs); self.__token=object()

    def _metadata(self,value,intent,reasons):
        denials=intent_denials(value,intent,reasons)
        return ExportManifest(value['version_id'],value['content_sha256'],value['manifest_bytes'],value['purpose'],intent,
            'restricted' if reasons else 'clear',tuple(reasons),not denials,denials,value['metadata']['split_plan']['kind']!='none',
            value['file_count'],tuple(value['metadata']['source_kinds']))

    def describe(self,version_id,*,purpose,actor):
        if type(actor) is not Actor: raise ContractError('contract_invalid')
        value,_,reasons=self.inspect_version(version_id)
        return self._metadata(value,purpose,reasons)

    def files(self,version_id,*,actor,offset=0,limit=100):
        if type(actor) is not Actor: raise ContractError('contract_invalid')
        require_count(offset,maximum=2000); require_count(limit,minimum=1,maximum=100)
        with self._reader() as conn:
            conn.execute('SET TRANSACTION READ ONLY'); self._version_row(conn.cursor(),version_id)
            return tuple(freeze(r[0]) for r in conn.execute('SELECT descriptor FROM waveforms.dataset_files WHERE version_id=%s ORDER BY ordinal OFFSET %s LIMIT %s',(version_id,offset,limit)).fetchall())

    def _export(self,cur,id,actor):
        if type(id) is not UUID or type(actor) is not Actor: raise ContractError('contract_invalid')
        cur.execute('SELECT e.*,v.purpose FROM waveforms.dataset_exports e JOIN waveforms.dataset_versions v USING(version_id) WHERE export_id=%s',(id,)); record=row(cur)
        if record is None: fail('export_not_found','Export does not exist')
        if identity(Actor(**record['actor']))!=identity(actor): fail('export_actor','Export belongs to another actor')
        return ExportRecord(record['export_id'],record['version_id'],record['content_sha256'],record['purpose'],record['intent'],Actor(**record['actor']))

    def _record(self,id,actor):
        with self._reader() as conn:
            conn.execute('SET TRANSACTION READ ONLY'); return self._export(conn.cursor(),id,actor)

    def create(self,body,command):
        require_fields(body,('version_id','purpose')); version=require_uuid(body['version_id'])
        digest=export_request_hash(version,command,body=body); saved=self._receipt(command,digest)
        if saved: return self._record(UUID(saved['id']),command.actor)
        with self._reader() as conn:
            conn.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY'); cur=conn.cursor()
            value=self._version_row(cur,version); prepared=self.load_prepared(cur,version)
        try:
            with self._connect() as conn:
                cur=conn.cursor(); self._guard(cur); saved=business.receipt(cur,command,digest)
                if saved: conn.rollback()
                else:
                    reasons=self.recheck_dataset(cur,prepared,strict=False)
                    if intent_denials(value,body['purpose'],reasons): fail('export_denied','Export intent is not currently permitted')
                    id=uuid4()
                    cur.execute('INSERT INTO waveforms.dataset_exports(export_id,version_id,accounting_bucket,intent,actor,content_sha256) VALUES(%s,%s,%s,%s,%s,%s)',
                        (id,version,self._bucket,body['purpose'],Jsonb(asdict(command.actor)),value['content_sha256']))
                    saved=dict(type='export',id=str(id),version_id=str(version),content_sha256=value['content_sha256'])
                    business.finish(cur,self._bucket,command,digest,'exports.create',id,saved); prepare_transaction(cur); conn.commit()
            return self._record(UUID(saved['id']),command.actor)
        except psycopg.Error as error: raise WaveformError('waveform.export_unavailable','Export creation failed; no durable success') from error

    def open(self,export_id,*,actor):
        record=self._record(export_id,actor); metadata=self.describe(record.version_id,purpose=record.intent,actor=actor)
        if not metadata.allowed: fail('export_denied','Export intent is no longer permitted')
        return ExportContext(self,record,actor,metadata,self.__token)

    def _is_context(self,context):
        return type(context) is ExportContext and context._exporter is self and context._token is self.__token

    def _authorize_download(self,context,key):
        if not self._is_context(context): fail('export_context','Server export context required')
        with self._reader() as conn:
            conn.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY'); cur=conn.cursor()
            record=self._export(cur,context._record.id,context._actor); value=self._version_row(cur,record.version_id); prepared=self.load_prepared(cur,record.version_id)
            file=None
            if key is not None:
                if type(key) is not CaptureKey: raise ContractError('contract_invalid')
                cur.execute('SELECT descriptor,plan FROM waveforms.dataset_files WHERE version_id=%s AND descriptor->\'key\'->>\'gateway_id\'=%s AND descriptor->\'key\'->>\'device_id\'=%s AND descriptor->\'key\'->>\'capture_id\'=%s',
                    (record.version_id,key.gateway_id,key.device_id,str(key.capture_id)))
                file=cur.fetchone()
                if file is None: fail('export_membership','Capture does not belong to the frozen version')
        try:
            with self._connect() as conn:
                cur=conn.cursor(); self._guard(cur); reasons=self.recheck_dataset(cur,prepared,strict=False)
                metadata=self._metadata(value,record.intent,reasons)
                if not metadata.allowed: fail('export_denied','Export intent changed before download')
                details=dict(export_id=str(record.id),intent=record.intent,kind='capture' if file else 'manifest',filename=file[0]['filename'] if file else 'dataset.json')
                cur.execute('INSERT INTO waveforms.business_audit(accounting_bucket,actor,action,subject_id,details) VALUES(%s,%s,%s,%s,%s)',
                    (self._bucket,Jsonb(asdict(context._actor)),'exports.download',record.version_id,Jsonb(details)))
                prepare_transaction(cur); conn.commit()
            context.metadata=metadata
            return value,file
        except psycopg.Error as error: raise WaveformError('waveform.export_unavailable','Download audit failed; no bytes authorized') from error


class ExportContext:
    """One server-created request. First advance audits; no DB handle survives yield.

    Task13 must reauthorize live capabilities before each open, prime the iterator
    before sending headers, and call iterator.close() on disconnect. No persistent
    cursor, transaction, temporary file or raw buffer is owned by an idle context.
    """
    def __init__(self,exporter,record,actor,metadata,token=None):
        self._exporter=exporter; self._record=record; self._actor=actor; self._token=token
        self.metadata=metadata; self._used=False; self._closed=False
        if not exporter._is_context(self): fail('export_context','Context must be created by exporter.open')

    def _claim(self):
        if self._used or self._closed: fail('export_context','A new context is required for each download or retry')
        self._used=True

    def close(self): self._closed=True

    def stream_capture(self,key):
        self._claim(); digest=sha256(); length=0
        try:
            _,file=self._exporter._authorize_download(self,key); descriptor,plan=file
            for fact in plan:
                if self._closed: return
                raw=self._exporter._raw(UUID(fact['fact_id']),self._actor)
                if len(raw)!=fact['raw_length'] or sha256(raw).hexdigest()!=fact['raw_sha256']: fail('file_identity','Immutable download bytes differ')
                digest.update(raw); digest.update(b'\n'); length+=len(raw)+1
                yield raw
                if self._closed: return
                yield b'\n'
            if length!=descriptor['content_length'] or digest.hexdigest()!=descriptor['file_sha256']: fail('file_identity','Frozen file digest differs')
        finally: self.close()

    def stream_manifest(self):
        self._claim(); digest=sha256(); length=0
        try:
            value,_=self._exporter._authorize_download(self,None)
            for ordinal in range(value['chunk_count']):
                if self._closed: return
                with self._exporter._reader() as conn:
                    conn.execute('SET TRANSACTION READ ONLY')
                    result=conn.execute('SELECT payload FROM waveforms.dataset_manifest_chunks WHERE version_id=%s AND ordinal=%s',(value['version_id'],ordinal)).fetchone()
                if result is None: fail('manifest_identity','Frozen manifest chunk missing')
                raw=bytes(result[0]); digest.update(raw); length+=len(raw); yield raw
            if length!=value['manifest_bytes'] or digest.hexdigest()!=value['content_sha256']: fail('manifest_identity','Frozen manifest digest differs')
        finally: self.close()


def lf_lines(lines):
    for payload in lines:
        yield payload
        yield b'\n'
