"""Incident-specific authorization: unrelated registry grants never suffice."""
from .errors import ReliabilityError


INGEST_ROLE_PERMISSIONS = {
    'administrator': ('ingest.read', 'ingest.raw.read', 'ingest.manage'),
    'production_engineer': ('ingest.read',),
    'algorithm_engineer': ('ingest.read', 'ingest.raw.read'),
    'maintenance_engineer': ('ingest.read', 'ingest.raw.read', 'ingest.manage'),
    'reviewer': ('ingest.read',),
}


def required_permissions(action):
    result = {'ingest.read'}
    if action in {'raw.preview', 'raw.download'}:
        result.add('ingest.raw.read')
    elif action in {'case.ack', 'case.close', 'case.reopen', 'case.note', 'receipt.resend'}:
        result.add('ingest.manage')
    elif action == 'audit':
        result.add('audit.read')
    elif action not in {'cases', 'case', 'receipts', 'receipt', 'health'}:
        raise ReliabilityError('ingest.not_found', '未知的接收可靠性操作', 404)
    return frozenset(result)
