"""Explicit read identity; disabled means no fallback to another account."""
from dataclasses import dataclass, field
import os
from pathlib import Path
import tomllib
from uuid import UUID

from backend.center.config import _postgres_settings, CenterConfigurationError
from backend.registry.config import _identity
from backend.registry.errors import RegistryError
from backend.secret_store import read_windows_credential
from .errors import MonitoringError


@dataclass(frozen=True)
class MonitoringConfig:
    postgres: dict = field(repr=False)
    environment: str
    source_ref: str


def load_monitoring_config(path, *, env=None, credential_reader=None) -> MonitoringConfig | None:
    if path is None:
        return None
    try:
        path = Path(path).resolve()
        with path.open('rb') as stream:
            raw = tomllib.load(stream)
        options = raw.get('monitoring', {})
        if not isinstance(options, dict) or type(options.get('enabled', False)) is not bool:
            raise ValueError()
        if not options.get('enabled', False):
            return None
        if set(options) != {'enabled', 'environment', 'source_ref'} or options['environment'] not in ('test', 'production'):
            raise ValueError()
        source = str(UUID(options['source_ref']))
        section = raw['postgres_monitoring']
        identity = _identity(section)
        if set(section) - {'host', 'port', 'database', 'user', 'password_env', 'password_credential', 'sslmode', 'sslrootcert'}:
            raise ValueError()
        for name, other in raw.items():
            if name != 'postgres_monitoring' and (name == 'postgres' or name.startswith('postgres_')):
                if _identity(other) != identity or other['user'] == section['user']:
                    raise ValueError()
        if section.get('sslmode', 'verify-full') not in {'disable', 'allow', 'prefer', 'require', 'verify-ca', 'verify-full'}:
            raise ValueError()
        postgres = _postgres_settings(section, path, os.environ if env is None else env,
                                      credential_reader or read_windows_credential) | {'connect_timeout': 3}
        if not isinstance(postgres['password'], str) or not postgres['password'] or '\0' in postgres['password']:
            raise ValueError()
        return MonitoringConfig(postgres, options['environment'], source)
    except (OSError, ValueError, KeyError, TypeError, AttributeError, RegistryError, CenterConfigurationError):
        raise MonitoringError('monitoring.config_invalid', '监控查询配置不完整或账号未独立，请联系管理员核查', 503) from None
