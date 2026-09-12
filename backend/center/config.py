"""Strict configuration loader for the central MQTT ingestion service."""

from dataclasses import dataclass, field
import ipaddress
import os
from pathlib import Path
import re
import tomllib

from backend.gateway.mqtt_uplink import MqttConfig, MqttConfigurationError
from backend.secret_store import SecretStoreError, read_windows_credential


_SAFE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")


class CenterConfigurationError(ValueError):
    pass


@dataclass(frozen=True)
class CenterConfig:
    topic_prefix: str
    mqtt: MqttConfig
    mqtt_session_expiry_seconds: int
    postgres: dict = field(repr=False)


def _loopback(host: str) -> bool:
    if host.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _secret(section: dict, environment, name: str, username: str, credential_reader) -> str:
    if "password" in section:
        raise CenterConfigurationError(f"inline {name} passwords are prohibited")
    variable = section.get("password_env")
    credential_target = section.get("password_credential")
    if bool(variable) == bool(credential_target):
        raise CenterConfigurationError(
            f"{name} must configure exactly one of password_env or password_credential"
        )
    if variable:
        value = environment.get(variable)
        if value is None:
            raise CenterConfigurationError(f"{name}.password_env is unresolved")
        return value
    try:
        credential = credential_reader(credential_target)
    except SecretStoreError as error:
        raise CenterConfigurationError(str(error)) from error
    if credential.username and credential.username != username:
        raise CenterConfigurationError(f"{name} credential username does not match configuration")
    return credential.password


def load_center_config(path, *, env=None, credential_reader=None) -> CenterConfig:
    config_path = Path(path).resolve()
    environment = os.environ if env is None else env
    credential_reader = credential_reader or read_windows_credential
    try:
        with config_path.open("rb") as handle:
            raw = tomllib.load(handle)
        center = raw["center"]
        mqtt_raw = raw["mqtt"]
        postgres_raw = raw["postgres"]
    except (OSError, KeyError, TypeError, tomllib.TOMLDecodeError) as error:
        raise CenterConfigurationError(f"cannot load center configuration: {error}") from error
    topic_prefix = center.get("topic_prefix", "oilfield")
    if not isinstance(topic_prefix, str) or not _SAFE_NAME.fullmatch(topic_prefix):
        raise CenterConfigurationError("center.topic_prefix is invalid")
    try:
        session_expiry = mqtt_raw.get("session_expiry_seconds", 604800)
        if not isinstance(session_expiry, int) or isinstance(session_expiry, bool) or not 3600 <= session_expiry <= 2_592_000:
            raise CenterConfigurationError(
                "mqtt.session_expiry_seconds must be between one hour and 30 days"
            )
        mqtt = MqttConfig(
            host=mqtt_raw["host"],
            port=mqtt_raw["port"],
            client_id=mqtt_raw["client_id"],
            username=mqtt_raw.get("username"),
            password=_secret(
                mqtt_raw, environment, "mqtt", mqtt_raw.get("username", ""), credential_reader
            ),
            ca_cert=mqtt_raw.get("ca_cert"),
            client_cert=mqtt_raw.get("client_cert"),
            client_key=mqtt_raw.get("client_key"),
            keepalive_seconds=mqtt_raw.get("keepalive_seconds", 30),
            connect_timeout_seconds=mqtt_raw.get("connect_timeout_seconds", 10),
            publish_timeout_seconds=mqtt_raw.get("publish_timeout_seconds", 10),
        )
        pg_host = postgres_raw["host"]
        pg_port = postgres_raw.get("port", 5432)
        pg_database = postgres_raw["database"]
        pg_user = postgres_raw["user"]
        for label, value in (("database", pg_database), ("user", pg_user)):
            if not isinstance(value, str) or not _SAFE_NAME.fullmatch(value):
                raise CenterConfigurationError(f"postgres.{label} is invalid")
        if not isinstance(pg_port, int) or not 1 <= pg_port <= 65535:
            raise CenterConfigurationError("postgres.port is invalid")
        sslmode = postgres_raw.get("sslmode", "verify-full")
        root_cert = postgres_raw.get("sslrootcert")
        if not _loopback(pg_host) and (sslmode != "verify-full" or not root_cert):
            raise CenterConfigurationError(
                "remote PostgreSQL requires sslmode=verify-full and sslrootcert"
            )
        postgres = {
            "host": pg_host,
            "port": pg_port,
            "dbname": pg_database,
            "user": pg_user,
            "password": _secret(
                postgres_raw, environment, "postgres", pg_user, credential_reader
            ),
            "sslmode": sslmode,
        }
        if root_cert:
            root_path = Path(root_cert)
            postgres["sslrootcert"] = str(
                root_path if root_path.is_absolute() else (config_path.parent.parent / root_path).resolve()
            )
    except (KeyError, TypeError, ValueError, MqttConfigurationError) as error:
        if isinstance(error, CenterConfigurationError):
            raise
        raise CenterConfigurationError(f"center configuration is invalid: {error}") from error
    return CenterConfig(
        topic_prefix=topic_prefix,
        mqtt=mqtt,
        mqtt_session_expiry_seconds=session_expiry,
        postgres=postgres,
    )
