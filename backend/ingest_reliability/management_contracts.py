"""Pure management-command types and validation; no database side effects."""
from dataclasses import dataclass, fields
import hashlib
import json
import re
from uuid import UUID

from .errors import ReliabilityError
from .facts import MAX_COUNTER


ACTIONS = frozenset({'case.ack', 'case.close', 'case.reopen', 'case.note', 'receipt.resend'})
_UUID = re.compile(r'[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}\Z')


def invalid():
    return ReliabilityError('ingest.management_invalid', '处置请求的身份、版本、原因或幂等键无效', 422)


def _text(value, *, minimum=1, maximum=128):
    if (not isinstance(value, str) or not minimum <= len(value) <= maximum
            or len(value.strip()) < minimum or any(ord(char) < 32 or 127 <= ord(char) <= 159 for char in value)):
        raise invalid()
    try:
        value.encode('utf-8')
    except UnicodeEncodeError:
        raise invalid() from None
    return value


@dataclass(frozen=True)
class Actor:
    domain: str
    user_id: str
    username: str
    display_name: str
    role_code: str

    def __post_init__(self):
        for field in fields(self):
            _text(getattr(self, field.name))


@dataclass(frozen=True)
class ManagementCommand:
    action: str
    target: UUID
    actor: Actor
    key: UUID
    expected_version: int
    reason: str
    request_hash: str

    def __post_init__(self):
        if (not isinstance(self.action, str) or self.action not in ACTIONS
                or not isinstance(self.target, UUID) or not isinstance(self.key, UUID)
                or not isinstance(self.actor, Actor) or type(self.expected_version) is not int
                or not 1 <= self.expected_version <= MAX_COUNTER
                or not isinstance(self.request_hash, str) or not re.fullmatch(r'[0-9a-f]{64}', self.request_hash)):
            raise invalid()
        self.actor.__post_init__()
        _text(self.reason, minimum=2, maximum=500)


def validate_management(action, target, actor, body, *, route):
    if (not isinstance(action, str) or action not in ACTIONS or not isinstance(target, UUID)
            or not isinstance(actor, Actor) or not isinstance(body, dict)
            or set(body) != {'reason', 'idempotency_key', 'expected_version'}):
        raise invalid()
    actor.__post_init__()
    resource = 'receipts' if action == 'receipt.resend' else 'cases'
    expected_route = f'/api/ingest-reliability/{resource}/{target}/{action.split(".")[-1]}'
    if route != expected_route:
        raise invalid()
    reason = _text(body['reason'], minimum=2, maximum=500)
    key, version = body['idempotency_key'], body['expected_version']
    if (not isinstance(key, str) or not _UUID.fullmatch(key) or not isinstance(version, str)
            or not re.fullmatch(r'[1-9][0-9]{0,18}', version) or int(version) > MAX_COUNTER):
        raise invalid()
    canonical = json.dumps({'action': action, 'route': route, 'body': body},
                           sort_keys=True, ensure_ascii=False, separators=(',', ':')).encode('utf-8')
    return ManagementCommand(action, target, actor, UUID(key), int(version), reason, hashlib.sha256(canonical).hexdigest())
