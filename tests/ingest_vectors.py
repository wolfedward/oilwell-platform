"""Deterministic software-test inputs, never evidence of field measurements."""

from datetime import datetime, timezone
from hashlib import sha256
import json

from backend.gateway.contracts import TelemetrySample, UplinkBatch
from backend.ingest_reliability.contracts import Observation, Receipt


FIXED_TIME = datetime(2026, 9, 24, tzinfo=timezone.utc)


def observation(message_id="M-1", *, value=41.0, gateway="GW", device="D", quality="good"):
    sample = TelemetrySample(gateway_id=gateway, device_id=device, point_code="load",
                             measured_at=FIXED_TIME, sequence=1, value=value,
                             quality=quality, unit="kN", source="modbus_tcp")
    batch = UplinkBatch.create(gateway, device, "BOOT-1", [sample],
                               message_id=message_id, created_at=FIXED_TIME)
    return Observation(batch.topic("oilfield"), batch.to_json().encode("utf-8"),
                       1, False, False, FIXED_TIME)


def receipt_for(obs, *, status="stored", reason_code="accepted"):
    body = json.loads(obs.payload)
    return Receipt(schema="OWP-INGEST-ACK/1.0",
                   receipt_id="00000000-0000-4000-8000-000000000001",
                   gateway_id=body["gateway_id"], device_id=body["device_id"],
                   message_id=body["message_id"], payload_sha256=sha256(obs.payload).hexdigest(),
                   status=status, reason_code=reason_code,
                   recorded_at=FIXED_TIME.isoformat(timespec="microseconds"))
