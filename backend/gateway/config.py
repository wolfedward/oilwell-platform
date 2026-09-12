"""Strict TOML loader for the production edge gateway."""

from dataclasses import dataclass
import os
from pathlib import Path
import re
import tomllib

from .contracts import ContractValidationError, PointDefinition
from .mqtt_uplink import MqttConfig, MqttConfigurationError
from .poller import ModbusDevice
from backend.secret_store import SecretStoreError, read_windows_credential


_SAFE_PREFIX = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,63}$")


class GatewayConfigurationError(ValueError):
    pass


@dataclass(frozen=True)
class GatewayConfig:
    gateway_id: str
    spool_database: Path
    poll_interval_seconds: float
    topic_prefix: str
    mqtt: MqttConfig
    devices: tuple[ModbusDevice, ...]


def _resolved_path(value, root: Path) -> str | None:
    if not value:
        return None
    path = Path(value)
    return str(path if path.is_absolute() else (root / path).resolve())


def load_gateway_config(path, *, env=None, project_root=None, credential_reader=None) -> GatewayConfig:
    config_path = Path(path).resolve()
    root = Path(project_root).resolve() if project_root else config_path.parent.parent
    environment = os.environ if env is None else env
    credential_reader = credential_reader or read_windows_credential
    try:
        with config_path.open("rb") as handle:
            raw = tomllib.load(handle)
        gateway = raw["gateway"]
        mqtt_raw = raw["mqtt"]
        devices_raw = raw["modbus"]["devices"]
    except (OSError, KeyError, TypeError, tomllib.TOMLDecodeError) as error:
        raise GatewayConfigurationError(f"cannot load gateway configuration: {error}") from error

    if "password" in mqtt_raw:
        raise GatewayConfigurationError("inline MQTT passwords are prohibited; use password_env")
    password_env = mqtt_raw.get("password_env")
    password_credential = mqtt_raw.get("password_credential")
    if bool(password_env) == bool(password_credential):
        raise GatewayConfigurationError(
            "configure exactly one of mqtt.password_env or mqtt.password_credential"
        )
    if password_env:
        password = environment.get(password_env)
        if password is None:
            raise GatewayConfigurationError(f"required secret environment variable is not set: {password_env}")
    else:
        try:
            credential = credential_reader(password_credential)
        except SecretStoreError as error:
            raise GatewayConfigurationError(str(error)) from error
        if credential.username and credential.username != mqtt_raw.get("username"):
            raise GatewayConfigurationError("MQTT credential username does not match configuration")
        password = credential.password

    gateway_id = gateway.get("id")
    poll_interval = gateway.get("poll_interval_seconds", 5.0)
    topic_prefix = mqtt_raw.get("topic_prefix", "oilfield")
    if not isinstance(gateway_id, str) or any(character in gateway_id for character in "/+#"):
        raise GatewayConfigurationError("gateway.id is invalid")
    if not isinstance(poll_interval, (int, float)) or isinstance(poll_interval, bool) or poll_interval < 0.2:
        raise GatewayConfigurationError("poll_interval_seconds must be at least 0.2")
    if not isinstance(topic_prefix, str) or not _SAFE_PREFIX.fullmatch(topic_prefix):
        raise GatewayConfigurationError("mqtt.topic_prefix is invalid")

    try:
        mqtt = MqttConfig(
            host=mqtt_raw["host"],
            port=mqtt_raw["port"],
            client_id=mqtt_raw.get("client_id", gateway_id),
            username=mqtt_raw.get("username"),
            password=password,
            ca_cert=_resolved_path(mqtt_raw.get("ca_cert"), root),
            client_cert=_resolved_path(mqtt_raw.get("client_cert"), root),
            client_key=_resolved_path(mqtt_raw.get("client_key"), root),
            keepalive_seconds=mqtt_raw.get("keepalive_seconds", 30),
            connect_timeout_seconds=mqtt_raw.get("connect_timeout_seconds", 10.0),
            publish_timeout_seconds=mqtt_raw.get("publish_timeout_seconds", 10.0),
        )
        devices = []
        for item in devices_raw:
            points_raw = item.get("points") or []
            points = tuple(PointDefinition(**point) for point in points_raw)
            device_values = {key: value for key, value in item.items() if key != "points"}
            devices.append(ModbusDevice(points=points, **device_values))
    except (KeyError, TypeError, ValueError, ContractValidationError, MqttConfigurationError) as error:
        raise GatewayConfigurationError(f"gateway configuration is invalid: {error}") from error
    if not devices:
        raise GatewayConfigurationError("at least one Modbus device is required")
    identifiers = [device.device_id for device in devices]
    if len(set(identifiers)) != len(identifiers):
        raise GatewayConfigurationError("modbus device_id values must be unique")

    spool_value = gateway.get("spool_database", "data/uplink-spool.db")
    spool_path = Path(spool_value)
    if not spool_path.is_absolute():
        spool_path = (root / spool_path).resolve()
    return GatewayConfig(
        gateway_id=gateway_id,
        spool_database=spool_path,
        poll_interval_seconds=float(poll_interval),
        topic_prefix=topic_prefix,
        mqtt=mqtt,
        devices=tuple(devices),
    )
