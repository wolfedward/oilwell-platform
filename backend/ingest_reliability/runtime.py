"""Optional Web runtime: isolated reader/operator probes; never startup DDL."""
from dataclasses import dataclass, field
import logging
from typing import TYPE_CHECKING

from .config import load_reliability_config
from .errors import ReliabilityError

if TYPE_CHECKING:
    from .operations import ReliabilityOperations
    from .queries import ReliabilityQueries


@dataclass(frozen=True)
class ReliabilityRuntime:
    status: str
    queries: 'ReliabilityQueries | None' = field(default=None, repr=False)
    operations: 'ReliabilityOperations | None' = field(default=None, repr=False)
    code: str | None = None
    allowed_origins: tuple[str, ...] = ()


def probe_privileges(reader, operator):
    from .privileges import probe_role
    probe_role(reader, 'reader')
    probe_role(operator, 'operator')


def build_reliability_runtime(config_path):
    if config_path is None:
        return ReliabilityRuntime('disabled', code='ingest.disabled')
    try:
        config = load_reliability_config(config_path, component='management')
        if config is None:
            return ReliabilityRuntime('disabled', code='ingest.disabled')
        import psycopg
        from .queries import ReliabilityQueries
        from .operations import ReliabilityOperations
        def reader():
            return psycopg.connect(**config.postgres_reader)
        def operator():
            return psycopg.connect(**config.postgres_operator)
        probe_privileges(reader, operator)
        return ReliabilityRuntime('ready',
                                  ReliabilityQueries(reader, health_stale_seconds=config.health_stale_seconds),
                                  ReliabilityOperations(operator), allowed_origins=config.allowed_origins)
    except ReliabilityError as error:
        code = error.code
    except ImportError:
        code = 'ingest.dependency_unavailable'
    except Exception:
        # Driver diagnostics can contain connection strings and credentials.
        code = 'ingest.connection_unavailable'
    logging.getLogger(__name__).warning('Incident management unavailable: %s', code)
    return ReliabilityRuntime('unavailable', code=code)
