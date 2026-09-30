"""Explicit capabilities; roles are defaults, never runtime authority."""
from .errors import WaveformError

WAVEFORM_PERMISSIONS=tuple('waveform.'+s for s in ('read','raw.read','import','evidence.manage','pair.manage','dataset.manage','export','review','audit.read'))
WAVEFORM_ROLE_PERMISSIONS={
    'administrator':WAVEFORM_PERMISSIONS,
    'production_engineer':('waveform.read','waveform.pair.manage','waveform.review'),
    'algorithm_engineer':tuple(p for p in WAVEFORM_PERMISSIONS if p not in ('waveform.review','waveform.audit.read')),
    'maintenance_engineer':('waveform.read','waveform.raw.read','waveform.import','waveform.evidence.manage'),
    'reviewer':('waveform.read','waveform.review'),
}
READ={'health','captures','capture','window','evidence','evidence.get','evaluations','pairs','pair','pair.preview','datasets','dataset'}
EXTRA={
    'fact.raw':('raw.read',),'attachment.raw':('raw.read',),
    'import.get':('import',),'import.create':('import',),'import.append':('import',),'import.seal':('import',),'import.cancel':('import',),
    'attachment.upload':('evidence.manage',),'evidence.create':('evidence.manage',),
    'evidence.review':('review',),'evidence.revoke':('review',),'evaluation.create':('pair.manage',),
    'pair.create':('pair.manage',),'pair.review':('review',),
    'dataset.create':('dataset.manage',),'dataset.update':('dataset.manage',),'dataset.submit':('dataset.manage',),'dataset.freeze':('review',),
    'export.create':('raw.read','export'),'export.manifest':('raw.read','export'),'export.capture':('raw.read','export'),
    'audit':('audit.read',),
}

def required_permissions(action: str)->frozenset[str]:
    if action not in READ and action not in EXTRA:
        raise WaveformError('waveform.not_found','Unsupported waveform action',404)
    return frozenset({'waveform.read',*('waveform.'+p for p in EXTRA.get(action,()))})
