"""Authenticated actor values and canonical, independently bound import commands.

Authorization is the API's responsibility, including receipt replay. Identity
and idempotency use domain/user_id; display names and roles are audit snapshots.
"""
from dataclasses import asdict, dataclass
from hashlib import sha256
import json
from uuid import UUID

from .contracts import (MAX_I64, require_count, require_fields, require_name,
                        require_sha, require_text, require_uuid)
from .errors import ContractError
from .files import MAX_FILE_BYTES


@dataclass(frozen=True)
class Actor:
    domain: str
    user_id: str
    username: str
    role_code: str

    def __post_init__(self):
        for value in asdict(self).values():
            require_text(value)
            if not value.strip(" "):
                raise ContractError("contract_invalid")


@dataclass(frozen=True)
class Command:
    actor: Actor
    key: UUID
    expected_version: int
    reason: str
    request_hash: str

    def __post_init__(self):
        if type(self.actor) is not Actor or type(self.key) is not UUID:
            raise ContractError("contract_invalid")
        require_count(self.expected_version, maximum=MAX_I64)
        require_text(self.reason, maximum=512)
        if not self.reason.strip(" "):
            raise ContractError("contract_invalid")
        require_sha(self.request_hash)


def import_metadata(metadata):
    """Validate and return a canonical independent copy of exact file metadata."""
    require_fields(metadata, ("file_sha256", "bytes", "line_count", "final_lf",
                              "gateway_id", "device_id", "capture_id"))
    require_sha(metadata["file_sha256"])
    require_count(metadata["bytes"], minimum=1, maximum=MAX_FILE_BYTES)
    require_count(metadata["line_count"], minimum=1, maximum=513)
    if type(metadata["final_lf"]) is not bool:
        raise ContractError("contract_invalid")
    require_name(metadata["gateway_id"])
    require_name(metadata["device_id"])
    return metadata | {"capture_id": str(require_uuid(metadata["capture_id"]))}


