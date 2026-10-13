"""Bounded query inputs and precision-preserving export values."""
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import re
from urllib.parse import parse_qsl
from uuid import UUID

from .errors import MonitoringError, invalid

CONTEXT = {'revision', 'received_before', 'query_total'}
PAGING = CONTEXT | {'page', 'page_size'}
WINDOW = {'from_time', 'to_time'}
IDENTITY = {'gateway_id', 'device_id'}
FILTERS = {
    'well': PAGING | {'binding_scope'},
    'latest': PAGING | {'registry_device_id', 'point_code'},
    'history': PAGING | WINDOW | {'registry_device_id', 'point_code', 'quality'},
    'history_csv': CONTEXT | WINDOW | {'registry_device_id', 'point_code', 'quality'},
    'wells_csv': CONTEXT | {'area_code', 'well_code', 'status'},
    'messages': PAGING | WINDOW | IDENTITY,
    'message': PAGING,
    'quality': PAGING | WINDOW | IDENTITY | {'view', 'quality', 'reason_code', 'state', 'well_id', 'point_code', 'unresolved'},
}


def require_permissions(resource, filters, permissions):
    required = {'registry.read'}
    if resource in {'latest', 'history', 'history_csv'}: required.add('telemetry.read')
    if not required <= permissions:
        raise MonitoringError('monitoring.forbidden', '当前账号没有此项查询权限', 403)


@dataclass(frozen=True)
class CsvExport:
    filename: str
    body: bytes


def timestamp(value, field):
    try:
        value = datetime.fromisoformat(value.replace('Z', '+00:00')) if isinstance(value, str) else value
        if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
            raise ValueError()
        return value.astimezone(timezone.utc)
    except (ValueError, TypeError, OverflowError):
        raise invalid(field) from None


def parse_filters(resource: str, query_string: str) -> dict[str, str]:
    try:
        if resource not in FILTERS or len(query_string) > 8192 or re.search(r'%(?![0-9a-fA-F]{2})', query_string):
            raise ValueError()
        pairs = parse_qsl(query_string, keep_blank_values=True, strict_parsing=True,
                          encoding='utf-8', errors='strict', max_num_fields=24)
        if len(dict(pairs)) != len(pairs) or any(key not in FILTERS[resource] for key, _ in pairs):
            raise ValueError()
        return dict(pairs)
    except (ValueError, UnicodeError):
        raise invalid('query') from None


def decimal(value, field, maximum, *, zero=False):
    if type(value) is int:
        value = str(value)
    if not isinstance(value, str) or not re.fullmatch(r'0|[1-9][0-9]{0,18}', value):
        raise invalid(field)
    if not (0 if zero else 1) <= int(value) <= maximum:
        raise invalid(field)
    return value


def validate_filters(resource: str, supplied: dict[str, str]) -> dict:
    if resource not in FILTERS or not isinstance(supplied, dict):
        raise invalid('resource')
    internal = {'well_id'} if resource in {'well', 'latest', 'history', 'history_csv'} else {'id'} if resource == 'message' else set()
    if set(supplied) - (FILTERS[resource] | internal):
        raise invalid('query')
    result = dict(supplied)
    required = internal | ({'registry_device_id', 'point_code', 'from_time', 'to_time'} if resource in {'history', 'history_csv'} else set())
    if not required <= result.keys():
        raise invalid('/'.join(sorted(required - result.keys())))
    for key in ('well_id', 'registry_device_id'):
        if key in result:
            try:
                result[key] = str(UUID(result[key]))
            except (ValueError, TypeError, AttributeError):
                raise invalid(key) from None
    for key in ('gateway_id', 'device_id', 'point_code', 'area_code', 'well_code', 'reason_code', 'id'):
        if key in result and (not isinstance(result[key], str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}', result[key])):
            raise invalid(key)
    for key, choices in {'binding_scope': ('current', 'history'), 'quality': ('good', 'uncertain', 'bad'),
                          'view': ('observations', 'resolution', 'cases'), 'state': ('open', 'acknowledged', 'closed'),
                          'status': ('active', 'archived'), 'unresolved': ('true', 'false')}.items():
        if key in result and result[key] not in choices:
            raise invalid(key)
    if resource == 'quality':
        result.setdefault('view', 'observations')
        if result['view'] != 'cases' and 'state' in result or result['view'] == 'cases' and 'quality' in result:
            raise invalid('view')
    for key in WINDOW | {'received_before'}:
        if key in result:
            result[key] = timestamp(result[key], key)
    if WINDOW <= result.keys() and (result['to_time'] < result['from_time'] or result['to_time'] - result['from_time'] > timedelta(days=31)):
        raise invalid('from_time/to_time')
    for key in ('revision', 'query_total'):
        if key in result:
            result[key] = decimal(result[key], key, 9223372036854775807, zero=key == 'query_total')
    if 'page' in FILTERS[resource]:
        result['page'] = int(decimal(result.get('page', '1'), 'page', 100001))
        result['page_size'] = int(decimal(result.get('page_size', '32' if resource == 'latest' else '20'),
                                           'page_size', 32 if resource == 'latest' else 200))
        result['offset'] = (result['page'] - 1) * result['page_size']
        if result['offset'] > 100000 or result['page'] > 1 and not CONTEXT <= result.keys():
            raise invalid('page/context')
    if resource.endswith('_csv') and not CONTEXT <= result.keys():
        raise invalid('export/context')
    return result
