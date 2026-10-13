"""Explicit monitoring reader; startup probes privileges, never grants them."""
from dataclasses import dataclass, field
import logging
from .config import load_monitoring_config
from .errors import MonitoringError


@dataclass(frozen=True)
class MonitoringRuntime:
    status: str
    query: object = field(default=None, repr=False)
    code: str | None = None
    environment: str | None = None
    source_ref: str | None = None


def build_monitoring_runtime(path):
    try:
        config = load_monitoring_config(path)
        if config is None: return MonitoringRuntime('disabled', code='monitoring.disabled')
        from backend.registry.config import load_registry_config
        registry = load_registry_config(path)
        if registry is None: raise MonitoringError('monitoring.config_invalid', '台账配置未启用', 503)
        import psycopg
        from .privileges import probe_reader
        from .queries import MonitoringQueries
        def connect(): return psycopg.connect(**config.postgres)
        probe_reader(connect)
        query = MonitoringQueries(connect, environment=config.environment, source_ref=config.source_ref,
            online_seconds=registry.online_seconds, delayed_seconds=registry.delayed_seconds,
            clock_tolerance_seconds=registry.clock_tolerance_seconds)
        return MonitoringRuntime('ready', query=query, environment=config.environment, source_ref=config.source_ref)
    except MonitoringError as error: code = error.code
    except ImportError: code = 'monitoring.dependency_unavailable'
    except Exception: code = 'monitoring.unavailable'
    logging.getLogger(__name__).warning('Monitoring unavailable: %s', code)
    return MonitoringRuntime('unavailable', code=code)
