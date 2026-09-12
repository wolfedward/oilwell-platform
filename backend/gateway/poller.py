"""Synchronous Modbus TCP/RTU polling behind an injectable client boundary."""

from dataclasses import dataclass
from datetime import datetime, timezone

from .contracts import PointDefinition, Quality, TelemetrySample
from .modbus import build_read_blocks, decode_point


@dataclass(frozen=True)
class ModbusDevice:
    device_id: str
    transport: str
    points: tuple[PointDefinition, ...]
    host: str | None = None
    port: int = 502
    serial_port: str | None = None
    baudrate: int = 19200
    bytesize: int = 8
    parity: str = "N"
    stopbits: int = 1
    timeout_seconds: float = 3.0
    retries: int = 2
    max_gap: int = 0

    def __post_init__(self):
        if not self.device_id or any(character in self.device_id for character in "/+#"):
            raise ValueError("device_id is invalid")
        if self.transport not in ("tcp", "rtu"):
            raise ValueError("transport must be tcp or rtu")
        if self.transport == "tcp" and (not self.host or not 1 <= self.port <= 65535):
            raise ValueError("a TCP endpoint requires host and valid port")
        if self.transport == "rtu" and not self.serial_port:
            raise ValueError("an RTU endpoint requires serial_port")
        if self.timeout_seconds <= 0 or self.retries < 0 or not self.points:
            raise ValueError("polling policy or point map is invalid")
        if len({point.point_code for point in self.points}) != len(self.points):
            raise ValueError("point_code values must be unique per device")


@dataclass(frozen=True)
class PollError:
    code: str
    message: str
    point_codes: tuple[str, ...]
    quality: Quality = Quality.BAD


@dataclass(frozen=True)
class PollResult:
    device_id: str
    measured_at: datetime
    sequence: int
    samples: tuple[TelemetrySample, ...]
    errors: tuple[PollError, ...]


def _default_client_factory(device: ModbusDevice):
    try:
        from pymodbus.client import ModbusSerialClient, ModbusTcpClient
    except ImportError as error:
        raise RuntimeError(
            "pymodbus is not installed; install requirements-production.txt"
        ) from error
    common = {"timeout": device.timeout_seconds, "retries": device.retries}
    if device.transport == "tcp":
        return ModbusTcpClient(device.host, port=device.port, **common)
    return ModbusSerialClient(
        device.serial_port,
        baudrate=device.baudrate,
        bytesize=device.bytesize,
        parity=device.parity,
        stopbits=device.stopbits,
        **common,
    )


class ModbusPoller:
    def __init__(self, gateway_id: str, device: ModbusDevice, *, client_factory=None, clock=None):
        self.gateway_id = gateway_id
        self.device = device
        self.client_factory = client_factory or _default_client_factory
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.blocks = tuple(build_read_blocks(device.points, max_gap=device.max_gap))

    def poll(self, *, sequence: int) -> PollResult:
        measured_at = self.clock()
        if measured_at.tzinfo is None or measured_at.utcoffset() is None:
            raise ValueError("poller clock must be timezone-aware")
        measured_at = measured_at.astimezone(timezone.utc)
        client = self.client_factory(self.device)
        decoded = {}
        errors = []
        try:
            if not client.connect():
                errors.append(PollError(
                    "connection_failed",
                    f"cannot connect to Modbus {self.device.transport} endpoint",
                    tuple(point.point_code for point in self.device.points),
                ))
                return PollResult(self.device.device_id, measured_at, sequence, (), tuple(errors))
            for block in self.blocks:
                method = (
                    client.read_holding_registers
                    if block.function_code == 3
                    else client.read_input_registers
                )
                try:
                    response = method(
                        block.start_address,
                        count=block.quantity,
                        device_id=block.unit_id,
                    )
                    if response is None or response.isError():
                        raise RuntimeError("Modbus exception response")
                    registers = tuple(response.registers)
                    if len(registers) != block.quantity:
                        raise RuntimeError(
                            f"expected {block.quantity} registers, received {len(registers)}"
                        )
                    for point in block.points:
                        offset = point.address - block.start_address
                        value = decode_point(
                            point, registers[offset:offset + point.register_width]
                        )
                        decoded[point.point_code] = TelemetrySample(
                            gateway_id=self.gateway_id,
                            device_id=self.device.device_id,
                            point_code=point.point_code,
                            measured_at=measured_at,
                            sequence=sequence,
                            value=value.value,
                            quality=value.quality,
                            unit=value.unit,
                            source=f"modbus-{self.device.transport}",
                        )
                except Exception as error:
                    errors.append(PollError(
                        "read_failed",
                        str(error),
                        tuple(point.point_code for point in block.points),
                    ))
        except Exception as error:
            errors.append(PollError(
                "client_error",
                str(error),
                tuple(point.point_code for point in self.device.points),
            ))
        finally:
            client.close()
        samples = tuple(decoded[point.point_code] for point in self.device.points if point.point_code in decoded)
        return PollResult(self.device.device_id, measured_at, sequence, samples, tuple(errors))
