"""Exact paths and bounded query grammar; no dynamic service dispatch."""
import re
from urllib.parse import parse_qsl
from .contracts import fields, identity, integer_text, text
from .errors import InversionContractError

PREFIX = '/api/production-inversion'
COLLECTIONS = {'health':'health','assemblies':'assemblies.list','parameters':'parameters.list','models':'models.list','runs':'runs.list','audit':'audit'}
CREATES = {'assemblies':'assemblies.create','assignments':'assignments.create','parameter-versions':'parameters.create','models':'models.create','selections':'selections.create','runs':'runs.create','exports':'exports.create'}
ACTIONS = {'parameters':('submit','review','revoke'),'models':('review','revoke'),'selections':('rollback',),'runs':('cancel','retry'),'results':('review','revoke')}
# Exact HTTP mutation -> persisted transaction action(s), never prefix matching.
RECOVERY_ACTIONS = {
    'assemblies.create':('assembly.create',), 'assignments.create':('assembly.assign',),
    'parameters.create':('parameter.create',), 'parameters.submit':('parameter.submit',),
    'parameters.review':('parameter.approve','parameter.reject'), 'parameters.revoke':('parameter.revoke',),
    'models.create':('model.register',), 'models.review':('model.approve','model.reject'),
    'models.revoke':('model.revoke',), 'selections.create':('selection.select',),
    'selections.rollback':('selection.rollback',), 'runs.create':('runs.create',),
    'runs.retry':('runs.retry',), 'runs.cancel':('runs.cancel',),
    'results.review':('result.approve','result.reject'), 'results.revoke':('result.revoke',),
    'exports.create':('export.create',),
}


class RouteError(Exception):
    def __init__(self, status): self.status = status


def route(method, path):
    if '%' in path or '\\' in path or not path.startswith(PREFIX + '/'): raise InversionContractError()
    parts = path[len(PREFIX)+1:].split('/')
    if any(not p for p in parts): raise RouteError(404)
    possibilities = {}; identifier = None
    if len(parts) == 1:
        if parts[0] in COLLECTIONS: possibilities['GET'] = COLLECTIONS[parts[0]]
        if parts[0] in CREATES: possibilities['POST'] = CREATES[parts[0]]
    elif parts == ['runs','preflight']: possibilities['POST'] = 'runs.preflight'
    elif 2 <= len(parts) <= 3:
        identifier = identity(parts[1])
        if len(parts) == 2 and parts[0] in ('assemblies','parameters','runs','results','exports','requests'):
            possibilities['GET'] = parts[0] + '.get'
        elif len(parts) == 3:
            group, _, action = parts
            if action in ACTIONS.get(group, ()): possibilities['POST'] = group + '.' + action
            if group == 'assemblies' and action == 'selections': possibilities['GET'] = 'selections.list'
            if group == 'assemblies' and action == 'versions': possibilities['GET'] = 'assembly_versions.list'
            if group == 'results' and action in ('points','comparison'): possibilities['GET'] = 'results.' + action
    if not possibilities: raise RouteError(404)
    if method not in possibilities: raise RouteError(405)
    return possibilities[method], identifier


def query(raw, action):
    allowed = {'offset','limit'} if action.endswith('.list') or action in ('audit','results.points') else set()
    if action=='results.points':allowed|={'kind'}
    if action == 'parameters.list': allowed |= {'assembly_id'}
    if action == 'audit': allowed |= {'action','subject_id'}
    if action == 'requests.get': allowed = {'action','intent'}
    if action == 'results.get': allowed = {'include_input_metadata'}
    if len(raw) > 4096 or re.search(r'%(?![0-9a-fA-F]{2})', raw): raise InversionContractError()
    try:
        pairs = parse_qsl(raw, keep_blank_values=True, strict_parsing=True, encoding='utf-8', errors='strict', max_num_fields=4)
    except (ValueError, UnicodeError): raise InversionContractError() from None
    out = dict(pairs)
    if len(out) != len(pairs) or set(out)-allowed: raise InversionContractError()
    if 'kind' in out and out['kind'] not in ('prediction','comparison','display_only_comparison','display_only_fills','proof'):raise InversionContractError()
    if 'include_input_metadata' in out:
        if out['include_input_metadata'] != 'true': raise InversionContractError()
        out['include_input_metadata'] = True
    if action == 'requests.get' and out.get('action') not in RECOVERY_ACTIONS: raise InversionContractError()
    if action == 'requests.get':
        if out['action']=='exports.create':
            if out.get('intent') not in ('quantitative','evidence'): raise InversionContractError()
        elif 'intent' in out: raise InversionContractError()
    if 'offset' in allowed:
        out['offset'] = integer_text(out.get('offset','0'), maximum=163840 if action=='results.points' else 100000)
        out['limit'] = integer_text(out.get('limit','50'), minimum=1, maximum=1000 if action=='results.points' else 100)
    if 'subject_id' in out: out['subject_id'] = identity(out['subject_id'])
    if 'assembly_id' in out:
        parsed = identity(out['assembly_id'])
        if str(parsed) != out['assembly_id']: raise InversionContractError()
        out['assembly_id'] = parsed
    if 'action' in out: text(out['action'], maximum=128)
    return out
