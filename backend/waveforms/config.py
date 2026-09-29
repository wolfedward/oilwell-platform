"""Explicit immutable capacity budgets. Importing this module performs no I/O."""
from dataclasses import dataclass, fields, field
import os
from pathlib import Path
import tomllib
from types import MappingProxyType
from typing import Mapping
from uuid import UUID
from .contracts import MAX_I64, valid_name, valid_prefix
from .errors import WaveformError


@dataclass(frozen=True)
class Quota:
    raw_bytes: int
    facts: int
    attachment_bytes: int
    incomplete_captures: int
    orphan_bytes: int
    quarantine_bytes: int
    metadata_rows: int
    orphan_bytes_per_capture: int

    def __post_init__(self):
        for field in fields(self):
            value = getattr(self, field.name)
            if type(value) is not int or not 1 <= value <= MAX_I64:
                raise ValueError("quota values must be explicit positive signed 64-bit integers")
        if self.quarantine_bytes > self.raw_bytes or self.orphan_bytes > self.raw_bytes:
            raise ValueError("quarantine and orphan limits must fit raw capacity")
        if self.orphan_bytes_per_capture > self.orphan_bytes:
            raise ValueError("per-capture orphan capacity exceeds its bucket")


@dataclass(frozen=True)
class CapacityLimits:
    global_quota: Quota
    gateways: Mapping[str, Quota]

    def __post_init__(self):
        if type(self.global_quota) is not Quota or not isinstance(self.gateways, Mapping) or not self.gateways:
            raise ValueError("explicit global and gateway capacities are required")
        gateway_copy = dict(self.gateways)
        if any(not valid_name(key) or type(value) is not Quota for key, value in gateway_copy.items()):
            raise ValueError("invalid configured gateway capacity")
        for field in fields(Quota):
            if field.name == "orphan_bytes_per_capture":
                if any(value.orphan_bytes_per_capture > self.global_quota.orphan_bytes_per_capture for value in gateway_copy.values()):
                    raise ValueError("per-capture orphan budget exceeds global maximum")
            elif sum(getattr(value, field.name) for value in gateway_copy.values()) > getattr(self.global_quota, field.name):
                raise ValueError("sum of gateway budgets exceeds global capacity")
        object.__setattr__(self, "gateways", MappingProxyType(gateway_copy))


COMPONENT_ROLES = {"receive": ("receiver",), "assemble": ("assembler",),
                   "publish": ("receipt_publisher",), "management": ("reader", "operator", "receiver"),
                   "sender": ()}
HEALTH_COMPONENTS = {"receive": "receiver", "assemble": "assembler", "publish": "receipt_publisher"}


@dataclass(frozen=True)
class GatewayConfig:
    gateway_id: str
    source_ref: UUID
    mqtt: object | None = field(repr=False)
    session_expiry_seconds: int | None

    @property
    def client_id(self):
        return self.mqtt.client_id if self.mqtt else None


@dataclass(frozen=True)
class SenderSpoolConfig:
    path: Path
    max_bytes: int


@dataclass(frozen=True)
class WaveformConfig:
    component: str
    topic_prefix: str
    limits: CapacityLimits
    gateways: tuple[GatewayConfig, ...]
    postgres: Mapping = field(repr=False)
    assembly_workers: int
    max_db_connections: int
    shared_accounting_bucket: str | None = None
    sender_spool: SenderSpoolConfig | None = None


def _integer(value, low, high):
    if type(value) is not int or not low <= value <= high:
        raise ValueError("invalid bounded integer")
    return value


def _mqtt(section, config_path):
    from backend.center.config import _secret
    from backend.gateway.mqtt_uplink import MqttConfig
    from backend.secret_store import read_windows_credential
    allowed = {"host", "port", "client_id", "username", "password_env", "password_credential",
               "ca_cert", "client_cert", "client_key", "keepalive_seconds", "connect_timeout_seconds",
               "session_expiry_seconds", "tls_insecure", "verify_certificate"}
    if set(section) - allowed:
        raise ValueError("unknown MQTT setting")
    if section.get("tls_insecure", False) is not False or section.get("verify_certificate", True) is not True:
        raise ValueError("TLS verification must remain enabled")
    paths = {}
    for key in ("ca_cert", "client_cert", "client_key"):
        if section.get(key):
            path = Path(section[key])
            paths[key] = str(path if path.is_absolute() else (config_path.parent / path).resolve())
    return MqttConfig(host=section["host"], port=_integer(section["port"], 1, 65535),
        client_id=section["client_id"], username=section["username"],
        password=_secret(section, os.environ, "mqtt", section["username"], read_windows_credential),
        keepalive_seconds=_integer(section["keepalive_seconds"], 10, 300),
        connect_timeout_seconds=_integer(section["connect_timeout_seconds"], 1, 30), **paths)


