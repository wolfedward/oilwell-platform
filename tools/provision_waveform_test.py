"""Provision only the explicitly authorized WP-04 database on cluster 3822."""
import argparse
import json
from pathlib import Path
import re
import sys
import tomllib
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import psycopg
from psycopg import sql
from backend.secret_store import read_windows_credential
from tools.provision_ingest_test import ProvisionError, _credential, _verify_live
from tools.provision_ingest_test import validate_identity as _validate_cluster_identity

PINNED_IDENTITY = Path("C:/Users/hp/Documents/外专双百/data/registry-test/identity.json")
OUTPUT = Path("C:/Users/hp/Documents/外专双百/data/waveform-test")
TEST_DATABASE = "oilwell_waveforms_test"
ROLES = ("migration_owner", "receiver", "assembler", "receipt_publisher", "reader", "operator")
PROVISION_LOCK = 499001213


def validate_identity(path, raw):
    path = Path(path)
    if not path.is_absolute() or path.resolve() != PINNED_IDENTITY.resolve():
        raise ProvisionError("Waveform provisioning requires the pinned original cluster identity")
    return _validate_cluster_identity(path.resolve(), raw)


def load_identity(path=PINNED_IDENTITY):
    path = Path(path)
    if not path.is_absolute() or path.resolve() != PINNED_IDENTITY.resolve():
        raise ProvisionError("Refusing an unpinned identity before reading it or looking up secrets")
    return validate_identity(path, json.loads(path.read_text(encoding="utf-8")))


def validate_output(output):
    path = Path(output)
    if not path.is_absolute() or path.resolve() != OUTPUT.resolve():
        raise ProvisionError("Only the dedicated waveform-test output is authorized")
    return path.resolve()


def manifest_for(output, identity, *, database_exists):
    path = output / "identity.json"
    raw = json.loads(path.read_text(encoding="utf-8")) if path.exists() else None
    if raw is None:
        if database_exists or (output.exists() and any(output.iterdir())):
            raise ProvisionError("Refusing unowned database or output directory")
        token = uuid4().hex
    else:
        token = raw.get("fixture_id")
        if not isinstance(token, str) or not re.fullmatch(r"[0-9a-f]{32}", token):
            raise ProvisionError("Invalid owned waveform fixture identity")
    expected = dict(version=1, fixture_id=token, database=TEST_DATABASE, host="127.0.0.1", port=3822,
                    data=str(identity["data"]),
                    roles={role: "waveform_test_" + token[:16] + "_" + role for role in ROLES},
                    credential_targets={role: "oilwell-waveform-test-" + token + "-" + role for role in ROLES},
                    admin_target=identity["credential_targets"]["admin"])
    if raw is not None and raw != expected:
        raise ProvisionError("Existing waveform identity differs; refusing takeover")
    if raw is None:
        output.mkdir(parents=False, exist_ok=True)
        with path.open("x", encoding="utf-8") as handle: json.dump(expected, handle, ensure_ascii=False, indent=2)
    return expected


def config_sections(identity, manifest):
    sections = {"postgres_" + role: dict(host="127.0.0.1", port=3822, database=TEST_DATABASE,
                user=manifest["roles"][role], sslmode="disable", password_credential=manifest["credential_targets"][role]) for role in ROLES}
    sections["postgres_test_admin"] = dict(host="127.0.0.1", port=3822, database=TEST_DATABASE,
        user=identity["roles"]["admin"], sslmode="disable", password_credential=identity["credential_targets"]["admin"])
    return sections


