"""Fixed run identifiers and server-created immutable input packages."""
from dataclasses import dataclass
from hashlib import sha256
from uuid import UUID
from .contracts import strict_json,fields,identity,digest,integer,name,fail,canonical_bytes,freeze
from .series import PredictionInputs,ReferenceInputs
from .parameter_contracts import ParameterSet
from .policy import AlgorithmPolicy


@dataclass(frozen=True)
class RunRequest:
    dataset_version_id: UUID
    dataset_sha256: str
    member_ordinal: int
    pair_id: UUID
    pair_sha256: str
    pair_review_version: int
    assembly_id: UUID
    parameter_id: UUID
    parameter_sha256: str
    parameter_review_version: int
    model_id: UUID
    model_sha256: str
    model_review_version: int
    selection_version: int
    operating_condition_ids: tuple[str,...]

    def __post_init__(self):
        for key in self.__dataclass_fields__:
            value=getattr(self,key)
            if key.endswith('_id'):
                if type(value) is not UUID: fail()
            elif key.endswith('_sha256'): digest(value)
            elif key=='member_ordinal': integer(value,maximum=999)
            elif key=='operating_condition_ids':
                if type(value) is not tuple or not 1<=len(value)<=64: fail()
                for item in value: name(item)
                if len(set(value))!=len(value): fail()
            else: integer(value,minimum=1)


def parse_run_request(raw: bytes) -> RunRequest:
    body=fields(strict_json(raw),RunRequest.__dataclass_fields__)
    values={}
    for key,value in body.items():
        if key.endswith('_id'): values[key]=identity(value)
        elif key.endswith('_sha256'): values[key]=digest(value)
        elif key=='member_ordinal': values[key]=integer(value,maximum=999)
        elif key=='operating_condition_ids':
            if type(value) is not list or not 1<=len(value)<=64: fail()
            values[key]=tuple(name(item) for item in value)
            if len(set(value))!=len(value): fail()
        else: values[key]=integer(value,minimum=1)
    return RunRequest(**values)


@dataclass(frozen=True)
class InputSnapshot:
    body: object
    canonical: bytes
    content_sha256: str

    def __post_init__(self):
        if (type(self.canonical) is not bytes or len(self.canonical)>67108864
            or canonical_bytes(self.body)!=self.canonical
            or sha256(self.canonical).hexdigest()!=self.content_sha256): fail()
        import json
        object.__setattr__(self,'body',freeze(json.loads(self.canonical)))


@dataclass(frozen=True)
class PreparedInput:
    prediction_inputs: PredictionInputs
    reference_inputs: ReferenceInputs
    parameters: ParameterSet
    policy: AlgorithmPolicy
    snapshot: InputSnapshot
