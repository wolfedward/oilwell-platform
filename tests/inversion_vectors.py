"""Hand-authored valid wire examples for the inversion contract tests."""

import json
from hashlib import sha256


PIN = {"id": "11111111-1111-4111-8111-111111111111", "content_sha256": "a" * 64, "review_version": 1}
CHANNEL = {
    "gateway_id": "gateway-1", "device_id": "device-1",
    "capture_id": "22222222-2222-4222-8222-222222222222",
    "stream_id": "stream-1", "channel_id": "channel-1", "manifest_sha256": "b" * 64,
    "calibration_pin": PIN, "sampling_pin": PIN,
    "first_index": 0, "last_index": 10,
}


def policy_body():
    return {
        "schema": "inversion.algorithm-policy/1", "algorithm": "beam_energy_balance/1",
        "model_version": "beam_energy_balance/1", "numerical_margin": "0.001",
        "motion_max_time_span_ns": "100000000", "motion_max_angle_span_rad": "0.5",
        "motion_max_residual_rad": "0.01", "motion_max_condition": "10000",
        "max_load_error_N": "1000", "min_coverage_fraction": "0.8",
        "reference_max_gap_ns": "100000000", "fill_max_time_span_ns": "50000000",
        "fill_max_angle_span_rad": "0.2", "relative_error_min_denominator_N": "1",
    }


def policy_wire():
    return json.dumps(policy_body(), sort_keys=True, separators=(",", ":")).encode()


