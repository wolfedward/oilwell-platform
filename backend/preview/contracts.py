"""Shared preview contracts; these describe software fixtures, not field evidence."""
from typing import TypedDict, Literal


class PageSpec(TypedDict):
    id: str
    label: str
    group: str
    origin: Literal['demo', 'development', 'new']
    hidden: bool
    annual_tasks: list[str]
    content_version: str
    scenarios: list[str]


class PreviewMeta(TypedDict):
    mode: Literal['preview']
    control_enabled: Literal[False]
    snapshot_id: str
    snapshot_at: str
    data_from: str | None
    data_to: str | None
    source_kind: Literal['software_fixture']
    page_catalog_version: str


class PreviewActor(TypedDict):
    id: str
    display_name: str
    role_code: str
    permissions: list[str]


PreviewQuery = dict[str, str]
