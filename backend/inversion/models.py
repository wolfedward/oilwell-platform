"""Registration of already-installed, server-owned artifacts; never executes uploads."""
from dataclasses import asdict, dataclass
from hashlib import sha256
from importlib.metadata import version as package_version
from pathlib import Path
import platform
import sys
from types import MappingProxyType
from uuid import UUID, uuid4
from psycopg.types.json import Jsonb
from .contracts import InversionCommand, canonical_bytes, freeze, integer, fail
from .geometry import NUMERICAL_ASSUMPTIONS
from .parameters import GovernanceService, expected, actor_key, refuse
from .transactions import Change, Receipt


@dataclass(frozen=True)
class InstalledModel:
    installed_id: str
    version: int
    files: tuple[Path,...]

    def __post_init__(self):
        integer(self.version,minimum=1)
        if type(self.installed_id) is not str or not self.installed_id or type(self.files) is not tuple or not self.files:
            fail()

    def snapshot(self):
        try:
            # Paths belong solely to the installed server configuration, not any command body.
            files=[{'name':path.name,'sha256':sha256(path.read_bytes()).hexdigest()} for path in self.files]
            policy=sha256(Path(__file__).with_name('policy.py').read_bytes()).hexdigest()
        except OSError: refuse('artifact_unavailable')
        return dict(installed_id=self.installed_id,algorithm='beam_energy_balance/1',
            parameter_schema='inversion.parameter-set/1',files=files,policy_contract_sha256=policy,
            python=dict(implementation=platform.python_implementation(),version=sys.version,
                        platform=platform.platform(),machine=platform.machine()),
            dependencies={'psycopg':package_version('psycopg')},
            numerical_assumptions=[*NUMERICAL_ASSUMPTIONS,
                'Local QR error and motion reconstruction bounds are conditional software estimates; approved physical envelopes remain required',
                'Artifact approval does not certify hardware, field accuracy, or unseen dynamics'])


_ROOT=Path(__file__).parent
INSTALLED_MODELS=MappingProxyType({'beam_energy_balance/1':InstalledModel('beam_energy_balance/1',1,
    tuple(_ROOT/name for name in ('contracts.py','parameter_contracts.py','series.py','policy.py','geometry.py',
        'mechanics.py','small_linalg.py','motion.py','losses.py','solver.py','comparison.py','display_fill.py')))})


@dataclass(frozen=True)
class ModelRecord:
    id: UUID
    installed_id: str
    version: int
    content_sha256: str
    body: object
    review_version: int
    review_state: str
    reviews: tuple


class ModelService(GovernanceService):
    def __init__(self, connect, *, read_connect, authorize, installed=INSTALLED_MODELS):
        super().__init__(connect,read_connect=read_connect,authorize=authorize)
        if not installed or any(type(item) is not InstalledModel or key!=item.installed_id for key,item in installed.items()): fail()
        self.installed=MappingProxyType(dict(installed))

    def _artifact(self, installed_id):
        if type(installed_id) is not str or installed_id not in self.installed: refuse('model_not_installed')
        return self.installed[installed_id].snapshot()

    def register(self, installed_id: str, command: InversionCommand) -> Receipt:
        self._auth(command,'models.manage')
        artifact=self._artifact(installed_id)
        body={'artifact':artifact,'registrar':asdict(command.actor)}
        digest=sha256(canonical_bytes(body)).hexdigest()
        def apply(cur,cmd):
            expected(cmd,0)
            if self._artifact(installed_id)!=artifact: refuse('artifact_changed')
            cur.execute('SELECT 1 FROM inversion.model_versions WHERE model_key=%s',(installed_id,))
            if cur.fetchone(): refuse('model_registered')
            identifier=uuid4(); version=self.installed[installed_id].version
            cur.execute('INSERT INTO inversion.model_versions(id,model_key,version,body,content_sha256) VALUES(%s,%s,%s,%s,%s)',
                (identifier,installed_id,version,Jsonb(body),digest))
            return Change('model.register',identifier,1,digest,{'id':str(identifier)})
        return self._run('model.register',installed_id,{'installed_id':installed_id},command,apply)

    def _get(self, cur, id):
        if type(id) is not UUID: fail()
        cur.execute('SELECT model_key,version,content_sha256,body FROM inversion.model_versions WHERE id=%s',(id,))
        row=cur.fetchone()
        if row is None: refuse('model_missing')
        cur.execute('SELECT version,action,actor,reason,body FROM inversion.model_reviews WHERE subject_id=%s ORDER BY version',(id,))
        reviews=tuple(freeze(dict(zip(('version','action','actor','reason','body'),entry))) for entry in cur.fetchall())
        state={'approve':'approved','reject':'rejected','revoke':'revoked'}[reviews[-1]['action']] if reviews else 'unreviewed'
        return ModelRecord(id,*row[:3],freeze(row[3]),reviews[-1]['version'] if reviews else 1,state,reviews)

    def get(self, id: UUID) -> ModelRecord: return self._read(lambda cur:self._get(cur,id))

    def _validate(self, record):
        if canonical_bytes(record.body['artifact'])!=canonical_bytes(self._artifact(record.installed_id)):
            refuse('artifact_changed')

    def check_current(self, id):
        record=self.get(id)
        if record.review_state!='approved': refuse('model_unapproved')
        self._validate(record)
        return record

    def review(self, id: UUID, decision: str, command: InversionCommand) -> Receipt:
        if decision not in ('approve','reject'): fail()
        return self._transition(id,decision,command)

    def revoke(self, id: UUID, command: InversionCommand) -> Receipt: return self._transition(id,'revoke',command)

    def _transition(self, id, action, command):
        self._auth(command,'models.review')
        if type(id) is not UUID: fail()
        def apply(cur,cmd):
            record=self._get(cur,id); expected(cmd,record.review_version)
            if action=='revoke':
                if record.review_state!='approved': refuse('review_transition')
            else:
                if record.review_state!='unreviewed': refuse('review_transition')
                if actor_key(cmd.actor)==actor_key(record.body['registrar']): refuse('self_review')
                if action=='approve': self._validate(record)
            version=record.review_version+1
            cur.execute('INSERT INTO inversion.model_reviews(id,subject_id,version,action,actor,reason,body) VALUES(%s,%s,%s,%s,%s,%s,%s)',
                (uuid4(),id,version,action,Jsonb(asdict(cmd.actor)),cmd.reason,
                 Jsonb({'artifact_record_sha256':record.content_sha256,'meaning':'Reviewed installed artifact; no field certification'})))
            return Change('model.'+action,id,version,record.content_sha256,{'id':str(id),'action':action})
        return self._run('model.'+action,id,{},command,apply)
