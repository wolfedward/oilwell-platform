"""Read a digest-bound historical public export; never run or approve a model."""
from datetime import datetime
from fractions import Fraction
from hashlib import sha256
from pathlib import Path
import re

from backend.inversion.result_codec import MAX_RESULT
from backend.inversion.result_codec_v2 import KINDS
from .report_inversion_v2 import validate
from .storage import safe_path, strict_json


def _simple(value, depth=0):
    if depth > 4:
        raise ValueError('retained row nesting exceeds limit')
    if value is None or type(value) is bool:
        return
    if type(value) is str and len(value) <= 2500:
        return
    if type(value) is int and abs(value) < 2**53:
        return
    if type(value) is list and len(value) <= 128:
        for item in value:
            _simple(item, depth+1)
        return
    raise ValueError('unsupported retained row value')


def _number(value):
    # Bound decimal exponent syntax BEFORE Fraction can allocate a huge integer.
    if (type(value) is not str or len(value) > 2500 or not re.fullmatch(
            r'-?[0-9]+(?:/[0-9]+|(?:\.[0-9]+)?(?:[eE][+-]?[0-9]{1,4})?)', value)):
        raise ValueError('retained numeric string required')
    try:
        number = Fraction(value)
    except (ValueError, ZeroDivisionError, OverflowError):
        raise ValueError('invalid retained number') from None
    if max(number.numerator.bit_length(), number.denominator.bit_length()) > 16384:
        raise ValueError('retained number exceeds limit')
    return number


def _points(blocks, count):
    if type(blocks) is not list or not 1 <= len(blocks) <= 256:
        raise ValueError('retained chunks missing or excessive')
    output = {kind: [] for kind in KINDS}
    for block in blocks:
        if type(block) is not dict or set(block) != {'kind', 'columns', 'rows'}:
            raise ValueError('retained chunk shape')
        kind = block['kind']
        if type(kind) is not str or kind not in KINDS or block['columns'] != list(KINDS[kind]):
            raise ValueError('retained columns differ')
        if type(block['rows']) is not list or not block['rows']:
            raise ValueError('empty retained chunk')
        for values in block['rows']:
            if type(values) is not list or len(values) != len(KINDS[kind]):
                raise ValueError('retained row shape')
            for value in values:
                _simple(value)
            point = dict(zip(KINDS[kind], values))
            if len(output[kind]) >= count:
                raise ValueError('retained point count exceeded')
            _number(point['time_ns'])
            if kind in ('prediction', 'proof', 'comparison'):
                if point['power_index'] != str(len(output[kind])):
                    raise ValueError('retained native index missing or duplicated')
                if output[kind] and _number(point['time_ns']) <= _number(output[kind][-1]['time_ns']):
                    raise ValueError('retained native time is not increasing')
            if kind == 'prediction' and point['status'] == 'valid':
                _number(point['s_m']); _number(point['load_N'])
            output[kind].append(point)
    if any(len(output[kind]) != count for kind in ('prediction', 'proof', 'comparison')):
        raise ValueError('retained native series truncated')
    for prediction, proof, reference in zip(output['prediction'], output['proof'], output['comparison']):
        if prediction['time_ns'] != proof['time_ns'] or prediction['time_ns'] != reference['time_ns']:
            raise ValueError('retained native time binding differs')
    return output


def load_retained_result(summary, results_root: Path, case_id: str):
    """Return captured public rows, or None for a non-approved summary.

    Report and export hashes are provenance, not signatures or current DB rights.
    The owner-selected root has the same non-hostile-owner contract as safe_path.
    """
    validate(summary)
    if (type(case_id) is not str or not re.fullmatch(r'[a-z0-9][a-z0-9_.-]{0,127}', case_id)
            or '..' in case_id or summary['case_id'] != case_id):
        raise ValueError('retained case identity differs')
    if summary['approved_execution_status'] != 'passed':
        return None
    root = safe_path(Path(results_root))
    if not root.is_dir():
        raise ValueError('retained result directory required')
    path = safe_path(root/(case_id+'-export.json'))
    if not path.is_file() or not 0 < path.stat().st_size <= MAX_RESULT:
        raise ValueError('retained export exceeds size limit')
    with path.open('rb') as stream:
        raw = stream.read(MAX_RESULT+1)
    if len(raw) > MAX_RESULT or sha256(raw).hexdigest() != summary['digests']['export_sha256']:
        raise ValueError('retained export digest differs')
    value = strict_json(raw)
    try:
        body, historical = value['result'], value['historical']
        if (value['schema'] != 'inversion.export/1' or value['id'] != summary['record_ids']['export_id']
                or value['result_id'] != summary['record_ids']['result_id']
                or value['result_sha256'] != summary['digests']['result_sha256']
                or value['format'] != 'json' or value['intent'] != 'quantitative'
                or value['restricted'] is not False or value['restriction_reasons'] != []
                or value['current_review']['state'] != 'approved'
                or historical['review']['state'] != 'approved' or historical['execution'] != 'completed'
                or historical['source'] != 'synthetic'
                or historical['current_applicability']['state'] != 'applicable'
                or historical['current_applicability']['reasons'] != []
                or historical['field_validation']['certified'] is not False
                or historical['field_validation']['independent_comparison'] is not False):
            raise ValueError('retained approved export identity or state differs')
        if (body['schema'] != 'inversion.result/2' or type(body['point_count']) is not int or body['point_count'] != 1025
                or body['source_kind'] != 'synthetic' or body['field_accuracy_certified'] is not False
                or body['external_evidence_admission_required'] is not True
                or body['prediction_status'] != 'applicable'
                or body['source_proof']['status'] != 'verified'
                or body['source_proof']['case_id'] != case_id):
            raise ValueError('retained result is not the approved synthetic case')
        checked = datetime.fromisoformat(value['checked_at'].replace('Z', '+00:00'))
        if checked.tzinfo is None:
            raise ValueError('retained export timestamp must be aware')
        points = _points(value['chunks'], 1025)
        # Fixed public projection: never return arbitrary metadata or nested keys.
        display_body = {key: body[key] for key in ('schema', 'prediction_status',
            'prediction_reason_codes', 'parameter_core_sha256')}
        display_body['source_proof'] = {key: body['source_proof'][key]
            for key in ('status', 'reason_codes', 'case_id', 'config_sha256')}
    except (KeyError, TypeError, AttributeError, OverflowError) as error:
        raise ValueError('retained export structure invalid') from error
    return dict(prediction=points['prediction'], reference=points['comparison'], proof=points['proof'],
        body=display_body, provenance=dict(scope='historical_export_only', case_id=case_id,
            result_id=value['result_id'], export_id=value['id'], result_sha256=value['result_sha256'],
            export_sha256=summary['digests']['export_sha256'], checked_at=value['checked_at'],
            current_authorization_checked=False, field_accuracy_certified=False))
