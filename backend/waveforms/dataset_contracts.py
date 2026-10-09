"""Fixed bounded dataset commands, small DTOs and staged canonical manifest bytes."""
from dataclasses import dataclass
from fractions import Fraction
from hashlib import sha256
import json
import re
from tempfile import TemporaryFile
from uuid import UUID

from .commands import Actor
from .contracts import require_fields,require_count,require_text,require_uuid
from .errors import ContractError,WaveformError
from .evidence_contracts import canonical_bytes,freeze
from .pair_contracts import bounded

PURPOSES=('raw_archive','software_pair_test','field_pair_input')
SPLITS=('train','validation','test','unassigned')
SNAPSHOT_VERSIONS=dict(pair='waveform.pair-content/1',quality='waveform.quality/1',split='waveform.dataset-split/1')
MAX_MANIFEST=268500992
_FRACTION=re.compile(r'(?:0|-?[1-9][0-9]*)(?:/[1-9][0-9]*)?\Z',re.ASCII)


def fail(code,message): raise WaveformError('waveform.dataset_'+code,message,409)
def identity(actor): return dict(domain=actor.domain,user_id=actor.user_id)


def exact_fraction(value):
    if type(value) is not str or len(value)>128 or not _FRACTION.fullmatch(value): raise ContractError('contract_invalid')
    number=Fraction(value)
    if str(number)!=value: raise ContractError('contract_invalid')
    return number


def validate_split(plan):
    if type(plan) is not dict: raise ContractError('contract_invalid')
    kind=plan.get('kind')
    if kind in ('none','well_holdout'): require_fields(plan,('kind',)); return dict(plan)
    if kind!='time_holdout': raise ContractError('contract_invalid')
    require_fields(plan,('kind','intervals','gap_ns'))
    require_fields(plan['intervals'],('train','validation','test'))
    gap=exact_fraction(plan['gap_ns'])
    if gap<0: raise ContractError('contract_invalid')
    previous=None
    for split in ('train','validation','test'):
        item=plan['intervals'][split]
        if split=='validation' and item is None: continue
        require_fields(item,('start_ns','end_ns'))
        low,high=map(exact_fraction,(item['start_ns'],item['end_ns']))
        if low>high or previous is not None and (low<=previous or low-previous<gap): raise ContractError('contract_invalid')
        previous=high
    return json.loads(canonical_bytes(plan))


def member(body):
    require_fields(body,('kind','reference_id','quality_run_id','split'))
    if body['kind'] not in ('capture','pair') or body['split'] not in SPLITS: raise ContractError('contract_invalid')
    for name in ('reference_id','quality_run_id'):
        if str(require_uuid(body[name]))!=body[name]: raise ContractError('contract_invalid')
    return dict(body)


def metadata(body):
    require_fields(body,('title','purpose','split_plan'))
    require_text(body['title'],maximum=256)
    if not body['title'].strip() or body['purpose'] not in PURPOSES: raise ContractError('contract_invalid')
    plan=validate_split(body['split_plan'])
    if body['purpose']=='raw_archive' and plan['kind']!='none': raise ContractError('contract_invalid')
    return dict(title=body['title'],purpose=body['purpose'],split_plan=plan)


def validate_body(operation,body):
    body=bounded(body)
    if operation=='create': return metadata(body)
    if operation not in ('update','submit','freeze'): raise ContractError('contract_invalid')
    require_fields(body,('target_id','changes') if operation=='update' else ('target_id',))
    if str(require_uuid(body['target_id']))!=body['target_id']: raise ContractError('contract_invalid')
    if operation!='update': return body
    change=body['changes']
    if type(change) is not dict or len(change)!=1: raise ContractError('contract_invalid')
    key=next(iter(change)); value=change[key]
    if key=='metadata': change[key]=metadata(value)
    elif key in ('upsert_members','remove_members'):
        if type(value) is not list or not 1<=len(value)<=100: raise ContractError('contract_invalid')
        keys=[]
        for item in value:
            if key=='upsert_members': member(item)
            else:
                require_fields(item,('kind','reference_id'))
                if item['kind'] not in ('capture','pair') or str(require_uuid(item['reference_id']))!=item['reference_id']: raise ContractError('contract_invalid')
            keys.append((item['kind'],item['reference_id']))
        if len(keys)!=len(set(keys)): raise ContractError('contract_invalid')
    else: raise ContractError('contract_invalid')
    return body


def snapshot_hash(kind,value):
    return sha256(canonical_bytes(dict(protocol='waveform.dataset-snapshot/1',kind=kind,value=value))).hexdigest()


class StagedManifest:
    """Unique ephemeral staging handle, closed on success, failure, replay or caller exit."""
    def __init__(self,value):
        self._file=TemporaryFile(mode='w+b'); self.length=0; digest=sha256()
        try:
            for text in json.JSONEncoder(ensure_ascii=False,sort_keys=True,separators=(',',':'),allow_nan=False).iterencode(value):
                data=text.encode('utf-8'); self.length+=len(data)
                if self.length>MAX_MANIFEST: fail('manifest_limit','Manifest exceeds bounded format')
                self._file.write(data); digest.update(data)
            self.content_sha256=digest.hexdigest(); self.chunk_count=(self.length+65535)//65536
        except BaseException:
            self.close(); raise

    @property
    def closed(self): return self._file.closed
    def chunks(self):
        self._file.seek(0)
        while data:=self._file.read(65536): yield data
    def close(self): self._file.close()
    def __enter__(self): return self
    def __exit__(self,*args): self.close()


def stage_manifest(value): return StagedManifest(value)


@dataclass(frozen=True)
class DatasetDraft:
    id: UUID
    revision: int
    state: str
    title: str
    purpose: str
    split_plan: object
    member_count: int
    creator: Actor
    submitter: Actor|None


@dataclass(frozen=True)
class DatasetVersion:
    id: UUID
    dataset_id: UUID
    draft_revision: int
    content_sha256: str
    manifest_bytes: int
    purpose: str
    restriction_state: str
    restriction_reasons: tuple
    member_count: int
    snapshot_versions: object
    independent_split: bool


def reason_codes(values):
    result=tuple(sorted(set(values)))
    if len(result)>64: fail('restriction_limit','Restriction result exceeds bounded metadata')
    return result
