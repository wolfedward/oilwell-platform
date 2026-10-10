"""Bounded, explicitly opted-in historical metadata; never samples or evidence."""
from collections.abc import Mapping
from hashlib import sha256
import json
import re
from .contracts import canonical_bytes
from .input_contracts import parse_run_request
from .transactions import InversionError

ROLES = ('active_power', 'crank_angle', 'load', 'displacement')
MAX_METADATA_BYTES = 128 * 1024
ABSOLUTE_TERMS = ('endpoint_bound_ns', 'reference_bound_ns', 'reference_additional_bound_ns',
    'reference_utc_uncertainty_ns', 'utc_uncertainty_ns', 'calibration_residual_ns', 'total_bound_ns')


def refuse(code='input_metadata_invalid'):
    raise InversionError(code, status=409)


def mapping(value):
    if not isinstance(value, Mapping): refuse()
    return value


def scalar(value, maximum=4096):
    if value is None: return None
    if type(value) not in (str, int, bool): refuse()
    if len(str(value)) > maximum: refuse()
    return value


def fields(value, names, maximum=4096):
    source = mapping(value)
    return {key: scalar(source.get(key), maximum) for key in names}


def exact(value):
    if value is None: return None
    if type(value) not in (str, int) or len(str(value)) > 256: refuse()
    text = str(value)
    if not re.fullmatch(r'-?\d+(?:/\d+|\.\d+(?:[eE][+-]?\d+)?|[eE][+-]?\d+)?', text): refuse()
    if '/' in text and int(text.split('/')[1]) == 0: refuse()
    return text


def bounded_rows(value, maximum):
    if not isinstance(value, (list, tuple)) or len(value) > maximum: refuse()
    return value


def mapping_rows(value, maximum):
    return [mapping(row) for row in bounded_rows(value, maximum)]


def endpoint(value):
    return fields(value, ('gateway_id', 'device_id'), 128)


def capture(value):
    return fields(value, ('gateway_id', 'device_id', 'capture_id'), 128)


def evidence(value):
    if value is None: return None
    result = fields(value, ('id', 'content_sha256', 'review_version'), 128)
    if not isinstance(result['content_sha256'], str) or not re.fullmatch('[0-9a-f]{64}', result['content_sha256']): refuse()
    return result


def observation_metadata(value):
    source = mapping(value)
    result = fields(source, ('utc_start', 'physical_cycle_key'))
    for key in ('start_ns', 'end_ns', 'relative_start_ns', 'relative_end_ns', 'uncertainty_ns', 'relative_alignment_ns'):
        result[key] = exact(source.get(key))
    relative = source.get('relative_timing_terms')
    result['relative_timing_terms'] = None
    if relative is not None:
        relative = mapping(relative)
        terms = {key: exact(relative.get(key)) for key in ('mapping_and_calibration_ns', 'noncommon_capture_utc_ns', 'total_bound_ns')}
        rows = mapping_rows(relative.get('capture_utc_bounds'), 2)
        terms['capture_utc_bounds'] = [dict(capture=capture(row['capture']), utc_uncertainty_ns=exact(row.get('utc_uncertainty_ns'))) for row in rows]
        identities = [canonical_bytes(row['capture']) for row in terms['capture_utc_bounds']]
        if not rows or len(set(identities)) != len(identities): refuse()
        result['relative_timing_terms'] = terms
    absolute = source.get('absolute_timing_terms')
    result['absolute_timing_terms'] = None
    if absolute is not None:
        rows = mapping_rows(absolute, 4)
        if len(rows) != 4 or {row.get('role') for row in rows} != set(ROLES): refuse()
        result['absolute_timing_terms'] = [dict(role=row['role'], **{key: exact(row.get(key)) for key in ABSOLUTE_TERMS}) for row in rows]
    return result


