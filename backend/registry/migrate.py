"""Explicit registry DDL and role grants, never called by application startup."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import sys
import tomllib

import psycopg
from psycopg import sql

from backend.center.config import _postgres_settings, CenterConfigurationError
from backend.secret_store import read_windows_credential
from .errors import RegistryError


SQL_DIR = Path(__file__).resolve().parent / "sql"
MIGRATION_LOCK = 499001111
VERSION = 1


def _failure(code, message):
    return RegistryError("registry." + code, message, 503)


def _source():
    # Universal-newline reading makes migration identity stable across Git's
    # Windows CRLF checkout and Linux LF checkout, not platform-dependent.
    source = (SQL_DIR / "001_registry.sql").read_text(encoding="utf-8")
    return source, hashlib.sha256(source.encode("utf-8")).hexdigest()


def _target(connection, expected_database):
    if not isinstance(expected_database, str) or not expected_database:
        raise _failure("migration_target", "必须明确指定目标数据库")
    actual = connection.execute("SELECT current_database()").fetchone()[0]
    if actual != expected_database:
        raise _failure("migration_target", "连接数据库与明确指定的目标不一致")


def _record(connection, digest):
    if connection.execute("SELECT to_regclass('device_registry.schema_migrations')").fetchone()[0] is None:
        raise _failure("schema_missing", "台账迁移记录不存在，须由迁移身份显式初始化")
    rows = connection.execute("SELECT version,sha256 FROM device_registry.schema_migrations ORDER BY version").fetchall()
    if rows != [(VERSION, digest)]:
        raise _failure("schema_mismatch", "台账迁移版本或摘要不匹配，拒绝自动修复或覆盖")


def check_schema(connection, *, expected_database):
    """Read-only version/digest check on the caller's connection."""
    _target(connection, expected_database)
    _, digest = _source()
    _record(connection, digest)
    return {"version": str(VERSION), "sha256": digest, "applied": False}


def migrate(connect_kwargs: dict, *, expected_database: str) -> dict:
    source, digest = _source()
    settings = connect_kwargs | {"connect_timeout": 3}
    try:
        with psycopg.connect(**settings) as connection:
            with connection.transaction():
                _target(connection, expected_database)
                connection.execute("SET LOCAL lock_timeout = '1s'")
                connection.execute("SET LOCAL statement_timeout = '3s'")
                connection.execute("SELECT pg_advisory_xact_lock(%s)", (MIGRATION_LOCK,))
                exists = connection.execute("SELECT to_regnamespace('device_registry')").fetchone()[0]
                if exists is not None:
                    _record(connection, digest)
                    applied = False
                else:
                    connection.execute(source)
                    connection.execute("INSERT INTO device_registry.schema_migrations(version,sha256) VALUES (%s,%s)",
                                       (VERSION, digest))
                    applied = True
            return {"version": str(VERSION), "sha256": digest, "applied": applied}
    except psycopg.Error as error:
        raise _failure("migration_failed", "台账迁移失败；检查迁移身份、扩展安装权限及数据库日志") from error


def grant_roles(connection: psycopg.Connection, *, writer: str, reader: str) -> None:
    if not isinstance(writer, str) or not isinstance(reader, str) or not writer or not reader or writer == reader:
        raise _failure("invalid_roles", "须明确指定不同的台账写入和只读角色")
    for name in (writer, reader):
        row = connection.execute("SELECT oid,rolsuper,rolcanlogin,rolcreatedb,rolcreaterole FROM pg_roles WHERE rolname=%s", (name,)).fetchone()
        if row is None or row[1] or not row[2] or row[3] or row[4]:
            raise _failure("invalid_roles", "运行角色不存在或持有管理权限")
        owner = connection.execute("SELECT EXISTS(SELECT 1 FROM pg_namespace WHERE nspname='device_registry' AND nspowner=%s) "
                                   "OR EXISTS(SELECT 1 FROM pg_class c JOIN pg_namespace n ON c.relnamespace=n.oid "
                                   "WHERE n.nspname='device_registry' AND c.relowner=%s) "
                                   "OR EXISTS(SELECT 1 FROM pg_extension WHERE extname='btree_gist' AND extowner=%s)",
                                   (row[0], row[0], row[0])).fetchone()[0]
        if owner:
            raise _failure("invalid_roles", "迁移所有者不能兼任运行账号")
    statement = sql.SQL((SQL_DIR / "grants.sql").read_text(encoding="utf-8"))
    connection.execute(statement.format(writer=sql.Identifier(writer), reader=sql.Identifier(reader)))


def main(argv=None):
    parser = argparse.ArgumentParser(description="Explicit production registry migration (no database/account creation)")
    parser.add_argument("--config", required=True)
    parser.add_argument("--expected-database", required=True)
    parser.add_argument("--writer-role", required=True)
    parser.add_argument("--reader-role", required=True)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args(argv)
    try:
        config = Path(args.config).resolve()
        raw = tomllib.loads(config.read_text(encoding="utf-8"))
        settings = _postgres_settings(raw["postgres_migrator"], config, os.environ, read_windows_credential)
        if args.check:
            with psycopg.connect(**(settings | {"connect_timeout": 3})) as connection:
                connection.execute("SET TRANSACTION READ ONLY")
                result = check_schema(connection, expected_database=args.expected_database)
        else:
            result = migrate(settings, expected_database=args.expected_database)
            with psycopg.connect(**(settings | {"connect_timeout": 3})) as connection:
                _target(connection, args.expected_database)
                grant_roles(connection, writer=args.writer_role, reader=args.reader_role)
        print(json.dumps(result))
        return 0
    except RegistryError as error:
        print(json.dumps({"code": error.code, "message": error.message}, ensure_ascii=False), file=sys.stderr)
    except (OSError, ValueError, KeyError, TypeError, CenterConfigurationError, psycopg.Error):
        print(json.dumps({"code": "registry.migration_failed", "message": "检查配置、凭据引用与迁移权限"}, ensure_ascii=False), file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
