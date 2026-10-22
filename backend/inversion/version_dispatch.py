"""Schema dispatch preserves original types/bytes; unknown versions fail closed."""
from .contracts import strict_json, fail
from .parameter_contracts import ParameterSet, parse_parameter_set
from .parameter_contracts_v2 import ParameterSetV2, parse_parameter_set_v2


def parse_parameters(raw: bytes, *, draft: bool = False) -> ParameterSet | ParameterSetV2:
    body = strict_json(raw)
    if type(body) is not dict: fail()
    schema = body.get('schema')
    if schema == 'inversion.parameter-set/1': return parse_parameter_set(raw, draft=draft)
    if schema == 'inversion.parameter-set/2': return parse_parameter_set_v2(raw, draft=draft)
    fail()


def missing_parameters(value: ParameterSet | ParameterSetV2) -> tuple[str, ...]:
    if type(value) not in (ParameterSet, ParameterSetV2): fail()
    return value.missing
