"""Server-acquired v2 native evidence and explicit, bounded snapshot encoding."""
from dataclasses import dataclass
from fractions import Fraction as Q
from hashlib import sha256
import struct
from types import SimpleNamespace

from backend.waveforms.pair_contracts import capture_key
from backend.waveforms.quality_context import utc_ns
from .contracts import canonical_bytes,strict_json,fields,digest,name,fail
from .coverage_contracts import ChannelEvidence,PredictionEvidence,channel_evidence_bytes,exact_fraction
from .coverage_sources import inputs_from_evidence
from .input_contracts import PreparedInput
from .parameter_contracts_v2 import ParameterSetV2
from .series import ChannelPin
from .transactions import InversionError


def refuse(code): raise InversionError(code,status=409)


def prediction_evidence_bytes(evidence):
    if type(evidence) is not PredictionEvidence: fail()
    def channel(item): return strict_json(channel_evidence_bytes(item))|{'channel_sha256':item.channel_sha256}
    return canonical_bytes(dict(schema='inversion.prediction-evidence/1',power=channel(evidence.power),angle=channel(evidence.angle),
        pack_id=evidence.pack_id,case_id=evidence.case_id,config_sha256=evidence.config_sha256,generator_sha256=evidence.generator_sha256))


def parse_prediction_evidence(raw):
    body=fields(strict_json(raw),('schema','power','angle','pack_id','case_id','config_sha256','generator_sha256'))
    if body['schema']!='inversion.prediction-evidence/1': fail()
    def channel(item):
        fields(item,ChannelEvidence.__dataclass_fields__)
        for key in ('raw','quality','time_ns'):
            if type(item[key]) is not list or not 1<=len(item[key])<=32768: fail()
        values=item|dict(raw=tuple(item['raw']),quality=tuple(item['quality']),time_ns=tuple(exact_fraction(v) for v in item['time_ns']),
            scale=exact_fraction(item['scale']),offset=exact_fraction(item['offset']),pin=ChannelPin.from_wire(item['pin']))
        return ChannelEvidence(**values)
    return PredictionEvidence(channel(body['power']),channel(body['angle']),body['pack_id'],body['case_id'],body['config_sha256'],body['generator_sha256'])


def check_evidence_binding(evidence,parameters):
    coverage=parameters.coverage_contract.body
    if any(coverage[key]!=getattr(evidence,key) for key in ('pack_id','case_id','config_sha256','generator_sha256')):
        refuse('input_evidence_mismatch')
    for channel in (evidence.power,evidence.angle):
        expected=coverage['inputs'][channel.role]
        if (channel.pin!=ChannelPin.from_wire(strict_json(canonical_bytes(expected['pin'])))
                or len(channel.raw)!=expected['count']
                or channel.scale!=Q(expected['encoding']['scale']) or channel.offset!=Q(expected['encoding']['offset'])
                or any(getattr(channel,key)!=expected[key] for key in ('original_file_sha256','bound_file_sha256','channel_sha256'))):
            refuse('input_evidence_mismatch')
        for key,values in (('raw',channel.raw),('quality',channel.quality),('time',[str(t) for t in channel.time_ns])):
            if sha256(canonical_bytes(values)).hexdigest()!=expected[key+'_sha256']: refuse('input_evidence_mismatch')


@dataclass(frozen=True)
class PreparedInputV2(PreparedInput):
    prediction_evidence: PredictionEvidence

    def __post_init__(self):
        if type(self.parameters) is not ParameterSetV2 or type(self.prediction_evidence) is not PredictionEvidence: fail()
        check_evidence_binding(self.prediction_evidence,self.parameters)
        body=self.snapshot.body
        if (body['schema']!='inversion.input/2' or body['parameter_core_sha256']!=self.parameters.parameter_core_sha256
            or self.prediction_inputs!=inputs_from_evidence(self.prediction_evidence,self.parameters)
            or parse_prediction_evidence(canonical_bytes(body['prediction_evidence']))!=self.prediction_evidence):
            refuse('input_evidence_mismatch')


