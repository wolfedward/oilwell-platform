"""Append-only drafts and independently reviewed immutable dataset publication."""
from dataclasses import asdict
import json
from uuid import UUID,uuid4
import psycopg
from psycopg.types.json import Jsonb
from backend.registry.transactions import lock_history
from . import business
from .capacity import prepare_transaction,row
from .commands import Actor,dataset_request_hash
from .contracts import require_count
from .dataset_context import DatasetContext
from .dataset_contracts import (DatasetDraft,DatasetVersion,validate_body,stage_manifest,identity,
    SNAPSHOT_VERSIONS,fail,reason_codes)
from .dataset_splits import check_splits
from .evidence_contracts import freeze
from .errors import ContractError,WaveformError
from .pair_contracts import bounded


class DatasetService(DatasetContext):
    def _revision(self,cur,id,revision=None):
        if type(id) is not UUID: raise ContractError('contract_invalid')
        if revision is not None: require_count(revision,minimum=1,maximum=9223372036854775807)
        cur.execute('SELECT r.*,d.creator FROM waveforms.dataset_revisions r JOIN waveforms.dataset_drafts d USING(dataset_id) WHERE r.dataset_id=%s'+
                    (' AND r.revision=%s' if revision is not None else '')+' ORDER BY r.revision DESC LIMIT 1',
                    (id,revision) if revision is not None else (id,))
        value=row(cur)
        if value is None: fail('not_found','Draft revision does not exist')
        return value

    def _draft(self,cur,id,revision=None):
        value=self._revision(cur,id,revision); body=value['body']
        cur.execute("SELECT actor FROM waveforms.dataset_revisions WHERE dataset_id=%s AND state='submitted' AND revision<=%s ORDER BY revision DESC LIMIT 1",(id,value['revision']))
        submitter=cur.fetchone()
        return DatasetDraft(id,value['revision'],value['state'],body['title'],body['purpose'],freeze(body['split_plan']),len(body['members']),
                            Actor(**value['creator']),Actor(**submitter[0]) if submitter else None)

    def _version_row(self,cur,id):
        if type(id) is not UUID: raise ContractError('contract_invalid')
        cur.execute('SELECT * FROM waveforms.dataset_versions WHERE version_id=%s',(id,)); value=row(cur)
        if value is None: fail('not_found','Frozen version does not exist')
        return value

    def _version(self,value,reasons):
        codes=reason_codes(reasons)
        return DatasetVersion(value['version_id'],value['dataset_id'],value['draft_revision'],value['content_sha256'],value['manifest_bytes'],value['purpose'],
                              'restricted' if codes else 'clear',codes,value['member_count'],freeze(SNAPSHOT_VERSIONS),value['metadata']['split_plan']['kind']!='none')

    def get_draft(self,id,revision=None):
        with self._reader() as conn:
            conn.execute('SET TRANSACTION READ ONLY'); return self._draft(conn.cursor(),id,revision)

    def inspect_version(self,id):
        with self._reader() as conn:
            conn.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY'); cur=conn.cursor()
            value=self._version_row(cur,id); prepared=self.load_prepared(cur,id)
        with self._connect() as conn:
            cur=conn.cursor(); self._guard(cur)
            reasons=self.recheck_dataset(cur,prepared,strict=False); conn.rollback()
        return value,prepared,reasons

    def get(self,id):
        value,_,reasons=self.inspect_version(id)
        return self._version(value,reasons)

    def members(self,id,*,offset=0,limit=100):
        require_count(offset,maximum=1000); require_count(limit,minimum=1,maximum=100)
        with self._reader() as conn:
            conn.execute('SET TRANSACTION READ ONLY'); cur=conn.cursor(); self._version_row(cur,id)
            cur.execute('SELECT kind,reference_id,quality_run_id,split FROM waveforms.dataset_members WHERE version_id=%s ORDER BY ordinal OFFSET %s LIMIT %s',(id,offset,limit))
            return tuple(dict(kind=k,reference_id=str(r),quality_run_id=str(q),split=s) for k,r,q,s in cur.fetchall())

    def draft_members(self,id,*,revision=None,offset=0,limit=100):
        require_count(offset,maximum=1000); require_count(limit,minimum=1,maximum=100)
        with self._reader() as conn:
            conn.execute('SET TRANSACTION READ ONLY'); value=self._revision(conn.cursor(),id,revision)
            return tuple(freeze(m) for m in value['body']['members'][offset:offset+limit])

    def _guard(self,cur):
        prepare_transaction(cur); business.lock_dependencies(cur); lock_history(cur,shared=True); business.lock_payer(cur,self._bucket,self._limits)

    def _receipt(self,command,digest):
        if digest!=command.request_hash: fail('request_hash','Command does not bind actual content')
        with self._connect() as conn:
            cur=conn.cursor(); prepare_transaction(cur); business.lock_dependencies(cur); business.check_payer(cur,self._bucket)
            saved=business.receipt(cur,command,digest); conn.rollback(); return saved

    def _resolve(self,saved):
        return self.get(UUID(saved['version_id'])) if saved['type']=='version' else self.get_draft(UUID(saved['id']),saved['revision'])

    def _append(self,cur,id,revision,state,body,command,version_id=None):
        cur.execute('INSERT INTO waveforms.dataset_revisions(dataset_id,revision,accounting_bucket,state,body,actor) VALUES(%s,%s,%s,%s,%s,%s)',
                    (id,revision,self._bucket,state,Jsonb(body),Jsonb(asdict(command.actor))))
        cur.execute('INSERT INTO waveforms.dataset_events(event_id,dataset_id,revision,accounting_bucket,state,version_id,actor,reason) VALUES(%s,%s,%s,%s,%s,%s,%s,%s)',
                    (uuid4(),id,revision,self._bucket,state,version_id,Jsonb(asdict(command.actor)),command.reason))

    def create(self,body,command): return self._edit('create',body,command)
    def update(self,body,command): return self._edit('update',body,command)
    def submit(self,body,command): return self._edit('submit',body,command)

    def _edit(self,operation,body,command):
        body=validate_body(operation,body); target=None if operation=='create' else UUID(body['target_id'])
        digest=dataset_request_hash(operation,target,command,body=body)
        saved=self._receipt(command,digest)
        if saved: return self._resolve(saved)
        try:
            with self._connect() as conn:
                cur=conn.cursor(); self._guard(cur); saved=business.receipt(cur,command,digest)
                if saved: conn.rollback()
                else:
                    if operation=='create':
                        target=uuid4(); revision=1; content=body|dict(members=[]); state='draft'
                        cur.execute('INSERT INTO waveforms.dataset_drafts(dataset_id,accounting_bucket,creator) VALUES(%s,%s,%s)',(target,self._bucket,Jsonb(asdict(command.actor))))
                    else:
                        old=self._revision(cur,target)
                        if old['revision']!=command.expected_version: fail('version_conflict','Draft revision changed')
                        if old['state']!='draft': fail('terminal','Submitted revisions cannot be edited or resubmitted')
                        revision=old['revision']+1; content=old['body']; state='submitted' if operation=='submit' else 'draft'
                        if operation=='submit':
                            if not content['members']: fail('empty','Cannot submit an empty dataset')
                        else:
                            change=body['changes']
                            if 'metadata' in change: content=change['metadata']|dict(members=content['members'])
                            else:
                                entries={(m['kind'],m['reference_id']):m for m in content['members']}
                                if 'upsert_members' in change:
                                    for member in change['upsert_members']: entries[(member['kind'],member['reference_id'])]=member
                                else:
                                    for member in change['remove_members']: entries.pop((member['kind'],member['reference_id']),None)
                                if len(entries)>1000: fail('member_limit','Dataset exceeds1000 members')
                                content=content|dict(members=[entries[key] for key in sorted(entries)])
                    bounded(content,524288); self._append(cur,target,revision,state,content,command)
                    saved=dict(type='draft',id=str(target),revision=revision)
                    business.finish(cur,self._bucket,command,digest,'datasets.'+operation,target,saved)
                    prepare_transaction(cur); conn.commit()
            return self._resolve(saved)
        except psycopg.Error as error: raise WaveformError('waveform.dataset_unavailable','Dataset transaction failed; no durable success') from error

    def freeze(self,body,command):
        body=validate_body('freeze',body); target=UUID(body['target_id']); digest=dataset_request_hash('freeze',target,command,body=body)
        saved=self._receipt(command,digest)
        if saved: return self._resolve(saved)
        with self._reader() as conn:
            conn.execute('SET TRANSACTION READ ONLY'); draft=self._revision(conn.cursor(),target)
        self._freeze_check(draft,command)
        content=draft['body']; prepared=self.prepare_dataset(content['members'],content['purpose'],command.actor)
        members=[c['member']|dict(snapshot=c['snapshot']) for c in prepared['certificates']]
        check_splits(members,content['split_plan']); version_id=uuid4()
        header=dict(protocol='waveform.dataset-manifest/1',dataset_id=str(target),version_id=str(version_id),draft_revision=str(draft['revision']),
            title=content['title'],purpose=content['purpose'],source_kinds=prepared['source_kinds'],split_plan=content['split_plan'],
            creator=identity(Actor(**draft['creator'])),reviewer=identity(command.actor),snapshot_versions=SNAPSHOT_VERSIONS)
        bounded(header,16384)
        files=list(prepared['files'].values()); manifest=header|dict(members=members,files=[f['descriptor'] for f in files])
        # Adapt pre-serialized bounded values: publication only sends staged bytes.
        def frozen_json(value):
            text=json.dumps(value,ensure_ascii=False,separators=(',',':'),allow_nan=False)
            return Jsonb(value,dumps=lambda ignored:text)
        snapshots=[frozen_json(m['snapshot']) for m in members]
        file_values=[(frozen_json(f['descriptor']),frozen_json(f['plan'])) for f in files]
        try:
            with stage_manifest(manifest) as staged:
                with self._connect() as conn:
                    cur=conn.cursor(); self._guard(cur); saved=business.receipt(cur,command,digest)
                    if saved: conn.rollback()
                    else:
                        self._freeze_check(self._revision(cur,target),command)
                        self.recheck_dataset(cur,prepared,strict=True)
                        cur.execute('INSERT INTO waveforms.dataset_versions(version_id,dataset_id,draft_revision,accounting_bucket,purpose,metadata,content_sha256,manifest_bytes,member_count,file_count,chunk_count,reviewer) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)',
                            (version_id,target,draft['revision'],self._bucket,content['purpose'],Jsonb(header),staged.content_sha256,staged.length,len(members),len(files),staged.chunk_count,Jsonb(asdict(command.actor))))
                        for ordinal,(m,snapshot) in enumerate(zip(members,snapshots)):
                            cur.execute('INSERT INTO waveforms.dataset_members(version_id,ordinal,accounting_bucket,kind,reference_id,quality_run_id,split,snapshot) VALUES(%s,%s,%s,%s,%s,%s,%s,%s)',
                                (version_id,ordinal,self._bucket,m['kind'],UUID(m['reference_id']),UUID(m['quality_run_id']),m['split'],snapshot))
                        for ordinal,(file,values) in enumerate(zip(files,file_values)):
                            cur.execute('INSERT INTO waveforms.dataset_files(version_id,ordinal,accounting_bucket,capture_pk,descriptor,plan) VALUES(%s,%s,%s,%s,%s,%s)',
                                (version_id,ordinal,self._bucket,UUID(file['descriptor']['capture_pk']),*values))
                        for ordinal,payload in enumerate(staged.chunks()):
                            cur.execute('INSERT INTO waveforms.dataset_manifest_chunks(version_id,ordinal,accounting_bucket,payload) VALUES(%s,%s,%s,%s)',(version_id,ordinal,self._bucket,payload))
                        self._append(cur,target,draft['revision']+1,'frozen',content,command,version_id)
                        saved=dict(type='version',id=str(target),revision=draft['revision']+1,version_id=str(version_id),content_sha256=staged.content_sha256)
                        business.finish(cur,self._bucket,command,digest,'datasets.freeze',target,saved); prepare_transaction(cur); conn.commit()
            return self._resolve(saved)
        except psycopg.Error as error: raise WaveformError('waveform.dataset_unavailable','Freeze transaction failed; no durable success') from error

    def _freeze_check(self,draft,command):
        if draft['revision']!=command.expected_version: fail('version_conflict','Submitted revision changed')
        if draft['state']!='submitted': fail('terminal','Only submitted drafts may freeze')
        who=identity(command.actor)
        if who in (identity(Actor(**draft['creator'])),identity(Actor(**draft['actor']))): fail('self_review','Creator and submitter cannot freeze their own draft')
