"""Capability defaults and fresh, session-bound SQLite authorization."""
from datetime import datetime, timezone
from backend.auth import PermissionDenied
from .contracts import AuthoritySnapshot

PERMISSIONS = tuple('inversion.' + key for key in ('read','parameters.manage','parameters.review','run','review','models.manage','models.review','export','audit.read'))
ROLE_PERMISSIONS = {
    'administrator': PERMISSIONS,
    'production_engineer': ('inversion.read','inversion.run','inversion.review'),
    'algorithm_engineer': ('inversion.read','inversion.parameters.manage','inversion.run','inversion.models.manage','inversion.export'),
    'maintenance_engineer': ('inversion.read','inversion.parameters.manage'),
    'reviewer': ('inversion.read','inversion.parameters.review','inversion.review','inversion.models.review','inversion.audit.read'),
}
RUN = frozenset(('inversion.run','waveform.read','waveform.raw.read','waveform.export'))
EXPORT = frozenset(('inversion.read','inversion.export','waveform.read','waveform.raw.read','waveform.export'))


def required_permissions(action):
    if action in ('runs.create','runs.preflight','runs.retry'): return RUN
    if action == 'runs.cancel': return frozenset({'inversion.run'})
    if action in ('results.review','results.revoke'): return frozenset({'inversion.read','inversion.review'})
    if action.startswith('exports.'): return EXPORT
    capability = {
        'assemblies.create':'parameters.manage','assignments.create':'parameters.manage',
        'parameters.create':'parameters.manage','parameters.submit':'parameters.manage',
        'parameters.review':'parameters.review','parameters.revoke':'parameters.review',
        'models.create':'models.manage','models.review':'models.review','models.revoke':'models.review',
        'selections.list':'read','selections.create':'models.manage','selections.rollback':'models.manage',
        'results.review':'review','results.revoke':'review','audit':'audit.read',
    }.get(action, 'read')
    return frozenset({'inversion.' + capability})


class SessionAuthority:
    def __init__(self, auth, ref=None): self.auth, self.ref = auth, ref

    def refresh(self, ref, permissions=RUN):
        user = self.auth.refresh_background_authority(ref, permissions)
        return AuthoritySnapshot(ref, user.permissions, datetime.now(timezone.utc), user.version)

    def authorize(self, actor, capability):
        if self.ref is None or (actor.domain, actor.user_id) != (self.ref.domain, str(self.ref.user_id)):
            raise PermissionDenied('session identity mismatch')
        return self.refresh(self.ref, frozenset({capability}))
