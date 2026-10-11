"""Append selection and rollback events; never reinterpret physical history."""
from dataclasses import asdict
from hashlib import sha256
from uuid import UUID, uuid4
from psycopg.types.json import Jsonb
from .contracts import InversionCommand, JsonObject, canonical_bytes, strict_json, fields, identity, digest, integer, fail
from .parameters import GovernanceService, expected, refuse, ParameterService
from .models import ModelService
from .transactions import Change, Receipt

SELECTION_FIELDS=('assembly_id','parameter_id','parameter_sha256','parameter_review_version',
                  'model_id','model_sha256','model_review_version')


def _selection_body(body):
    body=strict_json(canonical_bytes(body)); fields(body,SELECTION_FIELDS)
    for key in ('assembly_id','parameter_id','model_id'): identity(body[key])
    for key in ('parameter_sha256','model_sha256'): digest(body[key])
    for key in ('parameter_review_version','model_review_version'): integer(body[key],minimum=1)
    return body


class SelectionService(GovernanceService):
    def __init__(self, connect, *, read_connect, authorize, parameters, models):
        super().__init__(connect,read_connect=read_connect,authorize=authorize)
        if type(parameters) is not ParameterService or type(models) is not ModelService: fail()
        self.parameters,self.models=parameters,models

    def select(self, body: JsonObject, command: InversionCommand) -> Receipt:
        self._auth(command,'models.manage')
        body=_selection_body(body)
        return self._select(body,None,command)

    def rollback(self, selection_id: UUID, command: InversionCommand) -> Receipt:
        self._auth(command,'models.manage')
        if type(selection_id) is not UUID: fail()
        return self._select(None,selection_id,command)

    def _select(self, body, rollback, command):
        action='selection.rollback' if rollback else 'selection.select'
        def apply(cur,cmd):
            candidate=body
            if rollback:
                cur.execute('SELECT body FROM inversion.selections WHERE id=%s',(rollback,))
                old=cur.fetchone()
                if old is None: refuse('selection_missing')
                # Reuse exactly the historic requested pins, never current repinning.
                candidate=_selection_body({key:old[0].get(key) for key in SELECTION_FIELDS})
            assembly,parameter_id,model_id=(UUID(candidate[key]) for key in ('assembly_id','parameter_id','model_id'))
            cur.execute('SELECT id,version,parameter_id,model_id FROM inversion.selections WHERE assembly_id=%s ORDER BY version DESC LIMIT 1',(assembly,))
            current=cur.fetchone(); expected(cmd,current[1] if current else 0)
            if rollback and (current is None or current[0]==rollback or current[2:]==(parameter_id,model_id)):
                refuse('rollback_current')
            parameter=self.parameters._get(cur,parameter_id); model=self.models._get(cur,model_id)
            if (candidate['parameter_sha256']!=parameter.content_sha256 or
                candidate['parameter_review_version']!=parameter.review_version or
                candidate['model_sha256']!=model.content_sha256 or
                candidate['model_review_version']!=model.review_version):
                refuse('selection_pin_mismatch')
            if parameter.review_state!='approved' or model.review_state!='approved': refuse('selection_unapproved')
            parsed=self.parameters._validate(cur,parameter)
            self.models._validate(model)
            if parameter.assembly_id!=assembly or parsed.policy.model_version!=model.installed_id or parsed.policy.algorithm!=model.body['artifact']['algorithm'] or parsed.schema!=model.body['artifact']['parameter_schema']:
                refuse('selection_incompatible')
            version=current[1]+1 if current else 1; identifier=uuid4()
            snapshot=candidate | {'policy_sha256':parameter.body['policy']['content_sha256'],
                                  'rollback_selection_id':str(rollback) if rollback else None}
            cur.execute('INSERT INTO inversion.selections(id,assembly_id,parameter_id,model_id,version,actor,reason,body) VALUES(%s,%s,%s,%s,%s,%s,%s,%s)',
                (identifier,assembly,parameter_id,model_id,version,Jsonb(asdict(cmd.actor)),cmd.reason,Jsonb(snapshot)))
            return Change(action,identifier,version,sha256(canonical_bytes(snapshot)).hexdigest(),{'id':str(identifier)})
        return self._run(action,rollback or body['assembly_id'],{'selection_id':str(rollback)} if rollback else body,
                         command,apply,waveform=True)
