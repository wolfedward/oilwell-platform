"""Frame-first, little-endian waveform sample decoding."""

from dataclasses import dataclass
import math
import struct

from waveform_contracts import MAX_I64, StreamSpec, require_count
from waveform_errors import ContractError


@dataclass(frozen=True)
class Sample:
    index: int
    channel_id: str
    raw_value: int | float | None
    quality: int


def validate_quality_bytes(data: bytes):
    """Checks rules independent of the manifest's per-channel encoding."""
    if len(data) % 5:
        raise ContractError("layout_invalid")
    for position in range(0, len(data), 5):
        quality = data[position + 4]
        if quality > 4 or (quality == 1 and data[position:position + 4] != b"\0\0\0\0"):
            raise ContractError("layout_invalid")


def iter_samples(stream: StreamSpec, data: bytes, *, sample_start: int):
    if type(stream) is not StreamSpec or not isinstance(data, bytes):
        raise ContractError("contract_invalid")
    require_count(sample_start)
    if len(data) % stream.frame_bytes or sample_start + len(data) // stream.frame_bytes > stream.frame_count:
        raise ContractError("layout_invalid")
    if stream.first_sample_index + sample_start + len(data) // stream.frame_bytes - 1 > MAX_I64:
        raise ContractError("layout_invalid")
    validate_quality_bytes(data)
    for frame_index in range(len(data) // stream.frame_bytes):
        for channel_index, channel in enumerate(stream.channels):
            position = frame_index * stream.frame_bytes + channel_index * 5
            value_bytes = data[position:position + 4]
            quality = data[position + 4]
            if quality == 1:
                raw_value = None
            elif channel.encoding == "i32le":
                raw_value = struct.unpack("<i", value_bytes)[0]
            else:
                raw_value = struct.unpack("<f", value_bytes)[0]
                if not math.isfinite(raw_value):
                    raise ContractError("layout_invalid")
            yield Sample(stream.first_sample_index + sample_start + frame_index,
                         channel.channel_id, raw_value, quality)
