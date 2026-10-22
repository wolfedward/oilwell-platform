"""Only the two preregistered finite-input configurations are accepted."""
from dataclasses import asdict, dataclass

from backend.inversion.synthetic_registry import installed_config
from .contracts import PackConfig


@dataclass(frozen=True)
class PackConfigV2(PackConfig):
    seed: int = 20261022
    generator_version: str = 'finite-input/2'


def parse_config_v2(body: dict) -> PackConfigV2:
    if type(body) is not dict: raise ValueError('Invalid v2 configuration')
    expected=installed_config(body.get('profile'))
    if set(body)!=set(expected) or any(type(body[k]) is not type(v) or body[k]!=v for k,v in expected.items()):
        raise ValueError('Configuration is not preregistered')
    return PackConfigV2(**{k:v for k,v in body.items() if k!='schema'})


def config_dict_v2(config: PackConfigV2):
    if type(config) is not PackConfigV2: raise ValueError('Explicit v2 configuration required')
    body={'schema':'owp.testpack.config/2',**asdict(config)}
    parse_config_v2(body)
    return body