def project_input_metadata(body, *, result_id, content_sha256, snapshot_sha256, request_sha256):
    """Caller supplies the SAME hash-verified StoredResult snapshot and identities."""
    try:
        body = mapping(body)
        request = json.loads(canonical_bytes(body['request']))
        parse_run_request(canonical_bytes(request))
        if sha256(canonical_bytes(request)).hexdigest() != request_sha256: refuse()
        applicability = mapping(body['applicability'])
        parameter = mapping(applicability['parameter_body'])
        provenance = mapping(parameter['provenance'])
        bindings = mapping_rows(provenance['channel_bindings'], 4)
        if len(bindings) != 4 or {row.get('role') for row in bindings} != set(ROLES): refuse()
        sources = {row['role']: row['source'] for row in bindings}
        pins = mapping(body['channel_pins'])
        if set(pins) != set(ROLES): refuse()
        channels = {}
        captures = set()
        for role in ROLES:
            pin = mapping(pins[role])
            if sources[role] not in ('native_waveform', 'modbus'): refuse()
            row = fields(pin, ('gateway_id', 'device_id', 'capture_id', 'stream_id', 'channel_id'), 128)
            digest = scalar(pin['manifest_sha256'], 64)
            if not isinstance(digest,str) or not re.fullmatch('[0-9a-f]{64}',digest): refuse()
            first, last = pin['first_index'], pin['last_index']
            if type(first) is not int or type(last) is not int or not 0 <= first <= last or last-first >= 32768: refuse()
            row.update(manifest_sha256=digest, first_index=exact(first), last_index=exact(last), source=sources[role],
                producer_endpoint=endpoint(pin['producer_endpoint']) if pin.get('producer_endpoint') is not None else None,
                physical_endpoint=endpoint(pin.get('producer_endpoint') or pin))
            for key in ('calibration_pin','sampling_pin','sync_pin'): row[key] = evidence(pin.get(key))
            channels[role] = row
            captures.add((row['gateway_id'],row['device_id'],row['capture_id']))
        if len(captures) > 2: refuse()
        selected = mapping(applicability['selection'])
        selected_body = mapping(selected['body'])
        if selected['version'] != request['selection_version']: refuse()
        for key in ('assembly_id','parameter_id','parameter_sha256','parameter_review_version','model_id','model_sha256','model_review_version'):
            if selected_body.get(key) != request[key]: refuse()
        conditions = mapping_rows(applicability['operating_conditions'], 64)
        if len(conditions) != len(request['operating_condition_ids']) or {row['id'] for row in conditions} != set(request['operating_condition_ids']): refuse()
        approved = []
        for item in conditions:
            row = fields(item, ('id','valid_from','valid_to'), 128)
            for key in ('temperature_K','supply_voltage_V','supply_frequency_Hz'):
                bound = mapping(item[key])
                row[key] = {name:exact(bound.get(name)) for name in ('min','max')}
            approved.append(row)
        observation = observation_metadata(body['observation'])
        if observation['relative_timing_terms'] is not None:
            actual = {tuple(row['capture'][key] for key in ('gateway_id','device_id','capture_id'))
                for row in observation['relative_timing_terms']['capture_utc_bounds']}
            if actual != captures: refuse()
        registry = mapping(applicability.get('registry', {}))
        well = mapping(registry.get('well', {}))
        result = dict(result_id=result_id,content_sha256=content_sha256,snapshot_sha256=snapshot_sha256,
            hash_scope='original_full_artifacts', request=request, observation=observation,
            channel_pins=channels, applicability=dict(
                assignment=fields(applicability['assignment'],('id','well_id','valid_from','valid_to','version','content_sha256'),128),
                mechanical=fields(applicability['mechanical'],('id','revision','valid_from','valid_to'),128),
                selection=dict(id=scalar(selected['id'],128),version=selected['version'],rollback_selection_id=scalar(selected_body.get('rollback_selection_id'),128)),
                well_id=scalar(well.get('id'),128),
                operating_conditions=approved))
        if len(canonical_bytes(result)) > MAX_METADATA_BYTES: refuse('input_metadata_size_limit')
        # Defense in depth only AFTER construction of the finite nested allowlist.
        from .exports import public_value
        return public_value(result)
    except (KeyError, TypeError, ValueError, OverflowError) as error:
        raise InversionError('input_metadata_invalid',status=409) from error
