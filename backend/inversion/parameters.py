"""Immutable physical declarations and independently reviewed parameter records.

authorize(actor, capability) is required and must freshly resolve authority or raise.
Read models are internal service interfaces; authenticated adapters own read access.
Attachment metadata proves identity only. Approval attests the author's declared
scope and basis; software cannot establish whether an engineering report is true.
"""
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from fractions import Fraction
from hashlib import sha256
from uuid import UUID, uuid4
from psycopg.types.json import Jsonb
from .contracts import (InversionCommand, JsonObject, canonical_bytes, strict_json, freeze,
    fields, identity, integer, name, text, utc, fail)
from .parameter_contracts import AttachmentPin
from .version_dispatch import parse_parameters as parse_parameter_set
from .geometry import build_geometry
from .transactions import InversionTransactions, InversionError, MutationRequest, LockPlan, Change, Receipt


def refuse(code): raise InversionError(code,status=409)


def expected(command, current):
    if current == 0:
        if command.expected.kind != 'absent': refuse('version_conflict')
    elif command.expected.kind != 'match' or command.expected.version != current:
        refuse('version_conflict')


def actor_key(actor):
    return (actor.domain,actor.user_id) if hasattr(actor,'domain') else (actor['domain'],actor['user_id'])


def _ns(value):
    if type(value) is not datetime or value.tzinfo is None: fail()
    delta=value-datetime(1970,1,1,tzinfo=timezone.utc)
    return (delta.days*86400+delta.seconds)*1_000_000_000+delta.microseconds*1000


def contains_interval(valid_from, valid_to, start, end):
    """Half-open declarations in UTC: adjacent ends may equal the outer end."""
    a,b=_ns(start),_ns(end)
    return a < b and _ns(valid_from) <= a and (valid_to is None or b <= _ns(valid_to))


def covers_observation(valid_from, valid_to, start_ns, end_ns, uncertainty_ns):
    """Closed actual sample support in exact Unix ns; no float/datetime rounding."""
    if any(type(value) not in (int,Fraction) for value in (start_ns,end_ns,uncertainty_ns)):
        fail()
    if start_ns>end_ns or uncertainty_ns<0: fail()
    return _ns(valid_from)<=start_ns-uncertainty_ns and (valid_to is None or end_ns+uncertainty_ns<_ns(valid_to))


@dataclass(frozen=True)
class ParameterRecord:
    id: UUID
    assembly_id: UUID
    assembly_version: int
    version: int
    content_sha256: str
    body: object
    author: object
    review_version: int
    review_state: str
    reviews: tuple
    valid_from: datetime | None
    valid_to: datetime | None


class GovernanceService:
    def __init__(self, connect, *, read_connect, authorize):
        if not all(callable(item) for item in (connect,read_connect,authorize)):
            raise ValueError('Explicit runtime connections and current authority provider required')
        self.connect,self.read_connect,self.authorize=connect,read_connect,authorize
        self.transactions=InversionTransactions(connect)

    def _auth(self, command, capability):
        if type(command) is not InversionCommand: fail()
        self.authorize(command.actor,'inversion.'+capability)

    def _run(self, action, target, body, command, apply, *, waveform=False, registry=False):
        return self.transactions.run(command,apply,request=MutationRequest(action,str(target),canonical_bytes(body)),
            dependencies=LockPlan(waveform=waveform,registry=registry))

    def _read(self, callback):
        with self.read_connect() as conn:
            conn.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY')
            conn.execute("SET LOCAL statement_timeout='3s'")
            return callback(conn.cursor())


