"""Explicit initializer for the authorized local WP-03 test environment only."""

import argparse
import json
import os
from pathlib import Path
import re
import secrets
import sys
import tomllib
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import psycopg
from psycopg import sql
from backend.secret_store import StoredCredential, read_windows_credential


TEST_DATABASE = "oilwell_ingest_reliability_test"
RUNTIME_ROLES = ("migrator", "ingestor", "publisher", "reader", "operator")
PROVISION_LOCK = 499001209


class ProvisionError(ValueError):
    pass


def validate_identity(path, raw):
    path = Path(path)
    if not path.is_absolute() or path.name != "identity.json" or path.parent.name != "registry-test":
        raise ProvisionError("必须使用现有隔离实例的身份记录")
    if not isinstance(raw, dict):
        raise ProvisionError("隔离实例身份结构无效")
    if (raw.get("database") != "oilwell_registry_test" or raw.get("host") != "127.0.0.1"
            or type(raw.get("port")) is not int or raw["port"] != 3822):
        raise ProvisionError("拒绝连接非指定隔离数据库实例")
    data = raw.get("data")
    if not isinstance(data, str) or not Path(data).is_absolute():
        raise ProvisionError("隔离实例数据目录必须为绝对路径")
    if Path(data).resolve() != (path.parent / "data").resolve():
        raise ProvisionError("隔离实例数据目录与身份记录位置不一致")
    roles, targets = raw.get("roles"), raw.get("credential_targets")
    if not isinstance(roles, dict) or roles.get("admin") != "registry_test_admin":
        raise ProvisionError("仅允许使用已授权隔离实例的管理员身份")
    if (not isinstance(targets, dict) or not isinstance(targets.get("admin"), str)
            or not re.fullmatch(r"oilwell-registry-test-[0-9a-f]{32}-admin", targets["admin"])):
        raise ProvisionError("隔离管理员凭据引用不符合要求")
    return dict(raw, data=Path(data).resolve())


def validate_output(identity_path, output):
    output = Path(output)
    expected = Path(identity_path).parent.parent / "ingest-reliability-test"
    if not output.is_absolute() or output.resolve() != expected.resolve():
        raise ProvisionError("仅允许写入独立 ingest-reliability-test 目录")
    return output.resolve()


def _verify_live(connection, identity, *, database):
    # PostgreSQL on this Windows fixture reports its native-encoded directory.
    # Do not decode that setting as a UTF-8 telemetry value.
    connection.execute("SET client_encoding TO SQL_ASCII")
    row = connection.execute("SELECT current_database(),current_setting('data_directory'),"
                             "host(inet_server_addr()),inet_server_port()").fetchone()
    connection.execute("SET client_encoding TO UTF8")
    def decoded(value):
        return value.decode("mbcs" if os.name == "nt" else "utf-8") if isinstance(value, bytes) else value
    if (decoded(row[0]) != database or Path(decoded(row[1])).resolve() != identity["data"]
            or decoded(row[2]) != "127.0.0.1" or row[3] != 3822):
        raise ProvisionError("实际连接不属于指定隔离实例")


def _fixture_manifest(output, identity, *, database_exists):
    path = output / "identity.json"
    if path.exists():
        raw = json.loads(path.read_text(encoding="utf-8"))
        token = raw.get("fixture_id")
        if not isinstance(token, str) or not re.fullmatch(r"[0-9a-f]{32}", token):
            raise ProvisionError("现有 WP-03 身份记录无效")
    else:
        if database_exists or (output.exists() and any(output.iterdir())):
            raise ProvisionError("拒绝接管未记录的数据库或输出目录")
        token = uuid4().hex
        raw = None
    expected = {
        "version": 1, "fixture_id": token, "database": TEST_DATABASE,
        "host": "127.0.0.1", "port": 3822, "data": str(identity["data"]),
        "roles": {role: "ingest_test_" + token[:16] + "_" + role for role in RUNTIME_ROLES},
        "credential_targets": {role: "oilwell-ingest-test-" + token + "-" + role for role in RUNTIME_ROLES},
        "admin_target": identity["credential_targets"]["admin"],
    }
    if raw is not None and raw != expected:
        raise ProvisionError("拒绝改写不匹配的 WP-03 环境身份")
    if raw is None:
        output.mkdir(parents=False, exist_ok=True)
        with path.open("x", encoding="utf-8") as handle:
            json.dump(expected, handle, ensure_ascii=False, indent=2)
    return expected


