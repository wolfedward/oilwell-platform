"""Fixed selections, exact identities and small immutable business return values."""
from dataclasses import dataclass,fields,is_dataclass
from fractions import Fraction
from hashlib import sha256
import json
from uuid import UUID

from .contracts import (CaptureKey,MAX_I64,require_fields,require_name,require_uuid,
                        require_sha,require_i64_string)
from .errors import ContractError
from .evidence_contracts import canonical_bytes
from .timing import METHOD_VERSION

METHOD_PARAMETERS=dict(interpolation='linear',max_periods=2,extrapolate=False,reference_role='displacement')


def public(value):
    if is_dataclass(value): return {f.name:public(getattr(value,f.name)) for f in fields(value)}
    if hasattr(value,'items'): return {str(k):public(v) for k,v in value.items()}
    if isinstance(value,(tuple,list)): return [public(v) for v in value]
    if isinstance(value,(UUID,Fraction)): return str(value)
    return value


def bounded(value, maximum=65536):
    try:
        # PostgreSQL JSONB adds spaces; reserve its exact common ASCII spacing.
        if len(json.dumps(value,ensure_ascii=False,allow_nan=False).encode('utf-8'))>maximum:
            raise ContractError('contract_invalid')
        from .wire import strict_json
        return strict_json(canonical_bytes(value))
    except (ValueError,TypeError,UnicodeError,RecursionError): raise ContractError('contract_invalid') from None


def key_dict(key):
    return dict(gateway_id=key.gateway_id,device_id=key.device_id,capture_id=str(key.capture_id))


def capture_key(value):
    if hasattr(value,'items'): value=dict(value)
    require_fields(value,('gateway_id','device_id','capture_id'))
    return CaptureKey(value['gateway_id'],value['device_id'],require_uuid(value['capture_id']))


def evidence_ref(value):
    if value is None: return None
    require_fields(value,('id','content_sha256','version'))
    id=require_uuid(value['id']); require_sha(value['content_sha256'])
    version=require_i64_string(value['version'],nonnegative=True)
    if version<1: raise ContractError('contract_invalid')
    return dict(id=str(id),content_sha256=value['content_sha256'],version=str(version))


def validate_selection(body):
    body=bounded(body)
    require_fields(body,('purpose','profile','captures','channels','cycle','sync'))
    if body['purpose'] not in ('raw_archive','software_pair_test','field_pair_input'): raise ContractError('contract_invalid')
    body['profile']=evidence_ref(body['profile']); body['sync']=evidence_ref(body['sync'])
    if type(body['captures']) is not list or not 1<=len(body['captures'])<=2: raise ContractError('contract_invalid')
    keys=[]
    for entry in body['captures']:
        require_fields(entry,('key','manifest_sha256','origins'))
        key=capture_key(entry['key']); entry['key']=key_dict(key); keys.append(key)
        if entry['manifest_sha256'] is not None: require_sha(entry['manifest_sha256'])
        if type(entry['origins']) is not list or len(entry['origins'])>8: raise ContractError('contract_invalid')
        entry['origins']=[evidence_ref(r) for r in entry['origins']]
        if any(r is None for r in entry['origins']) or len({r['id'] for r in entry['origins']})!=len(entry['origins']):
            raise ContractError('contract_invalid')
    if len(set(keys))!=len(keys): raise ContractError('contract_invalid')
    if type(body['channels']) is not list or len(body['channels'])>32: raise ContractError('contract_invalid')
    identities=[]; roles=[]; streams=set()
    for entry in body['channels']:
        require_fields(entry,('role','capture','stream_id','channel_id','first_index','last_index','sampling_config','calibration'))
        for name in ('role','stream_id','channel_id'): require_name(entry[name])
        key=capture_key(entry['capture']); entry['capture']=key_dict(key)
        if key not in keys: raise ContractError('contract_invalid')
        first=require_i64_string(entry['first_index'],nonnegative=True); last=require_i64_string(entry['last_index'],nonnegative=True)
        if first>last: raise ContractError('contract_invalid')
        for name in ('sampling_config','calibration'): entry[name]=evidence_ref(entry[name])
        roles.append(entry['role']); identities.append((key,entry['stream_id'],entry['channel_id'])); streams.add((key,entry['stream_id']))
    if len(set(roles))!=len(roles) or len(set(identities))!=len(identities) or len(streams)>8: raise ContractError('contract_invalid')
    if body['cycle'] is not None:
        require_fields(body['cycle'],('capture','cycle_id'))
        key=capture_key(body['cycle']['capture'])
        if key not in keys: raise ContractError('contract_invalid')
        body['cycle']=dict(capture=key_dict(key),cycle_id=str(require_uuid(body['cycle']['cycle_id'])))
    return body


def validate_proposal(body):
    body=bounded(body); require_fields(body,('selection','method_version'))
    if body['method_version']!=METHOD_VERSION: raise ContractError('contract_invalid')
    return dict(selection=validate_selection(body['selection']),method_version=METHOD_VERSION)


def selection_hash(selection):
    return sha256(canonical_bytes(dict(protocol='waveform.quality-selection/1',selection=validate_selection(selection)))).hexdigest()


def pair_content(body,method_version):
    return sha256(canonical_bytes(dict(protocol='waveform.pair-content/1',body=bounded(body),method_version=method_version))).hexdigest()


@dataclass(frozen=True)
class QualityRun:
    id: UUID
    key: CaptureKey
    manifest_sha256: str | None
    profile_id: UUID | None
    profile_sha256: str | None
    purpose: str
    status: str
    reasons: tuple
    validator_version: str
    selection_sha256: str


@dataclass(frozen=True)
class QualityRunSet:
    runs: tuple
    selection_sha256: str


@dataclass(frozen=True)
class PairVersion:
    id: UUID
    version: int
    content_sha256: str
    left_quality_run_id: UUID
    right_quality_run_id: UUID
    method_version: str
    body: object
    creator: object
    review_state: str


@dataclass(frozen=True)
class PairReview:
    pair_version_id: UUID
    version: int
    decision: str
    actor: object
    reason: str


@dataclass(frozen=True)
class PairInspection:
    pair_version_id: UUID
    version: int
    historical_status: str
    current_status: str
    current_reasons: tuple
    current_restrictions: tuple
    dependencies_current: bool