def import_request_hash(operation, target, command, *, metadata=None, line=None, payload=None):
    """SHA256 of sorted compact UTF-8 JSON (ensure_ascii=False), protocol v1.

    target is None for create, UUID otherwise. Lines are public zero-based.
    Version is a decimal string. Raw payloads bind length plus exact SHA256.
    The caller's request_hash and key are deliberately outside the digest.
    """
    if type(command) is not Command or operation not in {"create", "append", "seal", "cancel"}:
        raise ContractError("contract_invalid")
    if operation == "create":
        if target is not None or command.expected_version != 0 or line is not None or payload is not None:
            raise ContractError("contract_invalid")
        body = import_metadata(metadata)
    else:
        if type(target) is not UUID or command.expected_version < 1 or metadata is not None:
            raise ContractError("contract_invalid")
        body = {}
        if operation == "append":
            require_count(line, maximum=512)
            if not isinstance(payload, bytes) or not 1 <= len(payload) <= 262144:
                raise ContractError("contract_invalid")
            body = {"line": line, "payload_bytes": len(payload), "payload_sha256": sha256(payload).hexdigest()}
        elif line is not None or payload is not None:
            raise ContractError("contract_invalid")
    envelope = {"protocol": "waveform.import-command/1", "operation": "imports."+operation,
                "target": str(target) if target is not None else None,
                "actor": {"domain": command.actor.domain, "user_id": command.actor.user_id},
                "expected_version": str(command.expected_version), "reason": command.reason, "body": body}
    return sha256(json.dumps(envelope, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


def lock_import_guard(cursor, kind, *identity):
    """Transaction guard; fixed namespaces only, acquired before business rows."""
    if kind not in {"command", "creator"}:
        raise ValueError("Unknown import guard")
    encoded = json.dumps(["waveform.import-lock/1", kind, *identity], ensure_ascii=False,
                         separators=(",", ":")).encode("utf-8")
    number = int.from_bytes(sha256(encoded).digest()[:8], "big", signed=True)
    cursor.execute("SELECT pg_advisory_xact_lock(%s)", (number,))


def evidence_request_hash(operation, target, command, *, kind=None, body=None,
                          decision=None, attachment=None):
    """Fixed business commands, independently bound to validated actual content."""
    from .evidence_contracts import validate_body, canonical_bytes
    if type(command) is not Command or operation not in {'create','review','revoke','upload'}:
        raise ContractError('contract_invalid')
    if operation in {'create','upload'}:
        if target is not None or command.expected_version!=0: raise ContractError('contract_invalid')
    elif type(target) is not UUID or command.expected_version<1: raise ContractError('contract_invalid')
    if operation=='create':
        if decision is not None or attachment is not None: raise ContractError('contract_invalid')
        content={'kind':kind,'body':validate_body(kind,body)}
    elif operation=='upload':
        if any(v is not None for v in (kind,body,decision)): raise ContractError('contract_invalid')
        require_fields(attachment, ('media_type','payload_bytes','payload_sha256'))
        if attachment['media_type'] not in ('application/pdf','image/png','image/jpeg'): raise ContractError('contract_invalid')
        require_count(attachment['payload_bytes'], minimum=1, maximum=4194304)
        require_sha(attachment['payload_sha256']); content=dict(attachment)
    else:
        if any(v is not None for v in (kind,body,attachment)): raise ContractError('contract_invalid')
        if operation=='review':
            if decision not in ('approve','reject'): raise ContractError('contract_invalid')
            content={'decision':decision}
        else:
            if decision is not None: raise ContractError('contract_invalid')
            content={}
    envelope={'protocol':'waveform.business-command/1',
        'operation':'attachments.upload' if operation=='upload' else 'evidence.'+operation,
        'target':str(target) if target is not None else None,
        'actor':{'domain':command.actor.domain,'user_id':command.actor.user_id},
        'expected_version':str(command.expected_version),'reason':command.reason,'body':content}
    return sha256(canonical_bytes(envelope)).hexdigest()


def _quality_pair_hash(operation,target,command,content):
    from .evidence_contracts import canonical_bytes
    envelope=dict(protocol='waveform.business-command/1',operation=operation,target=target,
        actor=dict(domain=command.actor.domain,user_id=command.actor.user_id),
        expected_version=str(command.expected_version),reason=command.reason,body=content)
    return sha256(canonical_bytes(envelope)).hexdigest()


def quality_request_hash(key,command,*,body):
    from .contracts import CaptureKey
    from .pair_contracts import validate_selection,key_dict,capture_key
    if type(command) is not Command or command.expected_version!=0 or type(key) is not CaptureKey:
        raise ContractError('contract_invalid')
    content=validate_selection(body)
    if key not in [capture_key(c['key']) for c in content['captures']]: raise ContractError('contract_invalid')
    return _quality_pair_hash('quality.record',key_dict(key),command,content)


def pair_request_hash(operation,target,command,*,body=None,decision=None):
    from .pair_contracts import validate_proposal
    if type(command) is not Command: raise ContractError('contract_invalid')
    if operation=='propose':
        if target is not None or command.expected_version!=0 or decision is not None: raise ContractError('contract_invalid')
        content=validate_proposal(body)
    elif operation=='review':
        if type(target) is not UUID or command.expected_version<1 or body is not None or decision not in ('approve','reject','revoke'):
            raise ContractError('contract_invalid')
        content=dict(decision=decision)
    else: raise ContractError('contract_invalid')
    return _quality_pair_hash('pairs.'+operation,str(target) if target is not None else None,command,content)


def dataset_request_hash(operation,target,command,*,body):
    from .dataset_contracts import validate_body
    from .evidence_contracts import canonical_bytes
    if type(command) is not Command: raise ContractError('contract_invalid')
    content=validate_body(operation,body)
    if operation=='create':
        if target is not None or command.expected_version!=0: raise ContractError('contract_invalid')
    elif type(target) is not UUID or command.expected_version<1 or content['target_id']!=str(target): raise ContractError('contract_invalid')
    envelope=dict(protocol='waveform.business-command/1',operation='datasets.'+operation,target=str(target) if target else None,
        actor=dict(domain=command.actor.domain,user_id=command.actor.user_id),expected_version=str(command.expected_version),reason=command.reason,body=content)
    raw=canonical_bytes(envelope)
    if len(raw)>65536: raise ContractError('contract_invalid')
    return sha256(raw).hexdigest()


def export_request_hash(target,command,*,body):
    from .evidence_contracts import canonical_bytes
    require_fields(body,('version_id','purpose'))
    if (type(command) is not Command or command.expected_version!=0 or type(target) is not UUID
        or body['version_id']!=str(target) or body['purpose'] not in ('research','training','evidence')): raise ContractError('contract_invalid')
    envelope=dict(protocol='waveform.business-command/1',operation='exports.create',target=str(target),
        actor=dict(domain=command.actor.domain,user_id=command.actor.user_id),expected_version='0',reason=command.reason,body=body)
    return sha256(canonical_bytes(envelope)).hexdigest()
