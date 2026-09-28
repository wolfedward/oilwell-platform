"""Explicit component-scoped configuration for reliable ingestion."""

from dataclasses import dataclass, field
from ipaddress import IPv6Address
import math
import os
from pathlib import Path
import tomllib
from urllib.parse import urlsplit

from backend.center.config import _postgres_settings, _secret
from backend.gateway.mqtt_uplink import MqttConfig
from backend.secret_store import read_windows_credential
from .contracts import CapacityLimits, valid_name, valid_prefix
from .errors import ReliabilityError


PG_SECTIONS = {"ingestor": "postgres_reliable_ingest", "publisher": "postgres_receipt_publisher",
               "reader": "postgres_ingest_reader", "operator": "postgres_ingest_operator"}
OPTIONS = {"enabled", "receipt_gateways", "total_bytes", "quarantine_bytes", "allowed_origins",
           "publish_batch_size", "lease_seconds", "retry_max_seconds", "health_stale_seconds", "receive_maximum"}
PG_FIELDS = {"host", "port", "database", "user", "password_env", "password_credential", "sslmode", "sslrootcert"}


@dataclass(frozen=True)
class ReliabilityConfig:
    enabled: bool
    topic_prefix: str
    receipt_gateways: frozenset[str]
    limits: CapacityLimits
    postgres_ingest: dict | None = field(default=None, repr=False)
    postgres_publisher: dict | None = field(default=None, repr=False)
    postgres_reader: dict | None = field(default=None, repr=False)
    postgres_operator: dict | None = field(default=None, repr=False)
    mqtt_publisher: MqttConfig | None = field(default=None, repr=False)
    allowed_origins: tuple[str, ...] = ()
    receive_maximum: int = 20
    publish_batch_size: int = 20
    lease_seconds: int = 30
    retry_max_seconds: int = 300
    health_stale_seconds: int = 120


def _invalid():
    return ReliabilityError("ingest.config_invalid", "接收可靠性配置不完整或不安全，请检查独立账号、容量及组件参数")


def _integer(options, name, default, minimum, maximum):
    value = options.get(name, default)
    if type(value) is not int or not minimum <= value <= maximum:
        raise _invalid()
    return value


def _identity(section):
    if not isinstance(section, dict) or "password" in section:
        raise _invalid()
    host, port = section.get("host"), section.get("port", 5432)
    if (not isinstance(host, str) or not host or any(c.isspace() or ord(c) < 32 for c in host)
            or any(c in host for c in "/\\,") or type(port) is not int or not 1 <= port <= 65535
            or not valid_name(section.get("database")) or not valid_name(section.get("user"))):
        raise _invalid()
    return host, port, section["database"]


def _origin(value):
    if not isinstance(value, str) or any(ord(c) < 33 or ord(c) > 126 for c in value):
        raise _invalid()
    parsed = urlsplit(value)
    if (parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.port == 0 or parsed.netloc.endswith(":")
            or parsed.username is not None or parsed.password is not None or parsed.path or parsed.query or parsed.fragment
            or "?" in value or "#" in value or "*" in value or "\\" in value or "%" in parsed.netloc):
        raise _invalid()
    host = parsed.hostname
    if ":" in host:
        host = "[" + IPv6Address(host).compressed + "]"
    port = parsed.port
    suffix = "" if port is None or port == {"http": 80, "https": 443}[parsed.scheme] else f":{port}"
    return f"{parsed.scheme}://{host}{suffix}"


def _postgres(section, path, env, lookup):
    if set(section) - PG_FIELDS or section.get("sslmode", "verify-full") not in {
            "disable", "allow", "prefer", "require", "verify-ca", "verify-full"}:
        raise _invalid()
    for name in ("password_env", "password_credential", "sslrootcert"):
        if name in section and (not isinstance(section[name], str) or not section[name] or "\x00" in section[name]):
            raise _invalid()
    result = _postgres_settings(section, path, env, lookup) | {"connect_timeout": 3}
    if not isinstance(result["password"], str) or not result["password"] or "\x00" in result["password"]:
        raise _invalid()
    return result


def _mqtt_numbers(section):
    if not isinstance(section, dict):
        raise _invalid()
    numbers = {"port": _integer(section, "port", 1883, 1, 65535),
               "keepalive_seconds": _integer(section, "keepalive_seconds", 30, 10, 300)}
    for name in ("connect_timeout_seconds", "publish_timeout_seconds"):
        value = section.get(name, 10)
        if type(value) not in (int, float) or not math.isfinite(value) or not 0 < value <= 60:
            raise _invalid()
        numbers[name] = value
    return numbers


