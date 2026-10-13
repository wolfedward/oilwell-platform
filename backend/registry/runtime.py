"""Optional registry runtime: read-only preflight, safe failure, no startup DDL."""
from __future__ import annotations

from dataclasses import dataclass, field
import logging
from typing import TYPE_CHECKING

from .config import load_registry_config
from .errors import RegistryError

if TYPE_CHECKING:
    from .queries import RegistryQueries
    from .service import RegistryService


@dataclass(frozen=True)
class RegistryRuntime:
    status: str
    service: RegistryService | None = field(default=None, repr=False)
    query: RegistryQueries | None = field(default=None, repr=False)
    code: str | None = None
    allowed_origins: tuple[str, ...] = ()


def probe_privileges(writer_connect, reader_connect) -> None:
    # Lazy imports keep the disabled module usable without optional PG drivers.
    from .privileges import probe
    probe(writer_connect, reader_connect)


def build_registry_runtime(path, *, require_independent_review=False) -> RegistryRuntime:
    if path is None:
        return RegistryRuntime("disabled", code="registry.disabled")
    try:
        config = load_registry_config(path)
        if config is None:
            return RegistryRuntime("disabled", code="registry.disabled")
        import psycopg
        from .queries import RegistryQueries
        from .service import RegistryService
        from .transactions import RegistryTransactions
        def writer():
            return psycopg.connect(**config.writer)
        def reader():
            return psycopg.connect(**config.reader)
        probe_privileges(writer, reader)
        return RegistryRuntime("ready", RegistryService(RegistryTransactions(writer), require_independent_review=require_independent_review),
                               RegistryQueries(reader, online_seconds=config.online_seconds,
                                               delayed_seconds=config.delayed_seconds,
                                               clock_tolerance_seconds=config.clock_tolerance_seconds),
                               allowed_origins=config.allowed_origins)
    except RegistryError as error:
        code = error.code
    except ImportError:
        code = "registry.dependency_unavailable"
    except Exception:
        # Connection errors can contain credentials/DSNs. Never log their text,
        # repr or traceback. Module failure must not take down the old platform.
        code = "registry.connection_unavailable"
    logging.getLogger(__name__).warning("Registry unavailable: %s", code)
    return RegistryRuntime("unavailable", code=code)