class ParameterService(GovernanceService):
    def __init__(self, connect, *, read_connect, authorize, accounting_bucket):
        super().__init__(connect,read_connect=read_connect,authorize=authorize)
        self.bucket=name(accounting_bucket)

    def _attachments(self, cur, pins, purpose):
        if not pins: refuse('attachment_missing')
        for raw in pins:
            pin=raw if type(raw) is AttachmentPin else AttachmentPin.from_wire(raw)
            if pin.purpose != purpose: refuse('attachment_purpose')
            cur.execute('SELECT payload_sha256,accounting_bucket,media_type,payload_bytes FROM waveforms.attachment_metadata WHERE attachment_id=%s',(pin.id,))
            row=cur.fetchone()
            if row is None or row[:2] != (pin.content_sha256,self.bucket): refuse('attachment_mismatch')

    def create_assembly(self, body: JsonObject, command: InversionCommand) -> Receipt:
        self._auth(command,'parameters.manage')
        body=strict_json(canonical_bytes(body))
        fields(body,('assembly_id','manufacturer','model','asset_number','mechanism','source_kind',
                     'source_organization','valid_from','valid_to','parent_version','installation_evidence'))
        assembly=identity(body['assembly_id'])
        for key in ('manufacturer','model','asset_number','source_organization'): text(body[key],maximum=128)
        if body['mechanism'] != 'conventional_crank_balanced' or body['source_kind'] not in ('synthetic','bench_measurement','field_measurement','unknown'): fail()
        start=utc(body['valid_from']); end=utc(body['valid_to']) if body['valid_to'] is not None else None
        if end is not None and end<=start: fail()
        parent=body['parent_version']
        if parent is not None: integer(parent,minimum=1)
        digest=sha256(canonical_bytes(body)).hexdigest()
        def apply(cur,cmd):
            self._attachments(cur,body['installation_evidence'],'installation')
            cur.execute('SELECT v.id,v.version,v.body,s.revision,s.valid_from,s.valid_to FROM inversion.assembly_versions v LEFT JOIN inversion.mechanical_state s ON s.version_id=v.id WHERE v.assembly_id=%s ORDER BY v.version DESC LIMIT 1',(assembly,))
            old=cur.fetchone(); current=old[1] if old else 0
            expected(cmd,current)
            if parent != (current or None): refuse('parent_conflict')
            if old:
                if old[3] is None: refuse('physical_validity_unknown')
                if old[2]['source_kind'] != body['source_kind']:
                    refuse('assembly_identity_conflict')
                if start<=old[4]: refuse('physical_overlap')
                if old[5] is None or old[5]>start:
                    cur.execute('INSERT INTO inversion.mechanical_events(id,version_id,revision,valid_to,actor,reason,body) VALUES(%s,%s,%s,%s,%s,%s,%s)',
                        (uuid4(),old[0],old[3]+1,start,Jsonb(asdict(cmd.actor)),cmd.reason,
                         Jsonb({'successor_version':current+1,'installation_evidence':body['installation_evidence']})))
                cur.execute('INSERT INTO inversion.assembly_versions(assembly_id,version,body,content_sha256) VALUES(%s,%s,%s,%s)',(assembly,current+1,Jsonb(body),digest))
            else:
                cur.execute('INSERT INTO inversion.assemblies(id,body,content_sha256) VALUES(%s,%s,%s)',(assembly,Jsonb(body),digest))
            return Change('assembly.create',assembly,current+1,digest,{'id':str(assembly)})
        return self._run('assembly.create',assembly,body,command,apply,waveform=True)

    def assign(self, body: JsonObject, command: InversionCommand) -> Receipt:
        self._auth(command,'parameters.manage')
        body=strict_json(canonical_bytes(body))
        fields(body,('assembly_id','well_id','valid_from','valid_to','close_assignment_id'))
        assembly=identity(body['assembly_id']); well=body['well_id']; name(well)
        test=well.startswith('test:')
        if test: name(well[5:])
        else:
            if str(identity(well)) != well: fail()
        start=utc(body['valid_from']); end=utc(body['valid_to']) if body['valid_to'] is not None else None
        if end is not None and end<=start: fail()
        close=identity(body['close_assignment_id']) if body['close_assignment_id'] is not None else None
        digest=sha256(canonical_bytes(body)).hexdigest()
        def apply(cur,cmd):
            cur.execute('SELECT body FROM inversion.assemblies WHERE id=%s',(assembly,)); row=cur.fetchone()
            if row is None: refuse('assembly_missing')
            source=row[0].get('source_kind')
            allowed_sources=('synthetic','bench_measurement','field_measurement') if test else ('bench_measurement','field_measurement')
            if type(source) is not str or source not in allowed_sources: refuse('source_ineligible')
            if not test:
                cur.execute('SELECT status FROM device_registry.wells WHERE id=%s',(UUID(well),))
                if cur.fetchone() != ('active',): refuse('well_unavailable')
            if close is not None:
                cur.execute('SELECT a.assembly_id,s.version,a.valid_from,s.valid_to FROM inversion.assignments a JOIN inversion.assignment_state s ON s.assignment_id=a.id WHERE a.id=%s',(close,))
                row=cur.fetchone()
                if row is None or row[0]!=assembly: refuse('assignment_missing')
                expected(cmd,row[1])
                if start<=row[2] or row[3] is not None and start>row[3]: refuse('assignment_interval')
                cur.execute('INSERT INTO inversion.assignment_events(id,assignment_id,version,valid_to,actor,reason) VALUES(%s,%s,%s,%s,%s,%s)',
                            (uuid4(),close,row[1]+1,start,Jsonb(asdict(cmd.actor)),cmd.reason))
            else: expected(cmd,0)
            identifier=uuid4()
            cur.execute('INSERT INTO inversion.assignments(id,assembly_id,well_id,valid_from,valid_to,body,content_sha256) VALUES(%s,%s,%s,%s,%s,%s,%s)',
                        (identifier,assembly,well,start,end,Jsonb(body),digest))
            return Change('assembly.assign',identifier,1,digest,{'id':str(identifier)})
        return self._run('assembly.assign',assembly,body,command,apply,registry=not test)

    def create_version(self, body: JsonObject, command: InversionCommand) -> Receipt:
        self._auth(command,'parameters.manage')
        raw=canonical_bytes(body); parsed=parse_parameter_set(raw,draft=True); body=strict_json(raw)
        if parsed.assembly_id is None or parsed.assembly_version is None: fail()
        def apply(cur,cmd):
            cur.execute('SELECT 1 FROM inversion.assembly_versions WHERE assembly_id=%s AND version=%s',(parsed.assembly_id,parsed.assembly_version))
            if cur.fetchone() is None: refuse('assembly_version_missing')
            cur.execute('SELECT max(version) FROM inversion.parameter_versions WHERE assembly_id=%s',(parsed.assembly_id,))
            current=cur.fetchone()[0] or 0; expected(cmd,current)
            if parsed.parent_version != (current or None): refuse('parent_conflict')
            identifier=uuid4()
            cur.execute('INSERT INTO inversion.parameter_versions(id,assembly_id,assembly_version,version,parent_version,author,body,content_sha256) VALUES(%s,%s,%s,%s,%s,%s,%s,%s)',
                (identifier,parsed.assembly_id,parsed.assembly_version,current+1,parsed.parent_version,Jsonb(asdict(cmd.actor)),Jsonb(body),parsed.raw_sha256))
            return Change('parameter.create',identifier,current+1,parsed.raw_sha256,{'id':str(identifier)})
        return self._run('parameter.create',parsed.assembly_id,body,command,apply)

    def _get(self, cur, identifier):
        if type(identifier) is not UUID: fail()
        cur.execute('SELECT assembly_id,assembly_version,version,content_sha256,body,author FROM inversion.parameter_versions WHERE id=%s',(identifier,))
        row=cur.fetchone()
        if row is None: refuse('parameter_missing')
        cur.execute('SELECT version,action,actor,reason,body FROM inversion.reviews WHERE subject_id=%s ORDER BY version',(identifier,))
        reviews=tuple(freeze(dict(zip(('version','action','actor','reason','body'),entry))) for entry in cur.fetchall())
        state={'submit':'submitted','approve':'approved','reject':'rejected','revoke':'revoked'}[reviews[-1]['action']] if reviews else 'draft'
        body=row[4]
        return ParameterRecord(identifier,*row[:4],freeze(body),freeze(row[5]),reviews[-1]['version'] if reviews else 1,
            state,reviews,utc(body['valid_from']) if body.get('valid_from') else None,utc(body['valid_to']) if body.get('valid_to') else None)

    def get(self, id: UUID) -> ParameterRecord: return self._read(lambda cur:self._get(cur,id))

    def _evidence(self, cur, pin, kind, endpoint, source, start, end, observation, uncertainty_ns):
        cur.execute('SELECT kind,content_sha256,body,accounting_bucket FROM waveforms.evidence_versions WHERE evidence_id=%s',(pin.id,))
        row=cur.fetchone()
        cur.execute('SELECT version,state FROM waveforms.evidence_events WHERE evidence_id=%s ORDER BY version DESC LIMIT 1',(pin.id,))
        event=cur.fetchone()
        if row is None or row[0]!=kind or row[1]!=pin.content_sha256 or row[3]!=self.bucket or event!=(pin.review_version,'approved'):
            refuse('evidence_unavailable')
        body=row[2]
        if body['source_kind'] != source or body.get('endpoint') != endpoint: refuse('evidence_binding')
        if body.get('valid_from') is None: refuse('evidence_interval')
        valid_from=utc(body['valid_from']); valid_to=utc(body['valid_to']) if body.get('valid_to') else None
        eligible=(covers_observation(valid_from,valid_to,start,end,uncertainty_ns) if observation else
                  contains_interval(valid_from,valid_to,start,end))
        if not eligible:
            refuse('evidence_interval')
        return body

    def _sync_evidence(self,cur,pin,source):
        """Optional archived sync pins are real approval identities, not flags."""
        cur.execute('SELECT kind,content_sha256,body,accounting_bucket FROM waveforms.evidence_versions WHERE evidence_id=%s',(pin.id,))
        saved=cur.fetchone()
        cur.execute('SELECT version,state FROM waveforms.evidence_events WHERE evidence_id=%s ORDER BY version DESC LIMIT 1',(pin.id,))
        if (saved is None or saved[0]!='sync' or saved[1]!=pin.content_sha256 or saved[3]!=self.bucket
            or cur.fetchone()!=(pin.review_version,'approved')): refuse('archive_sync_unapproved')
        if saved[2]['source_kind']!=source: refuse('archive_sync_source')
        return saved[2]

    def _validate(self, cur, record, *, start=None, end=None, uncertainty_ns=0,alignment_ns=None):
        parameter=parse_parameter_set(canonical_bytes(record.body))
        if parameter.source_kind=='unknown': refuse('source_ineligible')
        observation=start is not None or end is not None
        if observation and (start is None or end is None): fail()
        if not observation:
            if uncertainty_ns != 0: fail()
            start,end=parameter.valid_from,parameter.valid_to
        def contained(valid_from,valid_to):
            return (covers_observation(valid_from,valid_to,start,end,uncertainty_ns) if observation else
                    contains_interval(valid_from,valid_to,start,end))
        if not contained(parameter.valid_from,parameter.valid_to): refuse('parameter_interval')
        cur.execute('SELECT v.body,s.valid_from,s.valid_to FROM inversion.assembly_versions v JOIN inversion.mechanical_state s ON s.version_id=v.id WHERE v.assembly_id=%s AND v.version=%s',(record.assembly_id,record.assembly_version))
        mechanical=cur.fetchone()
        if mechanical is None or not contained(mechanical[1],mechanical[2]): refuse('physical_interval')
        if mechanical[0]['source_kind']!=parameter.source_kind:
            refuse('source_mismatch')
        self._attachments(cur,mechanical[0]['installation_evidence'],'installation')
        for group in ('geometry','phase','bodies','drive','domain','uncertainty'):
            self._attachments(cur,getattr(parameter,group).evidence_refs,group)
        self._attachments(cur,parameter.drive.loss_grid.evidence_refs,'loss_grid')
        provenance=parameter.provenance
        self._attachments(cur,provenance.attachment_pins,'provenance')
        for condition in provenance.operating_conditions:
            self._attachments(cur,condition.attachment_pins,'operating_conditions')
            # A condition record may cover a wider period. Task8 checks its own
            # interval against the observation, separately from parameter validity.
            for field in ('temperature_K','supply_voltage_V','supply_frequency_Hz'):
                actual,domain=getattr(condition,field),getattr(parameter.domain,field)
                if actual.min<domain.min or actual.max>domain.max: refuse('condition_domain')
        alignment=uncertainty_ns if alignment_ns is None else alignment_ns
        if type(alignment) not in (int,Fraction) or alignment<0: fail()
        if parameter.schema=='inversion.parameter-set/2':
            from .coverage_sources import verify_declarations
            verify_declarations(parameter)
            coverage=parameter.coverage_contract.body
            if Fraction(coverage['time']['common_offset_bound_ns'])+Fraction(coverage['time']['differential_bound_ns'])<alignment:
                refuse('timing_envelope')
            budget=strict_json(canonical_bytes(coverage['binding']['budget_attachment_pin']))
            self._attachments(cur,[budget],budget['purpose'])
        else:
            coverage=provenance.coverage
            if int(coverage['max_time_alignment_error_ns'])<alignment: refuse('timing_envelope')
            for field in ('domain','uncertainty'):
                if coverage[field+'_sha256']!=sha256(canonical_bytes(record.body[field])).hexdigest(): refuse('coverage_mismatch')
            if coverage['policy_sha256']!=record.body['policy']['content_sha256']: refuse('coverage_mismatch')
            for item in coverage['scopes']: self._attachments(cur,[strict_json(canonical_bytes(item['attachment_pin']))],item['scope'])
        for binding in provenance.channel_bindings:
            self._attachments(cur,binding.installation_evidence_refs,'installation')
            pin=binding.channel_pin
            if pin.sync_pin is not None: self._sync_evidence(cur,pin.sync_pin,parameter.source_kind)
            endpoint=dict(gateway_id=pin.physical_endpoint.gateway_id,device_id=pin.physical_endpoint.device_id,
                          stream_id=pin.stream_id,channel_id=pin.channel_id)
            sampling=self._evidence(cur,pin.sampling_pin,'sampling_config',endpoint,parameter.source_kind,start,end,observation,uncertainty_ns)
            calibration=self._evidence(cur,pin.calibration_pin,'calibration',endpoint,parameter.source_kind,start,end,observation,uncertainty_ns)
            if calibration['sampling_config_version_id']!=str(pin.sampling_pin.id) or calibration['sampling_config_sha256']!=pin.sampling_pin.content_sha256:
                refuse('calibration_binding')
            if any(calibration.get(key) is None for key in ('scale','offset','calibrated_range','measurement_uncertainty','time_correction_ns','residual_ns','method','basis')):
                refuse('calibration_incomplete')
        return parameter

    def check_current(self, id, *, start=None, end=None, uncertainty_ns=0):
        """Internal read check; Task8 uses _validate on its already-guarded cursor."""
        def check(cur):
            record=self._get(cur,id)
            if record.review_state!='approved': refuse('parameter_unapproved')
            self._validate(cur,record,start=start,end=end,uncertainty_ns=uncertainty_ns)
            return record
        return self._read(check)

    def submit(self, id: UUID, command: InversionCommand) -> Receipt: return self._transition(id,'submit',command)
    def review(self, id: UUID, decision: str, command: InversionCommand) -> Receipt:
        if decision not in ('approve','reject'): fail()
        return self._transition(id,decision,command)
    def revoke(self, id: UUID, command: InversionCommand) -> Receipt: return self._transition(id,'revoke',command)

    def _transition(self, id, action, command):
        self._auth(command,'parameters.manage' if action=='submit' else 'parameters.review')
        if type(id) is not UUID: fail()
        # Immutable content and expensive full-cycle geometry are prepared without write locks.
        if action in ('submit','approve'):
            prepared=self.get(id)
            build_geometry(parse_parameter_set(canonical_bytes(prepared.body)))
        def apply(cur,cmd):
            record=self._get(cur,id); expected(cmd,record.review_version)
            if action=='submit':
                if record.review_state!='draft': refuse('review_transition')
            elif action in ('approve','reject'):
                if record.review_state!='submitted': refuse('review_transition')
                submitter=next(item['actor'] for item in record.reviews if item['action']=='submit')
                if actor_key(cmd.actor) in (actor_key(record.author),actor_key(submitter)): refuse('self_review')
            elif record.review_state!='approved': refuse('review_transition')
            if action in ('submit','approve'): self._validate(cur,record)
            version=record.review_version+1
            attestation={'parameter_sha256':record.content_sha256}
            if action in ('submit','approve'):
                if record.body['schema']=='inversion.parameter-set/2':
                    attestation['coverage_contract']=strict_json(canonical_bytes(record.body['coverage_contract']))
                    attestation['parameter_core_sha256']=record.body['coverage_contract']['binding']['parameter_core_sha256']
                    attestation['native_source_verification']='required_at_execution'
                else:
                    attestation['coverage']=strict_json(canonical_bytes(record.body['provenance']['coverage']))
                attestation['meaning']='Independent human review of declared engineering basis; not field precision certification'
            cur.execute('INSERT INTO inversion.reviews(id,subject_id,version,action,actor,reason,body) VALUES(%s,%s,%s,%s,%s,%s,%s)',
                (uuid4(),id,version,action,Jsonb(asdict(cmd.actor)),cmd.reason,Jsonb(attestation)))
            return Change('parameter.'+action,id,version,record.content_sha256,{'id':str(id),'action':action})
        return self._run('parameter.'+action,id,{},command,apply,waveform=True)
