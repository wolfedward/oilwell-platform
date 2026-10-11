"""Explicit private configuration. Import and disabled loading perform no DB I/O."""
from dataclasses import dataclass, field
import os
from pathlib import Path
import tomllib


class ConfigurationError(ValueError):
    def __init__(self): super().__init__('Invalid or unavailable inversion configuration')


@dataclass(frozen=True)
class InversionConfig:
    path: Path = field(repr=False)
    identity_db_path: Path = field(repr=False)
    waveform_config_path: Path = field(repr=False)
    temp_root: Path = field(repr=False)
    postgres: dict = field(repr=False)
    waveform: object = field(repr=False)
    total_storage_bytes: int


def load_inversion_config(path):
    try:
        path = Path(path).resolve(strict=True)
        with path.open('rb') as handle: raw = tomllib.load(handle)
        if set(raw)-{'inversion','postgres_reader','postgres_operator'}: raise ValueError()
        general = raw['inversion']
        if set(general)-{'enabled','identity_db_path','waveform_config_path','temp_root','total_storage_bytes'}: raise ValueError()
        if type(general['enabled']) is not bool: raise ValueError()
        if not general['enabled']: return None
        if set(general) != {'enabled','identity_db_path','waveform_config_path','temp_root','total_storage_bytes'}: raise ValueError()
        paths = {}
        for key in ('identity_db_path','waveform_config_path','temp_root'):
            value = general[key]
            if type(value) is not str or not value: raise ValueError()
            target = Path(value)
            paths[key] = (target if target.is_absolute() else path.parent / target).resolve(strict=True)
        if not paths['identity_db_path'].is_file() or not paths['waveform_config_path'].is_file() or not paths['temp_root'].is_dir(): raise ValueError()
        # Worker validates private staging ownership and replacement before use.
        total = general['total_storage_bytes']
        if type(total) is not int or not 1 <= total <= 2**63-1: raise ValueError()
        from backend.center.config import _postgres_settings
        from backend.secret_store import read_windows_credential
        from backend.waveforms.config import load_waveform_config
        postgres = {}
        for role in ('reader','operator'):
            section = raw['postgres_' + role]
            if set(section)-{'host','port','database','user','password_env','password_credential','sslmode','sslrootcert'}: raise ValueError()
            postgres[role] = _postgres_settings(section,path,os.environ,read_windows_credential) | {'connect_timeout':3}
        waveform = load_waveform_config(paths['waveform_config_path'], component='management')
        if waveform is None: raise ValueError()
        settings = [*postgres.values(), *(waveform.postgres[r] for r in ('reader','operator'))]
        if len({s['user'] for s in settings}) != 4: raise ValueError()
        if len({(s['host'].lower(),s['port'],s['dbname']) for s in settings}) != 1: raise ValueError()
        return InversionConfig(path, **paths, postgres=postgres, waveform=waveform, total_storage_bytes=total)
    except Exception:
        raise ConfigurationError() from None