def parameter_body():
    policy = policy_body()
    policy_sha = sha256(policy_wire()).hexdigest()
    body = json.loads(json.dumps({
        "schema": "inversion.parameter-set/1",
        "assembly_id": "33333333-3333-4333-8333-333333333333",
        "assembly_version": 1, "source_kind": "synthetic",
        "valid_from": "2026-01-01T00:00:00.000000Z",
        "valid_to": "2027-01-01T00:00:00.000000Z", "parent_version": None,
        "provenance": {
            "source_organization": "Software test", "source_date": "2026-01-01", "notes": "Synthetic software evidence",
            "attachment_pins": [PIN],
            "calibration_cycles": [{"start_ns": "0", "end_ns": "1000000000", "physical_cycle_id": "cycle-1"}],
            "operating_conditions": [{
                "id": "condition-1", "valid_from": "2026-01-01T00:00:00.000000Z",
                "valid_to": "2027-01-01T00:00:00.000000Z",
                "temperature_K": {"min": "290", "max": "310"},
                "supply_voltage_V": {"min": "380", "max": "420"},
                "supply_frequency_Hz": {"min": "49", "max": "51"},
                "source_description": "Controlled software case", "attachment_pins": [PIN],
            }],
            "channel_bindings": [{
                "role": role, "source": "native_waveform", "channel_pin": CHANNEL,
                "installation_evidence_refs": [PIN], "modbus": None,
            } for role in ("active_power", "crank_angle", "load", "displacement")],
        },
        "geometry": {
            "pivot_x_m": "1", "pivot_y_m": "2", "crank_radius_m": "0.5",
            "pitman_length_m": "2", "rear_arm_m": "1", "horsehead_radius_m": "1",
            "branch": 1, "rotation_sign": 1, "displacement_sign": 1,
            "evidence_refs": [PIN],
        },
        "phase": {
            "channel_pin": CHANNEL, "zero_reading_rad": "0", "encoder_direction": 1,
            "unwrap_kind": "wrapped_2pi", "evidence_refs": [PIN],
        },
        "bodies": {
            "crank_inertia_kg_m2": "1", "crank_first_moment_x_kg_m": "0",
            "crank_first_moment_y_kg_m": "0", "beam_inertia_kg_m2": "1",
            "beam_first_moment_x_kg_m": "0", "beam_first_moment_y_kg_m": "0",
            "pitman_mass_kg": "1", "pitman_com_fraction": "0.5",
            "pitman_inertia_kg_m2": "1", "gravity_m_s2": "9.80665",
            "component_ledger": [{"id": "motor", "name": "Motor", "owner": "upstream"}],
            "evidence_refs": [PIN],
        },
        "drive": {
            "supply_kind": "direct_line_induction", "ratio": "10",
            "upstream_inertia_kg_m2": "1",
            "loss_grid": {"power_W": ["-10", "10"], "motor_speed_rad_s": ["1", "2"],
                          "loss_W": [["0", "0"], ["0", "0"]],
                          "error_W": [["0", "0"], ["0", "0"]], "evidence_refs": [PIN]},
            "crank_friction_Nm": "0", "viscous_friction_Nm_s": "0",
            "evidence_refs": [PIN],
        },
        "domain": {
            **{name: {"min": "0", "max": "10"} for name in (
                "power_W", "speed_rad_s", "acceleration_rad_s2", "temperature_K",
                "supply_voltage_V", "supply_frequency_Hz", "bandwidth_Hz", "window_ns", "load_N")},
            "evidence_refs": [PIN],
        },
        "uncertainty": {
            "torque_bound_Nm": "0", "factor_bound_m": "0", "speed_bound_rad_s": "0",
            "numerical_budget_Nm": "0", "numerical_factor_budget_m": "0", "evidence_refs": [PIN],
        },
        "policy": {"body": policy, "content_sha256": policy_sha},
    }))


    def attachment(purpose):
        return {'id': PIN['id'], 'content_sha256': PIN['content_sha256'],
                'purpose': purpose, 'source_description': 'Explicit synthetic report section for ' + purpose}
    for group in ('geometry', 'phase', 'bodies', 'drive', 'domain', 'uncertainty'):
        body[group]['evidence_refs'] = [attachment(group)]
    body['drive']['loss_grid']['evidence_refs'] = [attachment('loss_grid')]
    body['provenance']['attachment_pins'] = [attachment('provenance')]
    for item in body['provenance']['operating_conditions']:
        item['attachment_pins'] = [attachment('operating_conditions')]
    for item in body['provenance']['channel_bindings']:
        item['installation_evidence_refs'] = [attachment('installation')]
    canonical = lambda item: json.dumps(item, sort_keys=True, separators=(',', ':')).encode()
    body['provenance']['coverage'] = {
        'policy_sha256': policy_sha,
        'domain_sha256': sha256(canonical(body['domain'])).hexdigest(),
        'uncertainty_sha256': sha256(canonical(body['uncertainty'])).hexdigest(),
        'max_time_alignment_error_ns': '0',
        'scopes': [{'scope': scope, 'attachment_pin': attachment(scope),
                    'basis': 'Known synthetic bound; uN/uJ contribution described in section ' + scope}
                   for scope in ('loss_calibration', 'time_alignment', 'averaged_power_mismatch',
                     'motion_angle_policy', 'upstream_dynamics', 'load_range', 'geometry_envelope', 'crank_friction')]}
    return body