def _origin_mapping(selection,records,data,frame,descriptor):
    """Original identity is reviewed provenance; bound bytes are actually read."""
    matches=[]
    try:
        chosen=next(item for item in selection['captures'] if capture_key(item['key'])==data.key)
        for ref in chosen['origins']:
            item=records[ref['id']];body=item.body
            if item.kind!='origin' or capture_key(body['capture'])!=data.key or dict(body['source_identity'])!=dict(data.stream.source):
                continue
            if (item.review_state!='approved' or item.version!=int(ref['version']) or item.content_sha256!=ref['content_sha256']
                    or body['source_kind']!='synthetic' or body['verification_method']!='testpack_original_bound/1'):
                refuse('input_origin_mapping')
            basis=body['verification_basis']
            if type(basis) is not str or len(basis)>2048: refuse('input_origin_mapping')
            mapping=fields(strict_json(basis.encode()),('schema','pack_id','case_id','config_sha256','generator_sha256','original_file_sha256','bound_file_sha256'))
            if mapping['schema']!='owp.testpack.binding/1': refuse('input_origin_mapping')
            for key in ('pack_id','case_id'): name(mapping[key])
            for key in ('config_sha256','generator_sha256','original_file_sha256','bound_file_sha256'): digest(mapping[key])
            provenance=body['provenance']
            imports=[entry for entry in descriptor['source_files'] if entry['import_id']==provenance['import_id']]
            if (provenance['transport']!='file' or provenance['import_id']!=provenance['source_ref']
                    or provenance['manifest_sha256']!=frame.raw_sha256 or len(imports)!=1
                    or imports[0]['file_sha256']!=mapping['bound_file_sha256']
                    or imports[0]['bytes']!=descriptor['content_length']
                    or descriptor['file_sha256']!=mapping['bound_file_sha256']):
                refuse('input_origin_mapping')
            matches.append(mapping|dict(archive_file_sha256=descriptor['file_sha256'],import_id=provenance['import_id'],
                manifest_sha256=frame.raw_sha256,origin_pin=dict(ref),original_identity_basis='approved_origin_mapping'))
    except (KeyError,TypeError,ValueError,StopIteration) as error:
        raise InversionError('input_origin_mapping',status=409) from error
    if len(matches)!=1: refuse('input_origin_mapping')
    return matches[0]


def extract_prediction_evidence(context,manifests,files,records,pins,selection,*,purpose,check):
    if purpose!='software_pair_test' or selection['purpose']!='software_pair_test': refuse('v2_purpose_ineligible')
    anchor=utc_ns(manifests[context.key])
    if anchor is None: refuse('observation_utc_unknown')
    channels={};mappings=[];identity=None
    for data in context.streams:
        if data.role not in ('active_power','crank_angle'): continue
        frame=manifests[data.key]
        if frame.body['source_kind']!='synthetic' or data.channel.encoding!='i32le': refuse('v2_source_ineligible')
        mapping=_origin_mapping(selection,records,data,frame,files[data.key]['descriptor'])
        current=tuple(mapping[key] for key in ('pack_id','case_id','config_sha256','generator_sha256'))
        if identity is not None and identity!=current: refuse('input_origin_mapping')
        identity=current
        if mapping not in mappings: mappings.append(mapping)
        # ChannelData.sample intentionally hides quality=1 raw codes. Preserve
        # the native i32 and quality byte here, including missing raw zero.
        offset=data.stream.channels.index(data.channel)*5
        raw=[];quality=[];times=[]
        for index in range(data.first_index,data.last_index+1):
            if index%256==0: check()
            code,flag=struct.unpack_from('<iB',data.data,(index-data.stream.first_sample_index)*data.stream.frame_bytes+offset)
            time=data.time(index)
            if time is None: refuse('timing_unknown')
            raw.append(code);quality.append(flag);times.append(Q(anchor)+time)
        # Normalization uses the actual approved affine calibration, not scale
        # copied from the coverage contract or a fixed generator description.
        from .input_acquisition import UNITS
        calibration=records[next(c for c in selection['channels'] if c['role']==data.role)['calibration']['id']]
        factor=UNITS[data.role][calibration.body['unit']]
        value=dict(role=data.role,raw=tuple(raw),quality=tuple(quality),time_ns=tuple(times),
            scale=data.scale*factor,offset=data.offset*factor,pin=pins[data.role],
            original_file_sha256=mapping['original_file_sha256'],bound_file_sha256=mapping['bound_file_sha256'])
        value['channel_sha256']=sha256(channel_evidence_bytes(SimpleNamespace(**value))).hexdigest()
        channels[data.role]=ChannelEvidence(**value)
    if set(channels)!= {'active_power','crank_angle'}: refuse('required_role_missing')
    return PredictionEvidence(channels['active_power'],channels['crank_angle'],*identity),tuple(mappings)
