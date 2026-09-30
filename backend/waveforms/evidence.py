"""Immutable reviewed documentation, controlled attachments, and append revocation."""
from dataclasses import asdict
from hashlib import sha256
import json
from uuid import UUID, uuid4
import psycopg
from psycopg.types.json import Jsonb

from . import business
from .capacity import prepare_transaction, row
from .commands import Actor, Command, evidence_request_hash
from .config import CapacityLimits
from .contracts import CaptureKey,valid_name
from .errors import ContractError, WaveformError
from .evidence_contracts import (EvidenceVersion, Attachment, Restriction, freeze,thaw,
    evidence_content, validate_body, validate_attachment,field_profile_complete,sync_proof)


def _fail(code,message): raise WaveformError('waveform.evidence_'+code,message,409)


class EvidenceService:
    def __init__(self,connect,*,limits,shared_accounting_bucket,read_connect=None):
        if not callable(connect) or type(limits) is not CapacityLimits or not valid_name(shared_accounting_bucket) or shared_accounting_bucket not in limits.gateways:
            raise ValueError('Explicit operator connection, capacities and configured business payer required')
        if read_connect is not None and not callable(read_connect): raise ValueError('Explicit reader connection required')
        self._connect,self._read_connect=connect,read_connect
        self._limits,self._bucket=limits,shared_accounting_bucket

    def _reader(self):
        if self._read_connect is None:
            raise WaveformError('waveform.reader_required','Controlled reader connection was not configured')
        return self._read_connect()

    def create(self,kind,body,command):
        body=validate_body(kind,body)
        return self._execute('create',None,command,kind=kind,body=body)

    def review(self,id,*,decision,command):
        return self._execute('review',id,command,decision=decision)

    def revoke(self,id,command): return self._execute('revoke',id,command)

    def upload_attachment(self,payload,*,media_type,command):
        validate_attachment(payload,media_type)
        return self._execute('upload',None,command,payload=payload,attachment={
            'media_type':media_type,'payload_bytes':len(payload),'payload_sha256':sha256(payload).hexdigest()})

    def _get(self,cur,id,*,version=None,state=None):
        cur.execute('SELECT * FROM waveforms.evidence_versions WHERE evidence_id=%s',(id,))
        item=row(cur)
        if item is None: _fail('not_found','Evidence version does not exist')
        if version is None:
            cur.execute('SELECT version,state FROM waveforms.evidence_events WHERE evidence_id=%s ORDER BY version DESC LIMIT 1',(id,))
            version,state=cur.fetchone()
        return EvidenceVersion(item['evidence_id'],item['kind'],item['content_sha256'],freeze(item['body']),
            Actor(**item['creator']),version,state,item['accounting_bucket'])

    def _decode(self,cur,result):
        if result['type']=='evidence':
            return self._get(cur,UUID(result['id']),version=result['version'],state=result['state'])
        if result['type']=='attachment':
            cur.execute('SELECT attachment_id,media_type,payload_bytes,payload_sha256,accounting_bucket FROM waveforms.attachment_metadata WHERE attachment_id=%s',(UUID(result['id']),))
            return Attachment(*cur.fetchone())
        cur.execute('SELECT restriction_id,evidence_id,reason_code,recorded_at,source_event_id FROM waveforms.restrictions WHERE restriction_id=%s',(UUID(result['id']),))
        id,subject,reason,at,event=cur.fetchone()
        return Restriction(id,'evidence',subject,reason,at,event)

    def _execute(self,operation,target,command,**values):
        payload=values.pop('payload',None)
        digest=evidence_request_hash(operation,target,command,**values)
        if digest!=command.request_hash: _fail('request_hash','Command does not bind actual content')
        # A persisted replay needs no new raw read and therefore no duplicate
        # read-audit charge. The write transaction repeats this race check.
        with self._connect() as replay:
            cur=replay.cursor(); prepare_transaction(cur)
            business.lock_dependencies(cur); business.check_payer(cur,self._bucket)
            saved=business.receipt(cur,command,digest)
            if saved is not None:
                result=self._decode(cur,saved); replay.rollback(); return result
            replay.rollback()
        source_body=None; source_kind=None
        if operation=='create': source_kind,source_body=values['kind'],values['body']
        elif operation=='review':
            with self._connect() as reading:
                reading.execute('SET TRANSACTION READ ONLY')
                current=self._get(reading.cursor(),target)
                source_kind,source_body=current.kind,thaw(current.body)
        # Audits acquire physical source quota separately, before any business lock.
        sources=self._prepare_sources(source_kind,source_body,command.actor) if source_kind in ('origin','sync') else ({},())
        try:
            with self._connect() as conn:
                cur=conn.cursor(); prepare_transaction(cur)
                business.lock_dependencies(cur)
                business.check_payer(cur,self._bucket)
                previous=business.receipt(cur,command,digest)
                if previous is not None:
                    result=self._decode(cur,previous); conn.rollback(); return result
                business.lock_payer(cur,self._bucket,self._limits)
                self._recheck_sources(cur,sources[1])
                if operation=='upload':
                    target=uuid4(); metadata=values['attachment']
                    cur.execute('INSERT INTO waveforms.attachments(attachment_id,accounting_bucket,media_type,payload,payload_bytes,payload_sha256,creator) VALUES(%s,%s,%s,%s,%s,%s,%s)',
                        (target,self._bucket,metadata['media_type'],payload,metadata['payload_bytes'],metadata['payload_sha256'],Jsonb(asdict(command.actor))))
                    result={'type':'attachment','id':str(target)}
                elif operation=='create':
                    target=uuid4(); kind=values['kind']; body=values['body']
                    content,content_hash=evidence_content(kind,body)
                    cur.execute('INSERT INTO waveforms.evidence_versions(evidence_id,accounting_bucket,kind,canonical_content,content_sha256,body,creator) VALUES(%s,%s,%s,%s,%s,%s,%s)',
                        (target,self._bucket,kind,content,content_hash,Jsonb(body),Jsonb(asdict(command.actor))))
                    self._event(cur,target,1,'draft',command)
                    result={'type':'evidence','id':str(target),'version':1,'state':'draft'}
                else:
                    item=self._get(cur,target)
                    if item.version!=command.expected_version: _fail('version_conflict','Evidence revision changed')
                    if item.review_state=='revoked': _fail('terminal','Evidence already revoked')
                    version=item.version+1
                    if operation=='review':
                        if (item.creator.domain,item.creator.user_id)==(command.actor.domain,command.actor.user_id):
                            _fail('self_review','Creator cannot review their own evidence')
                        if item.review_state!='draft': _fail('terminal','Evidence already reviewed')
                        state='approved' if values['decision']=='approve' else 'rejected'
                        if state=='approved' and item.kind=='profile' and not field_profile_complete(thaw(item.body)):
                            _fail('profile_incomplete','Field profile requires explicit trial conditions and required electrical quantities')
                        event=self._event(cur,target,version,state,command)
                        result={'type':'evidence','id':str(target),'version':version,'state':state}
                    else:
                        event=self._event(cur,target,version,'revoked',command)
                        restriction=uuid4()
                        cur.execute("INSERT INTO waveforms.restrictions(restriction_id,accounting_bucket,evidence_id,source_event_id,reason_code) VALUES(%s,%s,%s,%s,'evidence_revoked')",(restriction,self._bucket,target,event))
                        result={'type':'restriction','id':str(restriction)}
                business.finish(cur,self._bucket,command,digest,'attachments.upload' if operation=='upload' else 'evidence.'+operation,target,result)
                prepared=self._decode(cur,result)
                prepare_transaction(cur)
                conn.commit()
                return prepared
        except psycopg.Error as error:
            raise WaveformError('waveform.evidence_unavailable','Evidence transaction failed; no durable success is returned') from error

    def _event(self,cur,id,version,state,command):
        event=uuid4()
        cur.execute('INSERT INTO waveforms.evidence_events(event_id,evidence_id,version,accounting_bucket,state,actor,reason) VALUES(%s,%s,%s,%s,%s,%s,%s)',
            (event,id,version,self._bucket,state,Jsonb(asdict(command.actor)),command.reason))
        return event

    def get(self,id):
        if type(id) is not UUID: raise ContractError('contract_invalid')
        with self._reader() as conn:
            conn.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY')
            conn.execute("SET LOCAL statement_timeout='3s'")
            cur=conn.cursor(); business.check_payer(cur,self._bucket)
            return self._get(cur,id)

    def download_attachment(self,id,*,actor):
        if type(id) is not UUID or type(actor) is not Actor: raise ContractError('contract_invalid')
        with self._reader() as conn:
            cur=conn.cursor(); prepare_transaction(cur)
            business.check_payer(cur,self._bucket)
            cur.execute('SELECT media_type,payload_bytes,payload_sha256 FROM waveforms.attachment_metadata WHERE attachment_id=%s',(id,))
            metadata=cur.fetchone()
            if metadata is None: _fail('not_found','Attachment does not exist')
            cur.execute('SELECT waveforms.audited_attachment(%s,%s,%s)',(id,Jsonb(asdict(actor)),self._bucket))
            payload=bytes(cur.fetchone()[0])
            prepare_transaction(cur); conn.commit()
        return {'payload':payload,'media_type':metadata[0],'bytes':metadata[1],'sha256':metadata[2],
                'content_disposition':'attachment; filename="'+str(id)+'"','x_content_type_options':'nosniff'}

    def _prepare_sources(self,kind,body,actor):
        from .wire import parse_frame
        caps=[body['capture']] if kind=='origin' else [x['endpoint']['capture'] for x in body['endpoints']]
        keys=tuple(dict.fromkeys(CaptureKey(c['gateway_id'],c['device_id'],UUID(c['capture_id'])) for c in caps))
        manifests={}; references=[]
        # Each raw audit locks its actual intake quota. Commit separately so
        # reversed caller endpoint orders never accumulate cross-gateway locks.
        for key in keys:
            with self._reader() as conn:
                cur=conn.cursor(); prepare_transaction(cur)
                cur.execute('SELECT capture_pk,manifest_fact_id,manifest_sha256 FROM waveforms.capture_metadata WHERE gateway_id=%s AND device_id=%s AND capture_id=%s',
                            (key.gateway_id,key.device_id,key.capture_id))
                capture=cur.fetchone()
                if capture is None or capture[1] is None: continue
                cur.execute('SELECT waveforms.audited_raw(%s,%s)',(capture[1],Jsonb(asdict(actor))))
                raw=bytes(cur.fetchone()[0])
                prepare_transaction(cur); conn.commit()
            frame=parse_frame(raw)
            if frame.key!=key or frame.raw_sha256!=capture[2]: _fail('source_changed','Immutable manifest identity differs')
            manifests[key]=frame; references.append(capture)
        if kind=='sync': sync_proof(body,manifests,approved=False)
        return manifests,tuple(references)

    def _recheck_sources(self,cur,references):
        if not references: return
        cur.execute('SELECT waveforms.lock_business_captures(%s)',([r[0] for r in references],))
        for id,fact,digest in references:
            cur.execute('SELECT manifest_fact_id,manifest_sha256 FROM waveforms.capture_metadata WHERE capture_pk=%s',(id,))
            if cur.fetchone()!=(fact,digest): _fail('source_changed','Manifest changed during evidence preparation')
            # Preserve actual late conflicts. Reading them never rewrites evidence.
            cur.execute('SELECT restriction_id FROM waveforms.capture_restrictions WHERE subject_id=%s',(id,))
            cur.fetchall()

    def inspect(self,id,*,actor):
        """Current proof snapshot for Task11; approval does not certify instrument truth.

        Capture/provenance/manifest references are explicit. Missing file seals
        remain pending independently from accepted online paths to shared bytes.
        """
        if type(actor) is not Actor: raise ContractError('contract_invalid')
        item=self.get(id); body=thaw(item.body)
        if item.kind not in ('origin','sync'):
            _fail('proof_kind','Source proof inspection accepts only origin and sync evidence')
        manifests,refs=self._prepare_sources(item.kind,body,actor)
        with self._connect() as conn:
            cur=conn.cursor(); prepare_transaction(cur)
            business.lock_dependencies(cur); business.lock_payer(cur,self._bucket,self._limits)
            self._recheck_sources(cur,refs)
            current=self._get(cur,id)
            conflicts=[]
            for reference in refs:
                cur.execute('SELECT restriction_id FROM waveforms.capture_restrictions WHERE subject_id=%s',(reference[0],))
                conflicts.extend(str(r[0]) for r in cur.fetchall())
            if item.kind=='sync': proof=sync_proof(body,manifests,approved=current.review_state=='approved')
            else:
                p=body['provenance']
                cur.execute('SELECT * FROM waveforms.provenance_metadata WHERE observation_id=%s AND outcome_id=%s AND fact_id=%s AND source_ref=%s',
                            tuple(UUID(p[n]) for n in ('observation_id','outcome_id','fact_id','source_ref')))
                origin=row(cur)
                proof=self._origin_proof(body,manifests,origin,current.review_state)
            if conflicts or current.review_state=='revoked':
                proof=dict(proof,status='ineligible',reasons=tuple(proof['reasons'])+(('capture_conflict',) if conflicts else ('calibration_expired_or_revoked',)))
            # Inspection has no new durable result; raw audits were committed earlier.
            conn.rollback()
        return dict(proof,id=item.id,content_sha256=item.content_sha256,restrictions=tuple(conflicts))

    def _origin_proof(self,body,manifests,origin,review_state):
        reasons=[]; cap=body['capture']; p=body['provenance']
        key=CaptureKey(cap['gateway_id'],cap['device_id'],UUID(cap['capture_id']))
        frame=manifests.get(key)
        if origin is None or frame is None: reasons.append('evidence_unavailable')
        else:
            if (origin['status']!='stored' or origin['reason_code']!='accepted' or origin['kind']!='manifest'
                or (origin['gateway_id'],origin['device_id'],str(origin['capture_id']))!=(key.gateway_id,key.device_id,str(key.capture_id))
                or origin['payload_sha256']!=p['manifest_sha256'] or frame.raw_sha256!=p['manifest_sha256']
                or origin['transport']!=p['transport'] or body['source_kind']!=frame.body['source_kind']
                or not any(s.source==body['source_identity'] for s in frame.streams)):
                reasons.append('unknown_origin')
            if p['transport']=='file':
                if origin['import_id'] is None or str(origin['import_id'])!=p['import_id'] or origin['line_number'] is None or origin['import_state']!='complete':
                    reasons.append('import_not_sealed')
            elif not body['broker_config_ref']: reasons.append('unknown_origin')
        if review_state!='approved' or any(not body[n] or not body[n].strip() for n in ('verification_method','verification_basis')):
            reasons.append('evidence_unavailable')
        return {'status':'ineligible' if 'unknown_origin' in reasons else 'pending' if reasons else 'resolved',
                'reasons':tuple(dict.fromkeys(reasons)),'provenance':freeze(p)}
