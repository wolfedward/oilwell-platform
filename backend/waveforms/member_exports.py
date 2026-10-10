"""Internal actor-bound research export of exactly one frozen pair member."""
from dataclasses import dataclass,asdict
from uuid import UUID,uuid4
from psycopg.types.json import Jsonb
from . import business
from .capacity import prepare_transaction,row
from .commands import Actor,Command,_quality_pair_hash
from .contracts import CaptureKey,require_count
from .dataset_contracts import fail,identity,snapshot_hash
from .export import DatasetExporter,ExportContext


@dataclass(frozen=True)
class MemberExportRecord:
    id: UUID
    version_id: UUID
    member_ordinal: int
    dataset_content_sha256: str
    evidence_sha256: str
    actor: Actor


def member_request_hash(version_id,ordinal,command):
    if type(version_id) is not UUID or type(command) is not Command or command.expected_version!=0:
        fail('request_hash','Invalid research command')
    require_count(ordinal,maximum=999)
    return _quality_pair_hash('members.create',str(version_id),command,
                             dict(version_id=str(version_id),member_ordinal=ordinal,purpose='research'))


class MemberResearchExporter(DatasetExporter):
    def __init__(self,*args,**kwargs):
        super().__init__(*args,**kwargs); self.__member_token=object()
        if not callable(self._read_connect): raise ValueError('Explicit member reader required')
        def bounded(factory):
            def connect():
                conn=factory()
                try:
                    conn.execute("SET LOCAL statement_timeout='3s'")
                    conn.execute("SET LOCAL lock_timeout='1s'")
                except BaseException:
                    conn.close(); raise
                return conn
            return connect
        self._connect=bounded(self._connect); self._read_connect=bounded(self._read_connect)

    def _export(self,cur,id,actor):
        if type(id) is not UUID or type(actor) is not Actor: fail('export_context','Invalid export identity')
        cur.execute('SELECT * FROM waveforms.member_research_exports WHERE export_id=%s',(id,)); value=row(cur)
        if value is None: fail('export_not_found','Member export does not exist')
        if identity(Actor(**value['actor']))!=identity(actor): fail('export_actor','Export belongs to another actor')
        return MemberExportRecord(id,value['version_id'],value['member_ordinal'],value['content_sha256'],
                                  value['evidence_sha256'],Actor(**value['actor']))

    def _prepared(self,cur,record):
        version=self._version_row(cur,record.version_id)
        prepared=self.load_member_prepared(cur,record.version_id,record.member_ordinal)
        if (version['content_sha256']!=record.dataset_content_sha256 or
            snapshot_hash('member',prepared.certificate['snapshot'])!=record.evidence_sha256):
            fail('member_identity','Member export identity differs')
        if version['purpose']=='raw_archive' or prepared.applicability:
            fail('export_denied','Member is not currently eligible for research')
        return version,prepared

    def create(self,version_id: UUID,ordinal: int,command: Command) -> MemberExportRecord:
        digest=member_request_hash(version_id,ordinal,command)
        saved=self._receipt(command,digest)
        if saved: return self._record(UUID(saved['id']),command.actor)
        with self._reader() as conn:
            conn.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY'); cur=conn.cursor()
            version=self._version_row(cur,version_id); member=self.load_member_prepared(cur,version_id,ordinal)
        with self._connect() as conn:
            cur=conn.cursor(); self._guard(cur); saved=business.receipt(cur,command,digest)
            if not saved:
                if version['purpose']=='raw_archive' or self.recheck_member(cur,member): fail('export_denied','Member is currently restricted')
                id=uuid4(); evidence=snapshot_hash('member',member.certificate['snapshot'])
                cur.execute('INSERT INTO waveforms.member_research_exports(export_id,version_id,member_ordinal,accounting_bucket,actor,content_sha256,evidence_sha256) VALUES(%s,%s,%s,%s,%s,%s,%s)',
                    (id,version_id,ordinal,self._bucket,Jsonb(asdict(command.actor)),version['content_sha256'],evidence))
                saved=dict(type='member_export',id=str(id))
                business.finish(cur,self._bucket,command,digest,'members.create',id,saved)
                prepare_transaction(cur); conn.commit()
        return self._record(UUID(saved['id']),command.actor)

    def open(self,id: UUID,*,actor: Actor):
        with self._reader() as conn:
            conn.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY'); cur=conn.cursor()
            record=self._export(cur,id,actor); self._prepared(cur,record)
        return MemberExportContext(self,record,actor,None,self.__member_token)

    def _is_context(self,context):
        return type(context) is MemberExportContext and context._exporter is self and context._token is self.__member_token

    def _authorize_download(self,context,key):
        if not self._is_context(context) or type(key) is not CaptureKey: fail('export_context','Member capture context required')
        with self._reader() as conn:
            conn.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY'); cur=conn.cursor()
            record=self._export(cur,context._record.id,context._actor); version,member=self._prepared(cur,record)
            file=member.files.get(key)
            if file is None: fail('export_membership','Capture is outside selected member')
        with self._connect() as conn:
            cur=conn.cursor(); self._guard(cur)
            if self.recheck_member(cur,member): fail('export_denied','Member changed before download')
            cur.execute('INSERT INTO waveforms.business_audit(accounting_bucket,actor,action,subject_id,details) VALUES(%s,%s,%s,%s,%s)',
                (self._bucket,Jsonb(asdict(context._actor)),'members.download',record.version_id,
                 Jsonb(dict(export_id=str(record.id),member_ordinal=record.member_ordinal,evidence_sha256=record.evidence_sha256,
                            capture_pk=file['descriptor']['capture_pk'],intent='research'))))
            prepare_transaction(cur); conn.commit()
        return version,(file['descriptor'],file['plan'])


class MemberExportContext(ExportContext):
    def stream_manifest(self):
        fail('export_membership','Member exports do not expose whole dataset manifests')
