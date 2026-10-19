"""Draft-only binding of independent model values to actual archive evidence.

This module does not approve parameters. The fixed requested broad domain fails
the independent budget gate; syntactic validity must never be sold as validation.
"""
from copy import deepcopy
from decimal import Decimal
from fractions import Fraction
from hashlib import sha256

from backend.inversion.contracts import canonical_bytes
from backend.inversion.parameter_contracts import parse_parameter_set
from backend.inversion.series import ChannelPin, CycleWindow, PredictionInputs, Sample, SampleSeries


def number(value):
    value = Decimal(str(value))
    if not value.is_finite():
        raise ValueError('nonfinite model value')
    return format(value.normalize(), 'f')


def policy_body():
    return {'schema': 'inversion.algorithm-policy/1', 'algorithm': 'beam_energy_balance/1',
        'model_version': 'beam_energy_balance/1', 'numerical_margin': '0.001',
        'motion_max_time_span_ns': '250000000', 'motion_max_angle_span_rad': '0.5',
        'motion_max_residual_rad': '0.0001', 'motion_max_condition': '10000',
        'max_load_error_N': '1000', 'min_coverage_fraction': '0.8',
        'reference_max_gap_ns': '20000000', 'fill_max_time_span_ns': '20000000',
        'fill_max_angle_span_rad': '0.2', 'relative_error_min_denominator_N': '1'}


def bind_inversion_parameters(model: dict, pins: dict) -> bytes:
    """Produce a parseable draft; callers must gate *before* submit/review."""
    pins = deepcopy(pins)
    def attachment(purpose):
        return {**pins['attachment'], 'purpose': purpose,
            'source_description': 'Software report; not instrument certification; section ' + purpose}
    def evidence(purpose):
        return [attachment(purpose)]
    def values(group):
        return {key: value if type(value) is int and key in ('branch','rotation_sign','displacement_sign')
                else number(value) for key,value in model[group].items()}
    policy = policy_body()
    ranges = {'power_W': (-250000,250000), 'speed_rad_s': (.1,2),
        'acceleration_rad_s2': (-2,2), 'load_N': (0,100000), 'temperature_K': (250,350),
        'supply_voltage_V': (300,500), 'supply_frequency_Hz': (45,55),
        'window_ns': (1,10000000), 'bandwidth_Hz': (1,1000)}
    domain = {key: {'min': number(lo),'max': number(hi)} for key,(lo,hi) in ranges.items()}
    domain['evidence_refs'] = evidence('domain')
    uncertainty = {key: value for key,value in (
        ('torque_bound_Nm','100'), ('factor_bound_m','0.0001'), ('speed_bound_rad_s','0.00001'),
        ('numerical_budget_Nm','0.01'), ('numerical_factor_budget_m','0.0000001'))}
    uncertainty['evidence_refs'] = evidence('uncertainty')
    drive = {key: number(model['drive'][key]) for key in (
        'ratio','upstream_inertia_kg_m2','crank_friction_Nm','viscous_friction_Nm_s')}
    drive.update(supply_kind='direct_line_induction', evidence_refs=evidence('drive'),
        loss_grid={'power_W': ['-250000','250000'], 'motor_speed_rad_s': ['1','250'],
            'loss_W': [[number(model['drive']['loss_W'])]*2 for _ in range(2)],
            'error_W': [['0.02']*2 for _ in range(2)], 'evidence_refs': evidence('loss_grid')})
    body = {'schema':'inversion.parameter-set/1','assembly_id':pins['assembly_id'],
        'assembly_version':pins['assembly_version'],'source_kind':'synthetic',
        'valid_from':pins['valid_from'],'valid_to':pins['valid_to'],'parent_version':None,
        'geometry': values('geometry') | {'evidence_refs': evidence('geometry')},
        'bodies': values('bodies') | {'evidence_refs': evidence('bodies'),
            'component_ledger':[{'id':'motor','name':'Software upstream inertia','owner':'upstream'},
                                {'id':'crank','name':'Software crank and balance moment','owner':'crank'}]},
        'phase': {'channel_pin':pins['channels']['crank_angle'],'zero_reading_rad':'0',
            'encoder_direction':1,'unwrap_kind':'unwrapped','evidence_refs':evidence('phase')},
        'drive':drive, 'domain':domain, 'uncertainty':uncertainty,
        'policy':{'body':policy,'content_sha256':sha256(canonical_bytes(policy)).hexdigest()}}
    body['provenance'] = {'source_organization':'OWP software laboratory',
        'source_date':pins['valid_from'][:10],
        'notes':'Synthetic draft. Fixed broad-domain budget FAILED. Do not submit or approve. '
                'Same-cycle evaluation overlaps calibration; no independent field validation.',
        'attachment_pins': evidence('provenance'),
        'calibration_cycles':[{'start_ns':str(pins['cycle_start_ns']),
            'end_ns':str(pins['cycle_end_ns']),'physical_cycle_id':pins['physical_cycle_id']}],
        'operating_conditions':[{'id':'condition-1','valid_from':pins['valid_from'],'valid_to':pins['valid_to'],
            'temperature_K':{'min':'290','max':'310'},'supply_voltage_V':{'min':'380','max':'420'},
            'supply_frequency_Hz':{'min':'49','max':'51'},'source_description':'Synthetic fixed conditions',
            'attachment_pins':evidence('operating_conditions')}],
        'channel_bindings':[{'role':role,'source':'native_waveform','modbus':None,
            'channel_pin':pins['channels'][role],'installation_evidence_refs':evidence('installation')}
            for role in ('active_power','crank_angle','load','displacement')],
        'coverage':{'policy_sha256':body['policy']['content_sha256'],
            'domain_sha256':sha256(canonical_bytes(domain)).hexdigest(),
            'uncertainty_sha256':sha256(canonical_bytes(uncertainty)).hexdigest(),
            'max_time_alignment_error_ns':'0',
            'scopes':[{'scope':scope,'attachment_pin':attachment(scope),
                'basis':'UNAPPROVED: requested budget not validated over declared domain; see software gate.'}
                for scope in ('loss_calibration','time_alignment','averaged_power_mismatch',
                    'motion_angle_policy','upstream_dynamics','load_range','geometry_envelope','crank_friction')]}}
    raw = canonical_bytes(body)
    parse_parameter_set(raw)
    return raw


def prediction_inputs(cycle, channels):
    """Reference load/displacement are intentionally absent from this interface."""
    def series(role):
        channel = next(c for c in cycle.channels if c.role == role)
        values = tuple(Sample(Fraction(t),i,
            Fraction(raw)*Fraction(channel.scale)+Fraction(channel.offset) if q == 0 else None,q)
            for i,(t,raw,q) in enumerate(zip(cycle.time_ns,channel.raw,channel.quality)))
        return SampleSeries(role,values,ChannelPin.from_wire(channels[role]))
    return PredictionInputs(series('active_power'),series('crank_angle'),
        CycleWindow(Fraction(cycle.time_ns[0]),Fraction(cycle.time_ns[-1]),cycle.case_id),
        cycle.model['motion']['power_window_ns'],Fraction(0))


def software_budget(cycle):
    from .software_budget import inspect_budget
    return inspect_budget(cycle)
