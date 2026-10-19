"""Shared immutable records. Files are validated at the storage boundary."""
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class PackConfig:
    profile: str = 'smoke'
    seed: int = 20261019
    start_utc: str = '2026-09-30T16:00:00.000000Z'
    timezone: str = 'Asia/Shanghai'
    max_bytes: int = 268435456


@dataclass(frozen=True)
class Artifact:
    path: str
    media_type: str
    content: bytes


@dataclass(frozen=True)
class Case:
    case_id: str
    category: str
    artifacts: tuple[Artifact, ...]
    facts: dict
    expected: tuple[dict, ...]


@dataclass(frozen=True)
class Channel:
    role: str
    unit: str
    scale: str
    offset: str
    raw: tuple[int, ...]
    quality: tuple[int, ...]


@dataclass(frozen=True)
class Cycle:
    case_id: str
    sample_rate_hz: int
    time_ns: tuple[int, ...]
    channels: tuple[Channel, ...]
    turn_index: int
    model: dict
    diagnostics: dict


@dataclass(frozen=True)
class EvidenceBinding:
    sampling: dict
    calibration: dict
    origin_id: str | None
    sync_id: str | None
    reported_configs: dict | None = None


@dataclass(frozen=True)
class PackIndex:
    root: Path
    manifest: dict


@dataclass(frozen=True)
class CheckResult:
    check_id: str
    layer: str
    status: str
    expected: dict
    actual: dict
    artifacts: tuple[str, ...] = ()


@dataclass(frozen=True)
class LabHandle:
    root: Path
    lab_id: str
    monitoring_root: Path
    review_root: Path
    ports: dict
