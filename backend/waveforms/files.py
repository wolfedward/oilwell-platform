"""Bounded exact-byte scanning for one LF-delimited OWP-WAVE capture."""

from dataclasses import dataclass
from hashlib import sha256
from typing import BinaryIO, Iterator

from .contracts import (
    ChunkFrame, ManifestFrame, MAX_CAPTURE_RAW_BYTES, MAX_CHUNK_BYTES,
    MAX_MANIFEST_BYTES,
)
from .errors import ContractError
from .wire import parse_frame


MAX_FILE_BYTES = MAX_CAPTURE_RAW_BYTES + 513


@dataclass(frozen=True)
class FileLine:
    offset: int
    length: int
    sha256: str


@dataclass(frozen=True)
class FileManifest:
    sha256: str
    manifest_sha256: str
    size_bytes: int
    final_lf: bool
    lines: tuple[FileLine, ...]
    manifest: ManifestFrame


def _seek_start(source: BinaryIO) -> None:
    try:
        if not source.seekable():
            raise ContractError("contract_invalid")
        source.seek(0)
    except (AttributeError, OSError, TypeError, ValueError) as error:
        raise ContractError("contract_invalid") from error


def _lines(source: BinaryIO) -> Iterator[tuple[FileLine, bytes, bool]]:
    _seek_start(source)
    offset = 0
    raw_bytes = 0
    index = 0
    while True:
        if index == 513:
            try:
                extra = source.read(1)
            except (OSError, TypeError, ValueError) as error:
                raise ContractError("contract_invalid") from error
            if extra:
                raise ContractError("layout_invalid")
            return
        limit = MAX_MANIFEST_BYTES if index == 0 else MAX_CHUNK_BYTES
        try:
            line = source.readline(limit + 1)
        except (AttributeError, OSError, TypeError, ValueError) as error:
            raise ContractError("contract_invalid") from error
        if not isinstance(line, bytes):
            raise ContractError("contract_invalid")
        if not line:
            return
        has_lf = line.endswith(b"\n")
        payload = line[:-1] if has_lf else line
        if len(payload) > limit:
            raise ContractError("payload_too_large")
        if not payload or b"\r" in payload or b"\n" in payload or (index == 0 and payload.startswith(b"\xef\xbb\xbf")):
            raise ContractError("json_invalid")
        raw_bytes += len(payload)
        offset_after = offset + len(line)
        if raw_bytes > MAX_CAPTURE_RAW_BYTES or offset_after > MAX_FILE_BYTES:
            raise ContractError("payload_too_large")
        yield FileLine(offset, len(payload), sha256(payload).hexdigest()), payload, has_lf
        offset = offset_after
        index += 1


def iter_file_frames(source: BinaryIO) -> Iterator[bytes]:
    """Yield exact payloads in file order, seeking to byte zero first."""
    for _, payload, _ in _lines(source):
        yield payload


def scan_file(source: BinaryIO) -> FileManifest:
    """Validate one complete capture without retaining its decoded samples."""
    _seek_start(source)
    try:
        size = source.seek(0, 2)
    except (OSError, TypeError, ValueError) as error:
        raise ContractError("contract_invalid") from error
    if type(size) is not int or size < 0:
        raise ContractError("contract_invalid")
    if size > MAX_FILE_BYTES:
        raise ContractError("payload_too_large")

    file_hash = sha256()
    records = []
    manifest = None
    streams = {}
    chunks = {}
    offset_after = 0
    final_lf = False
    for line, payload, has_lf in _lines(source):
        file_hash.update(payload)
        if has_lf:
            file_hash.update(b"\n")
        offset_after = line.offset + line.length + int(has_lf)
        final_lf = has_lf
        frame = parse_frame(payload)
        if manifest is None:
            if not isinstance(frame, ManifestFrame):
                raise ContractError("layout_invalid")
            manifest = frame
            streams = {stream.stream_id: stream for stream in manifest.streams}
        else:
            if not isinstance(frame, ChunkFrame):
                raise ContractError("layout_invalid")
            if frame.key != manifest.key or frame.manifest_sha256 != manifest.raw_sha256:
                raise ContractError("reference_conflict")
            stream = streams.get(frame.stream_id)
            if stream is None:
                raise ContractError("reference_conflict")
            key = (frame.stream_id, frame.chunk_index)
            if key in chunks:
                raise ContractError("layout_invalid")
            expected_start = frame.chunk_index * stream.chunk_frames
            expected_count = min(stream.chunk_frames, stream.frame_count - expected_start)
            if (frame.chunk_index >= stream.chunk_count or expected_count <= 0
                    or frame.sample_start != expected_start or frame.sample_count != expected_count
                    or len(frame.data) != expected_count * stream.frame_bytes):
                raise ContractError("layout_invalid")
            chunks[key] = line
        records.append(line)

    if manifest is None or offset_after != size:
        raise ContractError("contract_invalid")
    if len(chunks) != sum(stream.chunk_count for stream in manifest.streams):
        raise ContractError("layout_invalid")

    for stream in manifest.streams:
        digest = sha256()
        for index in range(stream.chunk_count):
            line = chunks[(stream.stream_id, index)]
            try:
                source.seek(line.offset)
                payload = source.read(line.length)
            except (OSError, TypeError, ValueError) as error:
                raise ContractError("contract_invalid") from error
            if not isinstance(payload, bytes) or sha256(payload).hexdigest() != line.sha256:
                raise ContractError("contract_invalid")
            digest.update(parse_frame(payload).data)
        if digest.hexdigest() != stream.data_sha256:
            raise ContractError("digest_mismatch")
    source.seek(size)
    return FileManifest(file_hash.hexdigest(), manifest.raw_sha256, size, final_lf,
                        tuple(records), manifest)