def provision(identity_path, output):
    # These gates precede credential lookup and every possible mutation.
    identity, output = load_identity(identity_path), validate_output(output)
    secret = read_windows_credential(identity["credential_targets"]["admin"])
    if secret.username != identity["roles"]["admin"]: raise ProvisionError("Admin credential identity mismatch")
    settings = dict(host="127.0.0.1", port=3822, dbname="oilwell_registry_test", user=secret.username,
                    password=secret.password, sslmode="disable", connect_timeout=3)
    with psycopg.connect(**settings, autocommit=True) as admin:
        _verify_live(admin, identity, database="oilwell_registry_test")
        admin.execute("SET statement_timeout='15s'")
        admin.execute("SELECT pg_advisory_lock(%s)", (PROVISION_LOCK,))
        existing = admin.execute("SELECT pg_get_userbyid(datdba),shobj_description(oid,'pg_database') FROM pg_database WHERE datname=%s", (TEST_DATABASE,)).fetchone()
        manifest = manifest_for(output, identity, database_exists=existing is not None)
        marker, owner = "oilwell-waveform-test:" + manifest["fixture_id"], manifest["roles"]["migration_owner"]
        if existing and existing != (owner, marker): raise ProvisionError("Existing database is not owned by this fixture")
        config_path = output / "waveform-test.local.toml"
        sections = config_sections(identity, manifest)
        if config_path.exists() and tomllib.loads(config_path.read_text(encoding="utf-8")) != sections:
            raise ProvisionError("Refusing changed existing configuration")
        # Validate all existing role identities before creating any new role.
        existing_roles = {}
        for role, name in manifest["roles"].items():
            row = admin.execute("SELECT oid,rolsuper OR rolcreatedb OR rolcreaterole OR rolreplication OR rolbypassrls,rolcanlogin,shobj_description(oid,'pg_authid') FROM pg_roles WHERE rolname=%s", (name,)).fetchone()
            if row and (row[1] or not row[2] or row[3] != marker): raise ProvisionError("Refusing an unowned or elevated role")
            if row and admin.execute("SELECT EXISTS(SELECT 1 FROM pg_auth_members WHERE member=%s OR roleid=%s)", (row[0], row[0])).fetchone()[0]:
                raise ProvisionError("Fixture role has unexpected membership")
            existing_roles[role] = row
        with admin.transaction():
            for role, name in manifest["roles"].items():
                credential = _credential(manifest["credential_targets"][role], name, create=existing_roles[role] is None)
                if existing_roles[role] is None:
                    admin.execute(sql.SQL("CREATE ROLE {} LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS PASSWORD {} CONNECTION LIMIT 20").format(sql.Identifier(name), sql.Literal(credential.password)))
                    admin.execute(sql.SQL("COMMENT ON ROLE {} IS {}").format(sql.Identifier(name), sql.Literal(marker)))
        database = sql.Identifier(TEST_DATABASE)
        if existing is None:
            admin.execute(sql.SQL("CREATE DATABASE {} OWNER {} ENCODING 'UTF8' TEMPLATE template0").format(database, sql.Identifier(owner)))
            admin.execute(sql.SQL("COMMENT ON DATABASE {} IS {}").format(database, sql.Literal(marker)))
        with admin.transaction():
            admin.execute(sql.SQL("REVOKE ALL ON DATABASE {} FROM PUBLIC").format(database))
            admin.execute(sql.SQL("GRANT CONNECT ON DATABASE {} TO {}").format(database, sql.SQL(",").join(map(sql.Identifier, manifest["roles"].values()))))
        with psycopg.connect(**dict(settings, dbname=TEST_DATABASE)) as target:
            _verify_live(target, identity, database=TEST_DATABASE)
            target.execute("REVOKE CREATE ON SCHEMA public FROM PUBLIC")
        if not config_path.exists():
            content = "\n\n".join("["+name+"]\n" + "\n".join(key+" = "+json.dumps(value, ensure_ascii=False) for key,value in values.items()) for name,values in sections.items())+"\n"
            with config_path.open("x", encoding="utf-8") as handle: handle.write(content)
    return dict(status="prepared", database=TEST_DATABASE, host="127.0.0.1", port=3822, created=existing is None)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--identity", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args(argv)
    try:
        print(json.dumps(provision(args.identity, args.output)))
        return 0
    except (OSError, ValueError, KeyError, TypeError, psycopg.Error, RuntimeError):
        print('{"status":"refused","message":"Pinned waveform fixture validation or initialization failed"}', file=sys.stderr)
        return 1


if __name__ == "__main__": raise SystemExit(main())
