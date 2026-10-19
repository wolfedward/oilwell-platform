"""Strict, bounded generator configuration; no runtime credentials."""
from dataclasses import asdict
from datetime import datetime
import re
from .contracts import PackConfig

MAX_BYTES = 268435456


def parse_config(value: dict) -> PackConfig:
    fields = set(asdict(PackConfig())) | {'schema'}
    if not isinstance(value, dict) or set(value) != fields:
        raise ValueError('configuration fields')
    if value['schema'] != 'owp.testpack.config/1' or value['profile'] not in ('smoke', 'extended'):
        raise ValueError('configuration schema/profile')
    for key, lower, upper in (('seed', 0, 2**32-1), ('max_bytes', 1, MAX_BYTES)):
        if type(value[key]) is not int or not lower <= value[key] <= upper:
            raise ValueError('configuration ' + key)
    start = value['start_utc']
    if not isinstance(start, str) or not re.fullmatch(r'\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{6}Z', start):
        raise ValueError('start_utc must be explicit UTC with microseconds')
    datetime.fromisoformat(start)
    if value['timezone'] != 'Asia/Shanghai':
        raise ValueError('unsupported business timezone')
    return PackConfig(**{key: value[key] for key in fields-{'schema'}})


def config_dict(config: PackConfig) -> dict:
    value = {'schema': 'owp.testpack.config/1', **asdict(config)}
    parse_config(value)
    return value
