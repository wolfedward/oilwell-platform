"""One PostgreSQL commit for business state, audit, receipt and revision."""
from collections.abc import Callable
from dataclasses import asdict
from datetime import datetime
from uuid import UUID, uuid4

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from .contracts import Change, Command
from .errors import RegistryError
from .values import MAX_VERSION


def bump(value: int) -> int:
    if value >= MAX_VERSION:
        raise RegistryError("registry.version_exhausted", "版本已达到上限，不能继续变更", 409)
    return value + 1


def ensure_later(instant: datetime, starts: list[datetime]) -> None:
    if any(instant <= start for start in starts):
        raise RegistryError("registry.clock_conflict", "服务器时间未晚于现有生效时间，请核查时钟后重试", 409)


class RegistryTransactions:
    def __init__(self, connect: Callable[[], psycopg.Connection]):
        self.connect = connect

    def run(self, command: Command,
            apply: Callable[[psycopg.Cursor, Command, datetime, UUID], Change], *,
            lock: Callable[[psycopg.Cursor, Command], None] | None = None) -> dict:
        # Authentication/authorization belongs at the HTTP boundary BEFORE
        # calling here, including retries which might find an existing receipt.
        try:
            with self.connect() as conn:
                with conn.transaction(), conn.cursor(row_factory=dict_row) as cur:
                    cur.execute("SET LOCAL lock_timeout = '1s'")
                    cur.execute("SET LOCAL statement_timeout = '3s'")
                    cur.execute("SET LOCAL TIME ZONE 'UTC'")
                    cur.execute("SELECT revision FROM device_registry.registry_head WHERE singleton=1 FOR UPDATE")
                    head = cur.fetchone()
                    if head is None:
                        raise RegistryError("registry.unavailable", "台账修订记录缺失，请联系管理员", 503)
                    cur.execute("SELECT request_hash,result FROM device_registry.operation_receipts "
                                "WHERE actor_domain=%s AND actor_id=%s AND key=%s",
                                (command.actor.domain, command.actor.user_id, command.key))
                    receipt = cur.fetchone()
                    if receipt:
                        if receipt["request_hash"] != command.request_hash:
                            raise RegistryError("registry.idempotency_conflict", "重试内容已改变，不能复用原操作标识", 409)
                        result = receipt["result"] | {"replayed": True}
                    else:
                        if lock is not None:
                            lock(cur, command)
                        cur.execute("SELECT clock_timestamp() AS instant")
                        instant = cur.fetchone()["instant"]
                        event_id = uuid4()
                        change = apply(cur, command, instant, event_id)
                        next_revision = bump(head["revision"])
                        result = {"entity": change.entity, "registry_revision": str(next_revision),
                                  "committed_at": instant.isoformat(timespec="microseconds"), "replayed": False}
                        cur.execute("INSERT INTO device_registry.audit_events "
                                    "(id,actor,action,object_type,object_id,reason,before_data,after_data,event_at,request_id) "
                                    "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                                    (event_id, Jsonb(asdict(command.actor)), command.action, change.object_type,
                                     change.object_id, command.body["reason"], Jsonb(change.before),
                                     Jsonb(change.after), instant, command.key))
                        cur.execute("INSERT INTO device_registry.operation_receipts "
                                    "(actor_domain,actor_id,key,request_hash,result,committed_at) VALUES (%s,%s,%s,%s,%s,%s)",
                                    (command.actor.domain, command.actor.user_id, command.key,
                                     command.request_hash, Jsonb(result), instant))
                        cur.execute("UPDATE device_registry.registry_head SET revision=%s WHERE singleton=1", (next_revision,))
            # Return only AFTER both transaction and connection contexts exit;
            # commit errors must be observable even if the database committed.
            return result
        except (psycopg.errors.UniqueViolation, psycopg.errors.ExclusionViolation,
                psycopg.errors.SerializationFailure) as error:
            raise RegistryError("registry.write_conflict", "登记或历史关系发生冲突，请刷新后核对", 409) from error
        except psycopg.Error as error:
            # Never try another query in the failed transaction, and never
            # expose driver SQL, host names or credentials in the public error.
            raise RegistryError("registry.unavailable", "台账暂时不可用，请保留原操作标识和内容后重试", 503) from error
