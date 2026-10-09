"""Gateway-first quota accounting and the shared durable-transaction guard.

No connection, migration, or configuration changes occur at import time.
"""
from dataclasses import fields
from psycopg import sql
from .config import Quota
from .errors import WaveformError


def row(cursor):
    value = cursor.fetchone()
    if value is None:
        return None
    return dict(zip((column.name for column in cursor.description), value))


def _durability_settings(cursor):
    cursor.execute("SELECT current_setting('fsync'),current_setting('full_page_writes'),current_setting('synchronous_commit')")
    return cursor.fetchone()


def prepare_transaction(cursor):
    """Require a real transaction with logged storage and durable local WAL.

    Call before writes, including external import/audit transactions. This uses
    only bounded reads and SET LOCAL; no grants, DDL, or persistent settings.
    The caller must expose results only after commit returns successfully.
    """
    if cursor.connection.autocommit:
        raise WaveformError("waveform.transaction_required", "An explicit transaction is required")
    # Values in pg_settings are normalized milliseconds. Keep a stricter
    # caller deadline, while bounding a default unlimited wait.
    for setting, maximum in (("lock_timeout", 3000), ("statement_timeout", 15000)):
        cursor.execute("SELECT set_config(name,least(coalesce(nullif(setting::bigint,0),%s),%s)::text,true) FROM pg_catalog.pg_settings WHERE name=%s",
                       (maximum, maximum, setting))
    fsync, full_page_writes, synchronous = _durability_settings(cursor)
    if fsync != "on" or full_page_writes != "on":
        raise WaveformError("waveform.durability_unavailable", "Durable WAL settings are unavailable")
    if synchronous not in ("on", "remote_apply"):
        cursor.execute("SET LOCAL synchronous_commit=on")
    cursor.execute("SELECT current_setting('synchronous_commit')")
    if cursor.fetchone()[0] not in ("on", "remote_apply"):
        raise WaveformError("waveform.durability_unavailable", "Durable commit setting is unavailable")
    cursor.execute("SELECT c.relname,c.relpersistence FROM pg_catalog.pg_class c JOIN pg_catalog.pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname='waveforms' AND c.relkind IN ('r','p') ORDER BY c.relname LIMIT 65")
    tables = cursor.fetchall()
    if (not 1 <= len(tables) <= 64 or any(persistence != "p" for _, persistence in tables)
            or not {"facts", "message_outcomes", "capacity", "captures", "receipts", "observations", "state_head"}.issubset({name for name, _ in tables})):
        raise WaveformError("waveform.durability_unavailable", "Waveform storage must be permanent and logged")


def lock_gateway(cursor, gateway, limits):
    """First data lock in every producer transaction; '*' is never locked."""
    cursor.execute("SELECT * FROM waveforms.capacity WHERE bucket=%s FOR UPDATE", (gateway,))
    current = row(cursor)
    quota = limits.gateways[gateway]
    if current is None or any(current[f.name] != getattr(quota, f.name) for f in fields(Quota)):
        raise WaveformError("waveform.capacity_configuration", "Configured gateway capacity does not match storage")
    return current


def charge(cursor, gateway, current, **deltas):
    """Apply one net transfer; metadata rows are charged by existing triggers."""
    allowed = {"used_raw_bytes", "reserved_raw_bytes", "used_facts", "used_incomplete_captures",
               "used_orphan_bytes", "used_quarantine_bytes"}
    if not set(deltas) <= allowed or any(type(value) is not int for value in deltas.values()):
        raise ValueError("invalid capacity transfer")
    updated = current | {name: current[name] + delta for name, delta in deltas.items()}
    for name in allowed - {"reserved_raw_bytes"}:
        if not 0 <= updated[name] <= current[name.removeprefix("used_")]:
            raise WaveformError("waveform.capacity_exhausted", "Gateway waveform capacity is exhausted")
    if (updated["reserved_raw_bytes"] < 0 or updated["used_raw_bytes"] + updated["reserved_raw_bytes"] > current["raw_bytes"]
            or updated["used_orphan_bytes"] > updated["used_raw_bytes"] or updated["used_quarantine_bytes"] > updated["used_raw_bytes"]):
        raise WaveformError("waveform.capacity_exhausted", "Gateway waveform capacity is exhausted")
    nonzero = {name: delta for name, delta in deltas.items() if delta}
    if nonzero:
        cursor.execute(sql.SQL("UPDATE waveforms.capacity SET {} WHERE bucket=%s").format(
            sql.SQL(",").join(sql.SQL("{}={}+%s").format(sql.Identifier(name), sql.Identifier(name)) for name in nonzero)),
            (*nonzero.values(), gateway))
