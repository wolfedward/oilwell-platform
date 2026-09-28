"""Explicit registry identities; never fall back to the ingestion account."""
from dataclasses import dataclass, field
from ipaddress import IPv6Address
import os
from pathlib import Path
import re
import tomllib
from urllib.parse import urlsplit

from backend.center.config import _postgres_settings, CenterConfigurationError
from backend.secret_store import read_windows_credential
from .errors import RegistryError


@dataclass(frozen=True)
class RegistryConfig:
    writer: dict = field(repr=False)
    reader: dict = field(repr=False)
    allowed_origins: tuple[str, ...]
    online_seconds: int = 120
    delayed_seconds: int = 900
    clock_tolerance_seconds: int = 300


def _invalid(code="config_invalid"):
    return RegistryError("registry." + code, "台账配置不完整或不安全，请检查独立账号、连接与允许来源", 503)


def _origin(value):
    if not isinstance(value, str) or any(ord(c) < 33 or ord(c) > 126 for c in value):
        raise _invalid()
    try:
        parsed = urlsplit(value)
        if (parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.port == 0
                or parsed.netloc.endswith(":")
                or parsed.username is not None or parsed.password is not None
                or parsed.path or parsed.query or parsed.fragment or "?" in value or "#" in value
                or "*" in value or "\\" in value or "%" in parsed.netloc):
            raise _invalid()
        host = parsed.hostname
        if ":" in host:
            host = "[" + IPv6Address(host).compressed + "]"
        # Browsers omit default ports in Origin; do not normalize untrusted headers.
        port = parsed.port
        suffix = "" if port is None or port == {"http": 80, "https": 443}[parsed.scheme] else f":{port}"
    except ValueError:
        raise _invalid() from None
    return f"{parsed.scheme}://{host}{suffix}"


def _identity(section):
    if not isinstance(section, dict) or "password" in section:
        raise _invalid()
    host, port = section.get("host"), section.get("port", 5432)
    if (not isinstance(host, str) or not host or host != host.strip()
            or any(c.isspace() or ord(c) < 32 for c in host) or "/" in host or "\\" in host or "," in host
            or type(port) is not int or not 1 <= port <= 65535):
        raise _invalid()
    for key in ("database", "user"):
        if not isinstance(section.get(key), str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}", section[key]):
            raise _invalid()
    return host, port, section["database"]


def load_registry_config(path, *, env=None, credential_reader=None) -> RegistryConfig | None:
    try:
        config_path = Path(path).resolve()
        with config_path.open("rb") as handle:
            raw = tomllib.load(handle)
        options = raw.get("registry", {})
        if not isinstance(options, dict) or type(options.get("enabled", False)) is not bool:
            raise _invalid()
        if not options.get("enabled", False):
            return None
        if set(options) - {"enabled", "allowed_origins", "online_seconds", "delayed_seconds", "clock_tolerance_seconds"}:
            raise _invalid()
        if "postgres_query" not in raw:
            raise _invalid("query_identity_required")
        if "postgres_registry" not in raw or "postgres" not in raw:
            raise _invalid("writer_identity_required")
        sections = [raw[name] for name in ("postgres", "postgres_registry", "postgres_query")]
        if len({_identity(s) for s in sections}) != 1:
            raise _invalid("database_mismatch")
        if len({s["user"] for s in sections}) != 3:
            raise _invalid("identity_not_separated")
        origins = options.get("allowed_origins")
        if not isinstance(origins, list) or not 1 <= len(origins) <= 16:
            raise _invalid()
        origins = tuple(dict.fromkeys(_origin(value) for value in origins))
        thresholds = {"online_seconds": options.get("online_seconds", 120),
                      "delayed_seconds": options.get("delayed_seconds", 900),
                      "clock_tolerance_seconds": options.get("clock_tolerance_seconds", 300)}
        for key, value in thresholds.items():
            if type(value) is not int or not (0 if key == "clock_tolerance_seconds" else 1) <= value <= 86400:
                raise _invalid()
        if thresholds["delayed_seconds"] <= thresholds["online_seconds"]:
            raise _invalid()
        environment = os.environ if env is None else env
        lookup = credential_reader or read_windows_credential
        for section in sections[1:]:
            if set(section) - {"host", "port", "database", "user", "password_env", "password_credential", "sslmode", "sslrootcert"}:
                raise _invalid()
            if section.get("sslmode", "verify-full") not in {"disable", "allow", "prefer", "require", "verify-ca", "verify-full"}:
                raise _invalid()
            for key in ("password_env", "password_credential", "sslrootcert"):
                if key in section and (not isinstance(section[key], str) or not section[key] or "\x00" in section[key]):
                    raise _invalid()
        # Ingest credentials are not needed, and deliberately never resolved.
        identities = [_postgres_settings(s, config_path, environment, lookup) | {"connect_timeout": 3}
                      for s in sections[1:]]
        if any(not isinstance(identity["password"], str) or not identity["password"] or "\x00" in identity["password"]
               for identity in identities):
            raise _invalid()
        return RegistryConfig(identities[0], identities[1], origins, **thresholds)
    except RegistryError:
        raise
    except (OSError, ValueError, KeyError, TypeError, AttributeError, CenterConfigurationError):
        raise _invalid() from None