def _credential(target, username, *, create):
    """Reuse owned credentials; never rotate or replace an existing entry."""
    if os.name != "nt":
        raise ProvisionError("此本机初始化工具需要 Windows 凭据管理器")
    import ctypes
    from ctypes import wintypes
    class CREDENTIALW(ctypes.Structure):
        _fields_ = [("Flags", wintypes.DWORD), ("Type", wintypes.DWORD),
                    ("TargetName", wintypes.LPWSTR), ("Comment", wintypes.LPWSTR),
                    ("LastWritten", wintypes.FILETIME), ("CredentialBlobSize", wintypes.DWORD),
                    ("CredentialBlob", ctypes.POINTER(ctypes.c_ubyte)), ("Persist", wintypes.DWORD),
                    ("AttributeCount", wintypes.DWORD), ("Attributes", ctypes.c_void_p),
                    ("TargetAlias", wintypes.LPWSTR), ("UserName", wintypes.LPWSTR)]
    api = ctypes.WinDLL("Advapi32.dll", use_last_error=True)
    pointer_type = ctypes.POINTER(CREDENTIALW)
    api.CredReadW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.POINTER(pointer_type)]
    api.CredReadW.restype = wintypes.BOOL
    api.CredWriteW.argtypes = [pointer_type, wintypes.DWORD]
    api.CredWriteW.restype = wintypes.BOOL
    api.CredFree.argtypes = [ctypes.c_void_p]
    pointer = pointer_type()
    if api.CredReadW(target, 1, 0, ctypes.byref(pointer)):
        try:
            item = pointer.contents
            if item.UserName != username:
                raise ProvisionError("现有测试凭据身份不匹配")
            password = ctypes.string_at(item.CredentialBlob, item.CredentialBlobSize).decode("utf-16-le")
            if not password:
                raise ProvisionError("测试凭据为空")
            return StoredCredential(username, password)
        finally:
            api.CredFree(pointer)
    if ctypes.get_last_error() != 1168 or not create:
        raise ProvisionError("既有角色凭据无法读取；拒绝自动重置密码")
    password = secrets.token_urlsafe(32)
    raw = password.encode("utf-16-le")
    blob = (ctypes.c_ubyte * len(raw)).from_buffer_copy(raw)
    credential = CREDENTIALW(Type=1, TargetName=target, UserName=username,
                             Comment="Oilwell authorized isolated WP-03 test role",
                             CredentialBlobSize=len(raw), CredentialBlob=blob, Persist=2)
    if not api.CredWriteW(ctypes.byref(credential), 0):
        raise ProvisionError("无法持久保存新测试凭据；未输出密码")
    verified = read_windows_credential(target)
    if verified.username != username or verified.password != password:
        raise ProvisionError("新测试凭据回读核验失败")
    return verified


def _roles(connection, manifest):
    marker = "oilwell-ingest-test:" + manifest["fixture_id"]
    with connection.transaction():
        for role in RUNTIME_ROLES:
            name = manifest["roles"][role]
            row = connection.execute(
                "SELECT oid,rolsuper OR rolcreatedb OR rolcreaterole OR rolreplication OR rolbypassrls,"
                "rolcanlogin,shobj_description(oid,'pg_authid') FROM pg_roles WHERE rolname=%s", (name,)).fetchone()
            if row and (row[1] or not row[2] or row[3] != marker):
                raise ProvisionError("拒绝接管或修复现有非预期测试角色")
            if row and connection.execute("SELECT EXISTS(SELECT 1 FROM pg_auth_members WHERE member=%s OR roleid=%s)",
                                          (row[0], row[0])).fetchone()[0]:
                raise ProvisionError("测试角色已有非预期成员关系")
            secret = _credential(manifest["credential_targets"][role], name, create=row is None)
            if row is None:
                connection.execute(sql.SQL("CREATE ROLE {} LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE "
                                            "NOREPLICATION NOBYPASSRLS PASSWORD {} CONNECTION LIMIT 20").format(
                                                sql.Identifier(name), sql.Literal(secret.password)))
                connection.execute(sql.SQL("COMMENT ON ROLE {} IS {}").format(sql.Identifier(name), sql.Literal(marker)))


