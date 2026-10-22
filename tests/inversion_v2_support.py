"""Pure synthetic parser fixtures; no approvals, database or installed case proof."""
import json
from pathlib import Path
from fractions import Fraction as Q
from hashlib import sha256
from types import SimpleNamespace


def fixture_body():
    return json.loads((Path(__file__).parent/'fixtures/inversion_v2_contract.json').read_bytes())


def registered_fixture(case_id='cycle-base'):
    """Native numeric evidence only: fake pins confer NO service approval."""
    from backend.inversion.contracts import canonical_bytes
    from backend.inversion.coverage_contracts import ChannelEvidence,PredictionEvidence,channel_evidence_bytes,parameter_core_bytes,pin_wire
    from backend.inversion.series import ChannelPin
    from backend.inversion.synthetic_registry import installed_config,case_definition
    from tools.testpack.cycles_v2 import generate_case_v2
    from tools.testpack.storage import generator_fingerprint_v2
    from backend.inversion.coverage_sources import checker_sha256
    body=fixture_body(); coverage=body['coverage_contract']
    config_sha=sha256(canonical_bytes(installed_config('smoke'))).hexdigest()
    definition=case_definition(config_sha,case_id); cycle=generate_case_v2(definition)
    for key in ('geometry','bodies'):
        body[key].update(dict(definition.body[key]))
    body['drive'].update({key:value for key,value in definition.body['drive'].items() if key not in ('loss_W','loss_error_W')})
    gen_sha=generator_fingerprint_v2()['source_sha256']
    coverage.update(case_id=case_id,config_sha256=config_sha,generator_sha256=gen_sha)
    epoch=1790784000000000000; channels=[]
    for channel in cycle.channels[:2]:
        role=channel.role
        pin=ChannelPin.from_wire(coverage['inputs'][role]['pin'])
        values=dict(role=role,raw=channel.raw,time_ns=tuple(Q(epoch+t) for t in cycle.time_ns),quality=channel.quality,
            scale=Q(channel.scale),offset=Q(0),pin=pin,original_file_sha256='c'*64,bound_file_sha256='d'*64)
        values['channel_sha256']=sha256(channel_evidence_bytes(SimpleNamespace(**values))).hexdigest()
        evidence=ChannelEvidence(**values); channels.append(evidence)
        item=coverage['inputs'][role]
        item.update(pin=pin_wire(pin),channel_sha256=evidence.channel_sha256,
            raw_sha256=sha256(canonical_bytes(channel.raw)).hexdigest(),
            time_sha256=sha256(canonical_bytes([str(t) for t in evidence.time_ns])).hexdigest(),
            quality_sha256=sha256(canonical_bytes(channel.quality)).hexdigest())
    speed=case_id=='cycle-speed'; noisy=case_id=='cycle-noise'
    coverage['motion'].update(theta0_rad='4.68689893991090706065',rate_rad_s='0.78539816339744830962',
        epsilon_rad='0.1' if speed else '0',omega_max_rad_s='0.864' if speed else '0.786',
        acceleration_max_rad_s2='0.062' if speed else '0',third_derivative_max_rad_s3='0.049' if speed else '0',
        fourth_derivative_max_rad_s4='0.039' if speed else '0',
        support_start_ns=str(epoch-31250000),support_end_ns=str(epoch+8031250000))
    coverage['measurement'].update(angle_noise_error_rad='0.000001' if noisy else '0',power_noise_error_W='10' if noisy else '0')
    coverage['time']['common_offset_bound_ns']='15625000' if case_id=='cycle-shifted' else '0'
    coverage['binding']['checker_sha256']=checker_sha256()
    coverage['binding']['parameter_core_sha256']=sha256(parameter_core_bytes(body)).hexdigest()
    evidence=PredictionEvidence(*channels,coverage['pack_id'],case_id,config_sha,gen_sha)
    return body,evidence
