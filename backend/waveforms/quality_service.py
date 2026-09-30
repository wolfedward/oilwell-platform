"""Durable server-computed joint evaluations over explicit immutable selections."""
from dataclasses import asdict
from uuid import UUID,uuid4
import psycopg
from psycopg.types.json import Jsonb

from backend.registry.transactions import lock_history
from . import business
from .capacity import prepare_transaction,row
from .commands import quality_request_hash
from .errors import WaveformError,ContractError
from .pair_contracts import (QualityRun,QualityRunSet,validate_selection,capture_key,key_dict,
                             selection_hash,bounded,public)
from .quality import evaluate,evidence_items,cycle_summary
from .quality_context import ContextBuilder


class QualityService(ContextBuilder):
    def _outcome(self,context,result):
        return bounded(public(result)|{'cycle_summary':cycle_summary(context)})

    def _load_run(self,cur,id):
        cur.execute('SELECT * FROM waveforms.quality_runs WHERE quality_run_id=%s',(id,)); record=row(cur)
        if record is None: raise WaveformError('waveform.quality_not_found','Quality run does not exist',404)
        return QualityRun(record['quality_run_id'],capture_key(record['source_key']),record['manifest_sha256'],record['profile_id'],
            record['profile_sha256'],record['purpose'],record['status'],tuple(record['result']['reasons']),
            record['validator_version'],record['parameters']['selection_sha256'])

    def _decode_quality(self,cur,saved):
        return QualityRunSet(tuple(self._load_run(cur,UUID(id)) for id in saved['ids']),saved['selection_sha256'])

    def _replay(self,command,digest,decode):
        if digest!=command.request_hash: raise WaveformError('waveform.request_hash','Command does not bind actual content',409)
        with self._connect() as conn:
            cur=conn.cursor(); prepare_transaction(cur); business.lock_dependencies(cur); business.check_payer(cur,self._bucket)
            saved=business.receipt(cur,command,digest)
            result=decode(cur,saved) if saved else None; conn.rollback(); return result

    def _begin(self,cur,command,digest):
        prepare_transaction(cur); business.lock_dependencies(cur); business.check_payer(cur,self._bucket)
        saved=business.receipt(cur,command,digest)
        if saved: return saved
        lock_history(cur,shared=True)
        business.lock_payer(cur,self._bucket,self._limits)
        return None

    def _write_runs(self,cur,prepared,context,profile,result,actor):
        selection=prepared['selection']; digest=selection_hash(selection)
        parameters=bounded(dict(selection=selection,selection_sha256=digest,physical_cycles=public(context.cycle['physical_cycles'])))
        evidence=bounded([dict(id=str(e.id),kind=e.kind,content_sha256=e.content_sha256,version=str(e.version),review_state=e.review_state)
                          for e in evidence_items(context,profile)])
        snapshot=bounded(public(context.registry_snapshot)) if context.registry_snapshot else None
        outcome=self._outcome(context,result); runs=[]
        for chosen in selection['captures']:
            id=uuid4(); key=capture_key(chosen['key']); capture=prepared['captures'][key]
            cur.execute('INSERT INTO waveforms.quality_runs(quality_run_id,accounting_bucket,capture_pk,source_key,manifest_sha256,validator_version,profile_id,profile_sha256,purpose,status,parameters,evidence,registry_snapshot,result,creator) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)',
                (id,self._bucket,capture['capture_pk'] if capture else None,Jsonb(key_dict(key)),capture['manifest_sha256'] if capture else None,
                 result.validator_version,profile.id if profile else None,profile.evidence.content_sha256 if profile else None,
                 result.purpose,result.status,Jsonb(parameters),Jsonb(evidence),Jsonb(snapshot) if snapshot else None,Jsonb(outcome),Jsonb(asdict(actor))))
            runs.append(self._load_run(cur,id))
        return QualityRunSet(tuple(runs),digest)

    def record(self,key,body,command):
        selection=validate_selection(body); digest=quality_request_hash(key,command,body=selection)
        try:
            replay=self._replay(command,digest,self._decode_quality)
            if replay is not None: return replay
            prepared=self.prepare(selection,command.actor)
            with self._connect() as conn:
                cur=conn.cursor(); saved=self._begin(cur,command,digest)
                if saved:
                    result=self._decode_quality(cur,saved); conn.rollback(); return result
                context,profile,_=self.recheck(cur,prepared)
                result=self._write_runs(cur,prepared,context,profile,evaluate(context,profile=profile),command.actor)
                saved=dict(type='quality',ids=[str(r.id) for r in result.runs],selection_sha256=result.selection_sha256)
                business.finish(cur,self._bucket,command,digest,'quality.record',result.runs[0].id,saved)
                prepare_transaction(cur); conn.commit(); return result
        except psycopg.Error as error:
            raise WaveformError('waveform.quality_unavailable','Evaluation transaction failed; no durable success') from error

    def get(self,id):
        if type(id) is not UUID: raise ContractError('contract_invalid')
        with self._reader() as conn:
            conn.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY')
            conn.execute("SET LOCAL statement_timeout='3s'")
            business.check_payer(conn.cursor(),self._bucket)
            return self._load_run(conn.cursor(),id)