def _config(output, identity, manifest):
    sections = {}
    for role in RUNTIME_ROLES:
        sections["postgres_" + role] = {
            "host": "127.0.0.1", "port": 3822, "database": TEST_DATABASE,
            "user": manifest["roles"][role], "sslmode": "disable",
            "password_credential": manifest["credential_targets"][role]}
    sections["postgres_test_admin"] = {
        "host": "127.0.0.1", "port": 3822, "database": TEST_DATABASE,
        "user": identity["roles"]["admin"], "sslmode": "disable",
        "password_credential": identity["credential_targets"]["admin"]}
    path = output / "ingest-test.local.toml"
    if path.exists():
        if tomllib.loads(path.read_text(encoding="utf-8")) != sections:
            raise ProvisionError("现有测试配置有不同内容，拒绝覆盖")
    else:
        text = "\n\n".join("[" + name + "]\n" + "\n".join(
            key + " = " + json.dumps(value, ensure_ascii=False) for key, value in values.items())
                            for name, values in sections.items()) + "\n"
        with path.open("x", encoding="utf-8") as handle:
            handle.write(text)
    return path


def provision(identity, output):
    admin_secret = read_windows_credential(identity["credential_targets"]["admin"])
    if admin_secret.username != identity["roles"]["admin"]:
        raise ProvisionError("隔离管理员凭据身份不匹配")
    settings = dict(host="127.0.0.1", port=3822, dbname="oilwell_registry_test",
                    user=admin_secret.username, password=admin_secret.password, sslmode="disable", connect_timeout=3)
    with psycopg.connect(**settings, autocommit=True) as admin:
        _verify_live(admin, identity, database="oilwell_registry_test")
        admin.execute("SET statement_timeout='15s'")
        admin.execute("SELECT pg_advisory_lock(%s)", (PROVISION_LOCK,))
        existing = admin.execute("SELECT pg_get_userbyid(datdba) FROM pg_database WHERE datname=%s", (TEST_DATABASE,)).fetchone()
        manifest = _fixture_manifest(output, identity, database_exists=existing is not None)
        migrator = manifest["roles"]["migrator"]
        if existing and existing[0] != migrator:
            raise ProvisionError("测试数据库所有者不匹配；拒绝接管")
        # Refuse a user-edited configuration before changing any database ACL.
        # A partial initialization may leave this credential-reference-only file;
        # the owned manifest makes a later explicit retry safe.
        config = _config(output, identity, manifest)
        _roles(admin, manifest)
        database = sql.Identifier(TEST_DATABASE)
        if existing is None:
            admin.execute(sql.SQL("CREATE DATABASE {} OWNER {} ENCODING 'UTF8' TEMPLATE template0").format(
                database, sql.Identifier(migrator)))
            admin.execute(sql.SQL("COMMENT ON DATABASE {} IS {}").format(
                database, sql.Literal("oilwell-ingest-test:" + manifest["fixture_id"])))
        with admin.transaction():
            admin.execute(sql.SQL("REVOKE ALL ON DATABASE {} FROM PUBLIC").format(database))
            admin.execute(sql.SQL("GRANT CONNECT ON DATABASE {} TO {}").format(
                database, sql.SQL(",").join(sql.Identifier(name) for name in manifest["roles"].values())))
        with psycopg.connect(**dict(settings, dbname=TEST_DATABASE)) as target:
            _verify_live(target, identity, database=TEST_DATABASE)
            target.execute("REVOKE CREATE ON SCHEMA public FROM PUBLIC")
            target.execute(sql.SQL("GRANT USAGE,CREATE ON SCHEMA public TO {}").format(sql.Identifier(migrator)))
        return {"status": "prepared", "database": TEST_DATABASE, "host": "127.0.0.1", "port": 3822,
                "created": existing is None, "config": str(config)}


def main(argv=None):
    parser = argparse.ArgumentParser(description="Prepare only the local isolated ingest test database")
    parser.add_argument("--identity", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args(argv)
    try:
        path = Path(args.identity).resolve()
        identity = validate_identity(path, json.loads(path.read_text(encoding="utf-8")))
        output = validate_output(path, args.output)
        result = provision(identity, output)
        print(json.dumps(result, ensure_ascii=False))
        return 0
    except (OSError, ValueError, KeyError, TypeError, psycopg.Error, RuntimeError):
        print(json.dumps({"status": "refused", "message": "隔离测试身份或初始化条件不满足；未接管现有对象"},
                         ensure_ascii=False), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