def parameter_wire(body=None):
    return json.dumps(parameter_body() if body is None else body, sort_keys=True,
                      separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def acquired_case(case,*,count=129,later=False,angle=True,archive_sync=True,producer=None,
                  later_producer=None,changed_calibration=False,shared_selected=False,
                  mapping_offset=0,mapping_ppm='0',later_utc='2026-09-29T00:01:00.000000Z',
                  parameter_end=None,condition_end=None,reference_end='2000000001',
                  two=False,utc_uncertainties=(0,0),alignment_limit=0):
    """Genuine WP04 intake/evidence/pair/freeze and independently approved governance."""
    from copy import deepcopy
    from dataclasses import replace
    from uuid import uuid4
    from backend.waveforms.commands import Command,pair_request_hash
    from backend.waveforms.pairing import PairService
    from backend.waveforms.member_exports import MemberResearchExporter
    from backend.inversion.models import ModelService
    from backend.inversion.selections import SelectionService
    from backend.inversion.contracts import canonical_bytes
    from backend.waveforms.registry_context import datetime_ns
    from backend.inversion.contracts import utc
    from tests.waveform_dataset_support import DatasetCase
    from tests.waveform_pairing_support import seed_pair,CREATOR,REVIEWER
    from tests.waveform_support import LIMITS
    from tests.inversion_governance_support import command,AUTHOR,REVIEWER as INV_REVIEWER
    archive=seed_pair(case,count=count,inversion=True,producer=producer,shared_selected=shared_selected,
                      two=two,utc_uncertainties=utc_uncertainties)
    reuse=archive['channel_evidence']
    if changed_calibration:
        reuse=dict(reuse)
        config,cal=reuse['active_power']
        from backend.waveforms.evidence_contracts import thaw
        reuse['active_power']=(config,case.approved_evidence('calibration',thaw(cal.body)))
    vector=seed_pair(case,count=count,inversion=True,reuse_evidence=None if later_producer else reuse,
                     utc_start=later_utc,producer=later_producer or producer,shared_selected=shared_selected,
                     mapping_offset=mapping_offset,mapping_ppm=mapping_ppm,reference_end=reference_end,
                     two=two,utc_uncertainties=utc_uncertainties) if later else archive
    if not angle:
        vector=dict(vector); vector['selection']=deepcopy(vector['selection'])
        profile=__import__('backend.waveforms.evidence_contracts',fromlist=['thaw']).thaw(vector['profile'].body)
        for key in ('required_quantities','min_rates','ranges'):
            profile[key]=[item for item in profile[key] if item['role']!='crank_angle']
        profile=case.approved_evidence('profile',profile)
        vector['selection']['profile']=dict(id=str(profile.id),content_sha256=profile.content_sha256,version=str(profile.version))
        vector['selection']['channels']=[c for c in vector['selection']['channels'] if c['role']!='crank_angle']
    pairs=PairService(lambda:case.connect('operator'),limits=LIMITS,shared_accounting_bucket='GW',read_connect=lambda:case.connect('reader'))
    body=dict(selection=vector['selection'],method_version='linear_adjacent/1')
    cmd=Command(CREATOR,uuid4(),0,'Real inversion synthetic pair','0'*64)
    pair=pairs.propose(body,replace(cmd,request_hash=pair_request_hash('propose',None,cmd,body=body)))
    cmd=Command(REVIEWER,uuid4(),1,'Independent pair approval','0'*64)
    pairs.review(pair.id,decision='approve',command=replace(cmd,request_hash=pair_request_hash('review',pair.id,cmd,decision='approve')))
    service,draft=DatasetCase.submitted(case,pair)
    body=dict(target_id=str(draft.id)); frozen=service.freeze(body,case.command('freeze',draft.id,draft.revision,REVIEWER,body=body))
    assembly=case.create_assembly()[0].subject_id
    parameters=case.body(assembly)
    parameters['provenance']['operating_conditions'][0].update(
        valid_from='2025-01-01T00:00:00.000000Z',valid_to='2028-01-01T00:00:00.000000Z')
    if parameter_end: parameters['valid_to']=parameter_end
    if condition_end: parameters['provenance']['operating_conditions'][0]['valid_to']=condition_end
    # Replace every provenance pin with actual archived immutable channel identity.
    for binding in parameters['provenance']['channel_bindings']:
        chosen=next((c for c in archive['selection']['channels'] if c['role']==binding['role']),None)
        if chosen is None: continue  # Missing-angle case must fail the frozen-selection gate.
        capture=next(c for c in archive['selection']['captures'] if c['key']==chosen['capture'])
        binding['channel_pin']={**chosen['capture'],'stream_id':chosen['stream_id'],'channel_id':chosen['channel_id'],
            'manifest_sha256':capture['manifest_sha256'],'first_index':int(chosen['first_index']),'last_index':int(chosen['last_index']),
            'sampling_pin':dict(id=chosen['sampling_config']['id'],content_sha256=chosen['sampling_config']['content_sha256'],review_version=int(chosen['sampling_config']['version'])),
            'sync_pin':dict(id=str(archive['sync'].id),content_sha256=archive['sync'].content_sha256,review_version=archive['sync'].version),
            'calibration_pin':dict(id=chosen['calibration']['id'],content_sha256=chosen['calibration']['content_sha256'],review_version=int(chosen['calibration']['version']))}
        if not archive_sync: binding['channel_pin'].pop('sync_pin')
        if producer: binding['channel_pin']['producer_endpoint']=dict(gateway_id='GW',device_id=producer)
    parameters['phase']['channel_pin']=deepcopy(parameters['provenance']['channel_bindings'][1]['channel_pin'])
    parameters['phase']['unwrap_kind']='unwrapped'
    for key,lo,hi in (('power_W','-100','100'),('speed_rad_s','0.1','10'),('acceleration_rad_s2','-10','10'),
                      ('bandwidth_Hz','1','1000'),('window_ns','1','10000000'),('load_N','0','100000')):
        parameters['domain'][key]=dict(min=lo,max=hi)
    parameters['drive']['loss_grid'].update(power_W=['-100','100'],motor_speed_rad_s=['1','100'])
    policy=parameters['policy']['body']; policy['motion_max_time_span_ns']='1000000000'; policy['motion_max_angle_span_rad']='0.5'
    parameters['policy']['content_sha256']=sha256(canonical_bytes(policy)).hexdigest()
    coverage=parameters['provenance']['coverage']; coverage['policy_sha256']=parameters['policy']['content_sha256']
    coverage['max_time_alignment_error_ns']=str(alignment_limit)
    coverage['domain_sha256']=sha256(canonical_bytes(parameters['domain'])).hexdigest()
    frame=archive['frames'][-1]; declaration=frame.body['cycle']
    stream=next(s for s in frame.streams if s.stream_id=='sensor')
    identities=[dict(source=dict(stream.source),stream_id='sensor',channel_id='displacement',index=e['sample_index']) for e in declaration['reference_events']]
    anchor=datetime_ns(utc(frame.body['utc_start']))
    parameters['provenance']['calibration_cycles']=[dict(start_ns=str(anchor+17),end_ns=str(anchor+2000000017),
        physical_cycle_id=sha256(canonical_bytes(identities)).hexdigest())]
    approved=case.approved_parameter(assembly,parameters)
    case.parameters.assign(dict(assembly_id=str(assembly),well_id='test:synthetic-inversion',valid_from='2026-01-01T00:00:00.000000Z',
        valid_to=None,close_assignment_id=None),command())
    models=ModelService(case.inversion_fixture.connect,read_connect=lambda:case.inversion_fixture.connect('reader'),authorize=case.authorize)
    model=models.register('beam_energy_balance/1',command()); models.review(model.subject_id,'approve',command(INV_REVIEWER,1)); model=models.get(model.subject_id)
    selection=SelectionService(case.inversion_fixture.connect,read_connect=lambda:case.inversion_fixture.connect('reader'),
                              authorize=case.authorize,parameters=case.parameters,models=models)
    selection.select(dict(assembly_id=str(assembly),parameter_id=str(approved.id),parameter_sha256=approved.content_sha256,
        parameter_review_version=approved.review_version,model_id=str(model.id),model_sha256=model.content_sha256,model_review_version=model.review_version),command())
    request=dict(dataset_version_id=str(frozen.id),dataset_sha256=frozen.content_sha256,member_ordinal=0,pair_id=str(pair.id),
        pair_sha256=pair.content_sha256,pair_review_version=2,assembly_id=str(assembly),parameter_id=str(approved.id),
        parameter_sha256=approved.content_sha256,parameter_review_version=approved.review_version,model_id=str(model.id),
        model_sha256=model.content_sha256,model_review_version=model.review_version,selection_version=1,operating_condition_ids=['condition-1'])
    exporter=MemberResearchExporter(lambda:case.connect('operator'),limits=LIMITS,shared_accounting_bucket='GW',read_connect=lambda:case.connect('reader'))
    return dict(request=request,vector=vector,archive=archive,exporter=exporter,parameters=approved)
