"""Test-only pinned, read-only connections for Task 14 catalog observation.

Importing this module does not read credentials or connect. Building the
environment reads existing private test identities/configs and resolves only
their configured reader credentials. No fixture, provisioner, migration,
grant, reset, repair, or test suite is invoked here.
"""

from __future__ import annotations

import json
from pathlib import Path
import re
import sys
import tomllib
from typing import Callable


EXPECTED_SYSTEM_IDENTIFIER = "7688689458460612184"

import psycopg

from backend.center.config import _postgres_settings
from backend.secret_store import read_windows_credential
from tests.inversion_verification_security import AuthorizedInputs, ENDPOINTS, PINNED_CONFIGS, PINNED_IDENTITIES
from tools.provision_waveform_test import (
    OUTPUT, PINNED_IDENTITY, _verify_live, config_sections, load_identity, manifest_for)
from tools.provision_waveform_registry_test import (
    _config as registry_sidecar_config, sidecar_identity as registry_sidecar_identity)
from tools.provision_inversion_test import load_sidecar


def _read_test_config(alias: str) -> dict:
    path = PINNED_CONFIGS[alias]
    return tomllib.loads(path.read_text(encoding="utf-8"))


def build_security_environment() -> tuple[AuthorizedInputs, dict[str, Callable], Callable[[str], None]]:
    """Return exact test inputs, reader factories, and a mandatory live identity gate.

    The gate uses the pre-existing pinned test admin for *read-only* identity
    queries. Catalog snapshots use only each database's configured reader.
    The caller must not treat observations as approved post-fixture baselines.
    """

    parent = load_identity(PINNED_IDENTITY)
    registry_config = _read_test_config("registry")
    expected_keys = {"host", "port", "database", "user", "sslmode", "password_credential"}
    for role in ("migrator", "writer", "reader"):
        section = registry_config["postgres_" + role]
        if (set(section) != expected_keys
                or (section["database"], section["host"], section["port"]) != ENDPOINTS["registry"]
                or section["user"] != parent["roles"][role]
                or section["password_credential"] != parent["credential_targets"][role]
                or section["sslmode"] != "disable"):
            raise ValueError("Parent registry test configuration differs")

    waveform = manifest_for(OUTPUT.resolve(), parent, database_exists=True)
    waveform_config = _read_test_config("waveform")
    if waveform_config != config_sections(parent, waveform):
        raise ValueError("Waveform test configuration differs")

    registry_sidecar = registry_sidecar_identity(waveform)
    if (json.loads(PINNED_IDENTITIES["waveform_registry"].read_text(encoding="utf-8"))
            != registry_sidecar
            or _read_test_config("waveform_registry") != registry_sidecar_config(registry_sidecar)):
        raise ValueError("Waveform registry sidecar differs")

    inversion_parent, inversion_waveform, inversion = load_sidecar(
        expected_system_identifier=EXPECTED_SYSTEM_IDENTIFIER)
    if inversion_parent != parent or inversion_waveform != waveform:
        raise ValueError("Inversion parent identity differs")

    ingest = json.loads(PINNED_IDENTITIES["ingest"].read_text(encoding="utf-8"))
    token = ingest.get("fixture_id")
    if not isinstance(token, str) or not re.fullmatch(r"[0-9a-f]{32}", token):
        raise ValueError("Ingest fixture identity differs")
    ingest_roles = {role: "ingest_test_" + token[:16] + "_" + role
                    for role in ("migrator", "ingestor", "publisher", "reader", "operator")}
    ingest_targets = {role: "oilwell-ingest-test-" + token + "-" + role for role in ingest_roles}
    expected_ingest = dict(
        version=1, fixture_id=token, database=ENDPOINTS["ingest"][0],
        host="127.0.0.1", port=3822, data=str(parent["data"]),
        roles=ingest_roles, credential_targets=ingest_targets,
        admin_target=parent["credential_targets"]["admin"])
    if ingest != expected_ingest:
        raise ValueError("Ingest fixture identity differs")
    ingest_config = _read_test_config("ingest")
    expected_ingest_config = {
        "postgres_" + role: dict(
            host="127.0.0.1", port=3822, database=ENDPOINTS["ingest"][0],
            user=ingest_roles[role], sslmode="disable",
            password_credential=ingest_targets[role])
        for role in ingest_roles}
    expected_ingest_config["postgres_test_admin"] = dict(
        host="127.0.0.1", port=3822, database=ENDPOINTS["ingest"][0],
        user=parent["roles"]["admin"], sslmode="disable",
        password_credential=parent["credential_targets"]["admin"])
    if ingest_config != expected_ingest_config:
        raise ValueError("Ingest test configuration differs")

    reader_sections = {
        "registry": registry_config["postgres_reader"],
        "ingest": ingest_config["postgres_reader"],
        "waveform": waveform_config["postgres_reader"],
    }
    expected_readers = {
        "registry": parent["roles"]["reader"],
        "ingest": ingest_roles["reader"],
        "waveform": waveform["roles"]["reader"],
    }
    reader_settings = {}
    for alias, section in reader_sections.items():
        settings = _postgres_settings(
            section, PINNED_CONFIGS[alias], {}, read_windows_credential) | {"connect_timeout": 3}
        if ((settings["dbname"], settings["host"], settings["port"]) != ENDPOINTS[alias]
                or settings["user"] != expected_readers[alias]
                or settings["user"] == parent["roles"]["admin"]):
            raise ValueError("Unpinned test reader")
        reader_settings[alias] = settings

    role_names = tuple(sorted(set(parent["roles"].values())
                              | set(waveform["roles"].values())
                              | set(ingest_roles.values())
                              | set(inversion["roles"].values())
                              | {registry_sidecar["role"]}))
    authorized = AuthorizedInputs(
        configs=PINNED_CONFIGS, identities=PINNED_IDENTITIES,
        catalog_users=expected_readers, role_names=role_names)
    authorized.validate()

    def connect_reader(alias: str):
        if alias not in ENDPOINTS:
            raise ValueError("Unpinned test database")
        return psycopg.connect(**reader_settings[alias], autocommit=True)

    connectors = {alias: (lambda name=alias: connect_reader(name)) for alias in ENDPOINTS}

    expected_owners_and_markers = {
        "registry": (parent["roles"]["migrator"], None),
        "ingest": (ingest_roles["migrator"], "oilwell-ingest-test:" + token),
        "waveform": (waveform["roles"]["migration_owner"],
                     "oilwell-waveform-test:" + waveform["fixture_id"]),
    }

    def full_identity_gate(alias: str) -> None:
        if alias not in ENDPOINTS:
            raise ValueError("Unpinned test database")
        database, host, port = ENDPOINTS[alias]
        secret = read_windows_credential(parent["credential_targets"]["admin"])
        if secret.username != parent["roles"]["admin"]:
            raise ValueError("Pinned test admin identity differs")
        settings = dict(host=host, port=port, dbname=database, user=secret.username,
                        password=secret.password, sslmode="disable", connect_timeout=3)
        with psycopg.connect(**settings, autocommit=True) as conn:
            with conn.transaction():
                conn.execute("SET TRANSACTION READ ONLY")
                if conn.execute("SELECT system_identifier::text FROM pg_control_system()").fetchone() != (EXPECTED_SYSTEM_IDENTIFIER,):
                    raise RuntimeError("Pinned cluster identifier differs")
                _verify_live(conn, parent, database=database)
                if conn.execute("SELECT current_user").fetchone() != (parent["roles"]["admin"],):
                    raise RuntimeError("Pinned test admin user differs")
                actual = conn.execute(
                    "SELECT pg_get_userbyid(datdba),shobj_description(oid,'pg_database') "
                    "FROM pg_database WHERE datname=current_database()").fetchone()
                if actual != expected_owners_and_markers[alias]:
                    raise RuntimeError("Test database owner or marker differs")

    return authorized, connectors, full_identity_gate


if __name__ == "__main__":
    raise SystemExit("Library only: no database or test suite is started by this file")