def load_waveform_config(path, *, component: str) -> WaveformConfig | None:
    """Resolve only selected credentials. All errors omit credential references.

    source_ref is an explicitly provisioned intake UUID, stable across process
    restarts. A deliberate provenance-version change gets a new configured UUID;
    this identifier never authenticates a device or physical measurement.
    """
    from backend.center.config import _postgres_settings
    from backend.secret_store import read_windows_credential
    try:
        if component not in COMPONENT_ROLES:
            raise ValueError("unknown component")
        config_path = Path(path).resolve()
        with config_path.open("rb") as handle:
            raw = tomllib.load(handle)
        general = raw["waveforms"]
        if type(general["enabled"]) is not bool:
            raise ValueError("explicit enable required")
        if not general["enabled"]:
            return None
        if not valid_prefix(general["topic_prefix"]):
            raise ValueError("invalid prefix")
        gateways = raw["gateways"]
        if not 1 <= len(gateways) <= 64:
            raise ValueError("invalid gateway count")
        sender_spool = None
        if component == 'sender':
            spool = raw['sender_spool']
            if len(gateways) != 1 or set(spool) != {'path', 'max_bytes'} or not isinstance(spool['path'], str) or not spool['path']:
                raise ValueError('single gateway and explicit local spool required')
            spool_path = Path(spool['path'])
            sender_spool = SenderSpoolConfig(spool_path if spool_path.is_absolute() else config_path.parent / spool_path,
                _integer(spool['max_bytes'], 1, MAX_I64))
        limits = CapacityLimits(Quota(**raw["capacity"]["global"]),
                                {key: Quota(**value["quota"]) for key, value in gateways.items()})
        workers = _integer(general["assembly_workers"], 1, 64) if component == "assemble" else 0
        connections = _integer(general["max_db_connections"], 1, 64) if component != "sender" else 0
        if connections < (len(gateways) if component == "receive" else 2 * workers):
            raise ValueError("insufficient connection allocation")
        # Validate declared identity metadata without touching unused secrets.
        users, endpoints = [], set()
        for role in ("migration_owner", "receiver", "assembler", "receipt_publisher", "reader", "operator"):
            section = raw.get("postgres_" + role)
            if section is not None:
                if not valid_name(section["user"]) or not valid_name(section["database"]):
                    raise ValueError("invalid database identity")
                users.append(section["user"])
                endpoints.add((section["host"].lower(), _integer(section["port"], 1, 65535), section["database"]))
        if len(users) != len(set(users)) or len(endpoints) > 1:
            raise ValueError("database identities must be distinct at one endpoint")
        clients, mqtt_users, sources, configured = set(), {}, set(), []
        for gateway_id, gateway in gateways.items():
            source = UUID(gateway["source_ref"])
            if source.int == 0 or source in sources:
                raise ValueError("distinct explicit source references required")
            sources.add(source)
            for mqtt_role in ("receive", "publish", "sender"):
                section = gateway.get("mqtt_" + mqtt_role)
                if section is None:
                    continue
                if not valid_name(section["client_id"]) or not valid_name(section["username"]):
                    raise ValueError("invalid MQTT identity")
                if section["client_id"] in clients:
                    raise ValueError("duplicate session identity")
                clients.add(section["client_id"])
                # Per-gateway accounts also make broker least-topic ACLs feasible.
                username = section["username"]
                if username in mqtt_users:
                    raise ValueError("MQTT accounts must be separated by gateway and role")
                mqtt_users[username] = mqtt_role
            selected = gateway["mqtt_" + component] if component in ("receive", "publish", "sender") else None
            configured.append(GatewayConfig(gateway_id, source, _mqtt(selected, config_path) if selected else None,
                _integer(selected["session_expiry_seconds"], 0, 0) if selected and component == "publish" else
                _integer(selected["session_expiry_seconds"], 3600, 2592000) if selected else None))
        bucket = raw.get("management", {}).get("shared_accounting_bucket")
        if component == "management" and bucket not in limits.gateways:
            raise ValueError("management accounting allocation is required")
        postgres = {role: MappingProxyType(_postgres_settings(raw["postgres_" + role], config_path,
                    os.environ, read_windows_credential) | {"connect_timeout": 3}) for role in COMPONENT_ROLES[component]}
        return WaveformConfig(component, general["topic_prefix"], limits, tuple(configured),
                              MappingProxyType(postgres), workers, connections, bucket, sender_spool)
    except Exception:
        # Upstream secret-store errors may include references or OS error text.
        raise WaveformError("waveform.configuration_invalid", "Invalid or unavailable waveform component configuration") from None
