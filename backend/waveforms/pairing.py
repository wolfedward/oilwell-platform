"""Immutable jointly evaluated candidates and independent append-only review."""
from dataclasses import asdict
from uuid import UUID,uuid4
import psycopg
from psycopg.types.json import Jsonb

from . import business
from .capacity import prepare_transaction,row
from .commands import Actor,pair_request_hash
from .errors import WaveformError,ContractError
from .evidence_contracts import freeze,thaw
from .pair_contracts import (PairVersion,PairReview,PairInspection,METHOD_PARAMETERS,
    bounded,validate_proposal,pair_content,public)
from .quality import evaluate
from .quality_service import QualityService


def refuse(code,message): raise WaveformError('waveform.pair_'+code,message,409)


class PairService(QualityService):
    def _pair(self,cur,id,*,version=None,state=None):
        cur.execute('SELECT * FROM waveforms.pair_versions WHERE pair_version_id=%s',(id,)); item=row(cur)
        if item is None: refuse('not_found','Candidate does not exist')
        if version is None:
            cur.execute('SELECT version,decision FROM waveforms.pair_reviews WHERE pair_version_id=%s ORDER BY version DESC LIMIT 1',(id,))
            review=cur.fetchone(); version,state=review if review else (1,'proposed')
        if pair_content(item['body'],item['method_version'])!=item['content_sha256']: refuse('identity','Candidate content differs')
        return PairVersion(item['pair_version_id'],version,item['content_sha256'],item['left_quality_run_id'],item['right_quality_run_id'],
                           item['method_version'],freeze(item['body']),Actor(**item['creator']),state)

    def _decode_pair(self,cur,saved):
        if saved['type']=='pair': return self._pair(cur,UUID(saved['id']),version=saved['version'],state=saved['state'])
        cur.execute('SELECT pair_version_id,version,decision,actor,reason FROM waveforms.pair_reviews WHERE pair_version_id=%s AND version=%s',(UUID(saved['id']),saved['version']))
        id,version,decision,actor,reason=cur.fetchone()
        return PairReview(id,version,decision,Actor(**actor),reason)

    def propose(self,body,command):
        body=validate_proposal(body); digest=pair_request_hash('propose',None,command,body=body)
        try:
            replay=self._replay(command,digest,self._decode_pair)
            if replay is not None: return replay
            prepared=self.prepare(body['selection'],command.actor)
            with self._connect() as conn:
                cur=conn.cursor(); saved=self._begin(cur,command,digest)
                if saved:
                    result=self._decode_pair(cur,saved); conn.rollback(); return result
                context,profile,_=self.recheck(cur,prepared); quality=evaluate(context,profile=profile)
                runs=self._write_runs(cur,prepared,context,profile,quality,command.actor)
                id=uuid4(); left,right=runs.runs[0].id,runs.runs[-1].id
                content=bounded(dict(selection=body['selection'],selection_sha256=runs.selection_sha256,quality_run_ids=[str(left),str(right)],
                    joint_result=self._outcome(context,quality),physical_cycles=public(context.cycle['physical_cycles']),method_parameters=METHOD_PARAMETERS))
                cur.execute('INSERT INTO waveforms.pair_versions(pair_version_id,accounting_bucket,left_quality_run_id,right_quality_run_id,method_version,body,content_sha256,creator) VALUES(%s,%s,%s,%s,%s,%s,%s,%s)',
                    (id,self._bucket,left,right,body['method_version'],Jsonb(content),pair_content(content,body['method_version']),Jsonb(asdict(command.actor))))
                saved=dict(type='pair',id=str(id),version=1,state='proposed')
                business.finish(cur,self._bucket,command,digest,'pairs.propose',id,saved)
                result=self._decode_pair(cur,saved); prepare_transaction(cur); conn.commit(); return result
        except psycopg.Error as error:
            raise WaveformError('waveform.pair_unavailable','Candidate transaction failed; no durable success') from error

    def get(self,id):
        if type(id) is not UUID: raise ContractError('contract_invalid')
        with self._reader() as conn:
            conn.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY')
            conn.execute("SET LOCAL statement_timeout='3s'")
            business.check_payer(conn.cursor(),self._bucket)
            return self._pair(conn.cursor(),id)

    def review(self,id,*,decision,command):
        digest=pair_request_hash('review',id,command,decision=decision)
        try:
            replay=self._replay(command,digest,self._decode_pair)
            if replay is not None: return replay
            pair=self.get(id); prepared=self.prepare(thaw(pair.body['selection']),command.actor,strict=False)
            with self._connect() as conn:
                cur=conn.cursor(); saved=self._begin(cur,command,digest)
                if saved:
                    result=self._decode_pair(cur,saved); conn.rollback(); return result
                current=self._pair(cur,id)
                if current.version!=command.expected_version: refuse('version_conflict','Candidate review revision changed')
                if (current.creator.domain,current.creator.user_id)==(command.actor.domain,command.actor.user_id): refuse('self_review','Submitter cannot review own candidate')
                if current.review_state=='revoke' or decision!='revoke' and current.review_state!='proposed': refuse('terminal','Candidate already reviewed')
                context,profile,dependencies=self.recheck(cur,prepared,strict=False)
                quality=evaluate(context,profile=profile)
                if decision=='approve' and (not dependencies or quality.status!='eligible' or quality.purpose=='raw_archive'
                        or current.body['joint_result']['status']!='eligible'):
                    refuse('hard_gate','Candidate has not passed current quantitative machine gates')
                version=current.version+1
                cur.execute('INSERT INTO waveforms.pair_reviews(pair_version_id,version,accounting_bucket,decision,actor,reason) VALUES(%s,%s,%s,%s,%s,%s)',
                            (id,version,self._bucket,decision,Jsonb(asdict(command.actor)),command.reason))
                saved=dict(type='review',id=str(id),version=version)
                business.finish(cur,self._bucket,command,digest,'pairs.review',id,saved)
                result=self._decode_pair(cur,saved); prepare_transaction(cur); conn.commit(); return result
        except psycopg.Error as error:
            raise WaveformError('waveform.pair_unavailable','Review transaction failed; no durable success') from error

    def inspect(self,id,*,actor):
        from backend.registry.transactions import lock_history
        if type(actor) is not Actor: raise ContractError('contract_invalid')
        pair=self.get(id); prepared=self.prepare(thaw(pair.body['selection']),actor,strict=False)
        with self._connect() as conn:
            cur=conn.cursor(); prepare_transaction(cur); business.lock_dependencies(cur); lock_history(cur,shared=True)
            business.lock_payer(cur,self._bucket,self._limits)
            context,profile,current=self.recheck(cur,prepared,strict=False); quality=evaluate(context,profile=profile)
            pair=self._pair(cur,id); conn.rollback()
        status=quality.status; reasons=quality.reasons
        if not current:
            reasons=(*reasons,'selected_evidence_changed')
            if status=='eligible': status='pending'
        if pair.review_state in ('reject','revoke'):
            status='ineligible'; reasons=(*reasons,'pair_rejected' if pair.review_state=='reject' else 'pair_revoked')
        return PairInspection(id,pair.version,pair.body['joint_result']['status'],status,reasons,context.restrictions,current)
