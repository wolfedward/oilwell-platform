"""Deterministic synthetic wire fixture; never represents field measurement."""

import base64
from functools import lru_cache
from hashlib import sha256
import json
import struct


def encoded(body):
    return json.dumps(body, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


@lru_cache(maxsize=1)
def golden_capture():
    gateway = "GW-001"
    device = "PUMP-001"
    capture = "11111111-1111-4111-8111-111111111111"
    boot = "22222222-2222-4222-8222-222222222222"
    common = {"schema": "OWP-WAVE/1.0", "gateway_id": gateway, "device_id": device, "capture_id": capture}
    streams = []
    payloads = []
    layouts = (
        ("electrical", 8000, 80001, 8000, ("voltage_a", "voltage_b", "voltage_c"), "V"),
        ("sensor", 100, 1001, 1000, ("load", "displacement"), "kN"),
    )
    for stream_id, rate, count, chunk_frames, names, unit in layouts:
        channels = [{"channel_id": name, "quantity": name, "unit": unit if name != "displacement" else "m",
                     "encoding": "i32le", "measurement_source": "synthetic", "signal_kind": "instantaneous",
                     "config_id": "33333333-3333-4333-8333-333333333333", "calibration_id": None}
                    for name in names]
        data = b"".join(struct.pack("<iB", (frame * 7 + offset * 13) % 20001 - 10000, 0)
                        for frame in range(count) for offset in range(len(names)))
        chunk_count = (count + chunk_frames - 1) // chunk_frames
        streams.append({"stream_id": stream_id,
                        "source": {"gateway_id": gateway, "device_id": device, "boot_id": boot},
                        "clock_domain_id": "clock-1", "rate_num": rate, "rate_den": 1,
                        "first_sample_index": "0", "offset_ns": "0", "frame_count": count,
                        "frame_bytes": 5 * len(names), "chunk_frames": chunk_frames,
                        "chunk_count": chunk_count, "decoded_bytes": len(data),
                        "data_sha256": sha256(data).hexdigest(), "channels": channels})
        payloads.append(data)
    manifest = dict(common, kind="manifest", boot_id=boot, capture_kind="cycle", source_kind="synthetic",
                    producer={"manufacturer": "Synthetic Test", "recorder_version": "1.0"}, source_evidence=None,
                    utc_start="2026-09-29T00:00:00.000000Z", utc_uncertainty_ns="0",
                    clock_domain_id="clock-1", sync_evidence_id=None, window_ns="10000000000",
                    cycle={"cycle_id": "44444444-4444-4444-8444-444444444444", "start_ns": "0",
                           "turn_ns": "5000000000", "end_ns": "10000000000", "direction": "clockwise",
                           "zero_reference": "start", "reference_channel": "displacement",
                           "reference_events": [
                               {"stream_id": "sensor", "sample_index": "0", "event": "start"},
                               {"stream_id": "sensor", "sample_index": "500", "event": "turn"},
                               {"stream_id": "sensor", "sample_index": "1000", "event": "end"}]},
                    streams=streams)
    raw = encoded(manifest)
    digest = sha256(raw).hexdigest()
    chunks = []
    for stream, data in zip(streams, payloads):
        for index in range(stream["chunk_count"]):
            first = index * stream["chunk_frames"]
            count = min(stream["chunk_frames"], stream["frame_count"] - first)
            piece = data[first * stream["frame_bytes"]:(first + count) * stream["frame_bytes"]]
            chunks.append(encoded(dict(common, kind="chunk", stream_id=stream["stream_id"], chunk_index=index,
                                       sample_start=first, sample_count=count, manifest_sha256=digest,
                                       data_sha256=sha256(piece).hexdigest(), data_base64=base64.b64encode(piece).decode("ascii"))))
    return raw, tuple(chunks)