def _publisher_mqtt(raw, path, env, lookup):
    section = raw["mqtt_receipt_publisher"]
    allowed = {"host", "port", "client_id", "username", "password_env", "password_credential", "ca_cert",
               "client_cert", "client_key", "keepalive_seconds", "connect_timeout_seconds", "publish_timeout_seconds"}
    if not isinstance(section, dict) or set(section) - allowed:
        raise _invalid()
    if not valid_name(section.get("client_id")) or not valid_name(section.get("username")):
        raise _invalid()
    incoming = raw.get("mqtt", {})
    if not isinstance(incoming, dict):
        raise _invalid()
    if any(section[key] == incoming.get(key) for key in ("client_id", "username")):
        raise _invalid()
    host = section["host"]
    if not isinstance(host, str) or not host or any(c.isspace() or ord(c) < 32 for c in host):
        raise _invalid()
    numbers = _mqtt_numbers(section)
    certificates = {}
    for name in ("ca_cert", "client_cert", "client_key"):
        value = section.get(name)
        if value is not None:
            if not isinstance(value, str) or not value or "\x00" in value:
                raise _invalid()
            certificate = Path(value)
            certificates[name] = str(certificate if certificate.is_absolute() else (path.parent.parent / certificate).resolve())
    password = _secret(section, env, "mqtt_receipt_publisher", section["username"], lookup)
    if not isinstance(password, str) or not password or "\x00" in password:
        raise _invalid()
    return MqttConfig(host=host, client_id=section["client_id"], username=section["username"],
                      password=password, **numbers, **certificates)


def load_reliability_config(path, *, component, env=None, credential_reader=None):
    try:
        if component not in {"ingestor", "publisher", "management"}:
            raise _invalid()
        config_path = Path(path).resolve()
        with config_path.open("rb") as handle:
            raw = tomllib.load(handle)
        options = raw.get("reliability", {})
        if not isinstance(options, dict) or type(options.get("enabled", False)) is not bool:
            raise _invalid()
        if not options.get("enabled", False):
            return None
        if set(options) - OPTIONS:
            raise _invalid()
        prefix = raw.get("center", {}).get("topic_prefix", "oilfield")
        if not valid_prefix(prefix):
            raise _invalid()
        limits = CapacityLimits(options["total_bytes"], options["quarantine_bytes"])
        gateways = options.get("receipt_gateways", [])
        if (not isinstance(gateways, list) or len(gateways) > 10_000 or any(not valid_name(item) for item in gateways)
                or len(set(gateways)) != len(gateways)):
            raise _invalid()
        required = ("reader", "operator") if component == "management" else (component,)
        if any(PG_SECTIONS[role] not in raw for role in required):
            raise _invalid()
        declared = [raw[name] for name in ("postgres", *PG_SECTIONS.values()) if name in raw]
        if len({_identity(section) for section in declared}) != 1 or len({section["user"] for section in declared}) != len(declared):
            raise _invalid()
        environment = os.environ if env is None else env
        lookup = credential_reader or read_windows_credential
        identities = {role: _postgres(raw[PG_SECTIONS[role]], config_path, environment, lookup) for role in required}
        extras = {}
        if component == "ingestor":
            _mqtt_numbers(raw.get("mqtt", {}))
            extras["receive_maximum"] = _integer(options, "receive_maximum", 20, 1, 1024)
        if component == "publisher":
            extras.update(mqtt_publisher=_publisher_mqtt(raw, config_path, environment, lookup),
                          publish_batch_size=_integer(options, "publish_batch_size", 20, 1, 200),
                          lease_seconds=_integer(options, "lease_seconds", 30, 5, 300),
                          retry_max_seconds=_integer(options, "retry_max_seconds", 300, 1, 3600))
        if component in {"publisher", "management"}:
            extras["health_stale_seconds"] = _integer(options, "health_stale_seconds", 120, 5, 86400)
        if component == "management":
            origins = options.get("allowed_origins")
            if not isinstance(origins, list) or not 1 <= len(origins) <= 16:
                raise _invalid()
            extras["allowed_origins"] = tuple(dict.fromkeys(_origin(origin) for origin in origins))
        return ReliabilityConfig(True, prefix, frozenset(gateways), limits,
                                 postgres_ingest=identities.get("ingestor"), postgres_publisher=identities.get("publisher"),
                                 postgres_reader=identities.get("reader"), postgres_operator=identities.get("operator"), **extras)
    except ReliabilityError:
        raise
    except (OSError, ValueError, KeyError, TypeError, AttributeError, RuntimeError):
        raise _invalid() from None
