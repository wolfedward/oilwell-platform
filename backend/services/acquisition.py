"""Transactional ingestion boundary for virtual DSP+MCU frames."""

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import base64
import hashlib
import json
import math
import queue
import sqlite3
import threading

from backend.protocol import FrameChecksumError, FrameError, decode_frame
from backend.simulator import SimulatorValidationError


SHANGHAI_TIMEZONE = timezone(timedelta(hours=8))
SQLITE_MAX_INTEGER = 9_223_372_036_854_775_807
MAX_GAP_SPAN = 4096
METRICS = {
    "current": (0.0, 200.0, "A"),
    "voltage": (200.0, 500.0, "V"),
    "active_power": (0.0, 100.0, "kW"),
    "power_factor": (0.0, 1.0, None),
    "frequency": (45.0, 55.0, "Hz"),
    "stroke": (0.1, 10.0, "m"),
    "strokes_per_minute": (0.1, 20.0, "1/min"),
    "fillage": (0.0, 1.0, None),
}
TERMINAL_STATES = {"booting", "online", "running", "stopped", "fault", "local_control", "offline"}


class AcquisitionValidationError(ValueError):
    pass


class AcquisitionNotFound(RuntimeError):
    pass


class AcquisitionVersionConflict(RuntimeError):
    pass


class _RejectedFrame(ValueError):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class IngestResult:
    accepted: bool
    status: str
    raw_message_id: int
    device_id: str | None = None
    sequence: int | None = None
    kind: str | None = None
    quality_event_ids: tuple[int, ...] = ()
    error_code: str | None = None


@dataclass(frozen=True)
class ReplayResult:
    device_id: str
    start_sequence: int
    end_sequence: int
    requested: int
    restored: int
    unavailable: tuple[int, ...]
    event_ids: tuple[int, ...]
    status: str


class AcquisitionService:
    """Retain every byte attempt and atomically materialize accepted business data."""

    def __init__(self, database, registry=None, *, clock=None):
        self.database = database
        self.registry = registry
        self._clock = clock or (lambda: datetime.now(SHANGHAI_TIMEZONE))
        self._lock = threading.RLock()

    def now(self):
        value = self._clock()
        if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() != timedelta(hours=8):
            raise AcquisitionValidationError("clock must return an aware +08:00 datetime")
        return value

    def ingest(self, raw: bytes, *, replay_event_id: int | None = None) -> IngestResult:
        if not isinstance(raw, (bytes, bytearray, memoryview)):
            raise AcquisitionValidationError("raw frame must be bytes")
        exact = bytes(raw)
        digest = hashlib.sha256(exact).hexdigest()
        received_at = self.now()
        try:
            frame = decode_frame(exact)
        except FrameError as error:
            code = "checksum_error" if isinstance(error, FrameChecksumError) else "format_error"
            return self._retain_rejection(exact, digest, received_at, code, str(error))

        with self._lock:
            device = self.database.query_one("SELECT id, well_id FROM devices WHERE id=?", (frame.device_id,))
            if device is None:
                return self._retain_rejection(
                    exact, digest, received_at, "unknown_device", "frame device is not registered",
                    claimed_device_id=frame.device_id, sequence=frame.sequence,
                    reported_at=frame.timestamp, kind=self._claimed_kind(frame.payload), payload=frame.payload,
                )
            if frame.sequence > SQLITE_MAX_INTEGER:
                return self._retain_rejection(
                    exact, digest, received_at, "sequence_out_of_range", "sequence exceeds SQLite signed integer range",
                    device_id=frame.device_id, claimed_device_id=frame.device_id, sequence=frame.sequence,
                    reported_at=frame.timestamp, kind=self._claimed_kind(frame.payload), payload=frame.payload,
                )
            try:
                kind = self._validate_frame(frame, received_at)
            except _RejectedFrame as error:
                return self._retain_rejection(
                    exact, digest, received_at, error.code, str(error), device_id=frame.device_id,
                    claimed_device_id=frame.device_id, sequence=frame.sequence,
                    reported_at=frame.timestamp, kind=self._claimed_kind(frame.payload), payload=frame.payload,
                    well_id=device["well_id"],
                )
            return self._accept(exact, digest, received_at, frame, kind, device, replay_event_id)

    def replay(self, device_id: str, start_sequence: int, end_sequence: int) -> ReplayResult:
        if self.registry is None:
            raise AcquisitionValidationError("terminal replay is unavailable")
        if (
            not isinstance(device_id, str) or not device_id
            or not isinstance(start_sequence, int) or isinstance(start_sequence, bool)
            or not isinstance(end_sequence, int) or isinstance(end_sequence, bool)
            or start_sequence < 1 or end_sequence < start_sequence
            or end_sequence > SQLITE_MAX_INTEGER or end_sequence - start_sequence > 1000
        ):
            raise AcquisitionValidationError("replay range is invalid")
        rows = self.database.query_all(
            """SELECT id,missing_sequences FROM data_quality_events
               WHERE device_id=? AND kind='sequence_gap' AND status='open'
                 AND missing_to>=? AND missing_from<=? ORDER BY id""",
            (device_id, start_sequence, end_sequence),
        )
        event_ids = tuple(row["id"] for row in rows)
        sequence_events = {}
        for row in rows:
            for sequence in json.loads(row["missing_sequences"] or "[]"):
                if start_sequence <= sequence <= end_sequence:
                    sequence_events.setdefault(sequence, row["id"])
        requested_sequences = sorted(sequence_events)
        if not requested_sequences:
            repaired = self.database.scalar(
                """SELECT 1 FROM data_quality_events WHERE device_id=? AND kind='sequence_gap'
                   AND status='repaired' AND missing_to>=? AND missing_from<=? LIMIT 1""",
                (device_id, start_sequence, end_sequence),
            )
            return ReplayResult(device_id, start_sequence, end_sequence, 0, 0, (), (), "already_repaired" if repaired else "no_open_gap")
        try:
            archived = self.registry.replay_frames(device_id, min(requested_sequences), max(requested_sequences))
        except SimulatorValidationError as error:
            raise AcquisitionValidationError(str(error)) from None
        restored = 0
        unavailable = []
        for sequence in requested_sequences:
            raw = archived.get(sequence)
            if raw is None:
                unavailable.append(sequence)
                continue
            result = self.ingest(raw, replay_event_id=sequence_events[sequence])
            if result.accepted:
                restored += 1
        remaining = self.database.scalar(
            "SELECT COUNT(*) FROM data_quality_events WHERE id IN (" + ",".join("?" for _ in event_ids) + ") AND status='open'",
            event_ids,
        ) if event_ids else 0
        status = "repaired" if remaining == 0 and not unavailable else "incomplete"
        return ReplayResult(device_id, start_sequence, end_sequence, len(requested_sequences), restored, tuple(unavailable), event_ids, status)

    def stream(self, after_id: int, limit: int):
        rows = self.database.query_all(
            "SELECT * FROM raw_messages WHERE source_type='virtual_device' AND id>? ORDER BY id LIMIT ?",
            (after_id, limit),
        )
        items = [self._message(row, include_raw=False) for row in rows]
        worker = getattr(self.registry, "acquisition_worker", None) if self.registry else None
        return {
            "items": items,
            "cursor": items[-1]["id"] if items else after_id,
            "queue": self.registry.frame_stats() if self.registry else {"queued": 0, "delivered": 0, "dropped": 0},
            "worker": worker.stats() if worker else None,
        }

    def list_messages(self, filters):
        clauses, values = ["source_type IN ('simulator','virtual_device')"], []
        for key, column in (("device_id", "device_id"), ("status", "status"), ("kind", "frame_kind")):
            if filters.get(key):
                clauses.append(f"{column}=?")
                values.append(filters[key])
        page, size = filters["page"], filters["page_size"]
        where = " AND ".join(clauses)
        total = self.database.scalar("SELECT COUNT(*) FROM raw_messages WHERE " + where, values)
        rows = self.database.query_all(
            "SELECT * FROM raw_messages WHERE " + where + " ORDER BY id DESC LIMIT ? OFFSET ?",
            [*values, size, (page - 1) * size],
        )
        return {"items": [self._message(row, include_raw=False) for row in rows], "page": page, "page_size": size, "total": total}

    def message_detail(self, message_id: int):
        row = self.database.query_one(
            "SELECT * FROM raw_messages WHERE id=? AND source_type IN ('simulator','virtual_device')", (message_id,)
        )
        if row is None:
            raise AcquisitionNotFound()
        return self._message(row, include_raw=True)

    def list_quality(self, filters):
        clauses, values = ["source_type IN ('simulator','virtual_device')"], []
        for key, column in (("device_id", "device_id"), ("status", "status"), ("kind", "COALESCE(kind,event_type)")):
            if filters.get(key):
                clauses.append(f"{column}=?")
                values.append(filters[key])
        page, size = filters["page"], filters["page_size"]
        where = " AND ".join(clauses)
        total = self.database.scalar("SELECT COUNT(*) FROM data_quality_events WHERE " + where, values)
        rows = self.database.query_all(
            "SELECT * FROM data_quality_events WHERE " + where + " ORDER BY id DESC LIMIT ? OFFSET ?",
            [*values, size, (page - 1) * size],
        )
        return {"items": [self._quality_item(row) for row in rows], "page": page, "page_size": size, "total": total}

    def repair_event(self, actor_id: int, event_id: int, version: int):
        if not isinstance(version, int) or isinstance(version, bool) or not 1 <= version <= SQLITE_MAX_INTEGER:
            raise AcquisitionValidationError("version is invalid")
        with self._lock:
            row = self.database.query_one(
                "SELECT * FROM data_quality_events WHERE id=? AND source_type='virtual_device'", (event_id,)
            )
            if row is None:
                raise AcquisitionNotFound()
            if row["version"] != version:
                raise AcquisitionVersionConflict()
            if row["kind"] != "sequence_gap" or row["status"] != "open":
                raise AcquisitionValidationError("quality event is not an open sequence gap")
            request_details = json.dumps(
                {"event_id": event_id, "version": version, "missing_from": row["missing_from"], "missing_to": row["missing_to"]},
                sort_keys=True,
            )
            # Persist the operator's request before replay. If this audit
            # boundary fails, no frame or quality state has changed. Each
            # successful replayed frame then closes its gap inside its own
            # acquisition.ingest transaction and audit.
            with self.database.transaction(
                actor_id, "data_quality.repair.request", "data_quality_event", str(event_id),
                source_type="manual", details_json=request_details,
            ):
                pass
            result = self.replay(row["device_id"], row["missing_from"], row["missing_to"])
            return result

    def record_delivery_failure(self, notice: dict) -> int:
        """Persist a bounded simulator/worker handoff-loss summary."""
        if (
            not isinstance(notice, dict)
            or not isinstance(notice.get("dropped_frames"), int)
            or isinstance(notice.get("dropped_frames"), bool)
            or notice["dropped_frames"] < 1
            or not isinstance(notice.get("devices"), list)
        ):
            raise AcquisitionValidationError("delivery failure notice is invalid")
        devices = notice["devices"]
        device_id = devices[0].get("device_id") if len(devices) == 1 and isinstance(devices[0], dict) else None
        device = self.database.query_one("SELECT id,well_id FROM devices WHERE id=?", (device_id,)) if device_id else None
        well_id = device["well_id"] if device else None
        validated_device_id = device["id"] if device else None
        details = json.dumps(notice, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        with self.database.transaction(
            None, "acquisition.delivery_failure", "acquisition_worker", None,
            source_type="virtual_device", details_json=details,
        ) as conn:
            return self._quality(
                conn, "delivery_failure", well_id, validated_device_id, None, notice,
            )

    @staticmethod
    def _message(row, *, include_raw):
        item = {
            "id": row["id"], "device_id": row["device_id"], "claimed_device_id": row["claimed_device_id"],
            "received_at": row["received_at"], "reported_at": row["reported_at"],
            "sequence": row["sequence_text"], "kind": row["frame_kind"], "status": row["status"],
            "digest": row["frame_digest"], "rejection_code": row["rejection_code"],
            "rejection_detail": row["rejection_detail"], "replay_event_id": row["replay_event_id"],
            "source_type": row["source_type"],
        }
        if include_raw:
            item["payload"] = json.loads(row["payload"]) if row["payload"] else None
            item["raw_frame_base64"] = base64.b64encode(bytes(row["raw_frame"] or b"")).decode("ascii")
        return item

    @staticmethod
    def _quality_item(row):
        try:
            detail = json.loads(row["detail"] or "{}")
        except json.JSONDecodeError:
            detail = {"legacy_detail": row["detail"]}
        try:
            missing = json.loads(row["missing_sequences"] or "[]")
        except json.JSONDecodeError:
            missing = []
        return {
            "id": row["id"], "well_id": row["well_id"], "device_id": row["device_id"],
            "raw_message_id": row["raw_message_id"], "kind": row["kind"] or row["event_type"],
            "detail": detail, "missing_from": row["missing_from"], "missing_to": row["missing_to"],
            "missing_sequences": missing, "status": row["status"], "resolution_detail": row["resolution_detail"],
            "resolved_at": row["resolved_at"], "version": row["version"], "created_at": row["created_at"],
            "source_type": row["source_type"],
        }

    def _accept(self, exact, digest, received_at, frame, kind, device, replay_event_id):
        details = json.dumps({"digest": digest, "device_id": frame.device_id, "sequence": str(frame.sequence), "kind": kind}, sort_keys=True)
        quality_ids = []
        with self.database.transaction(None, "acquisition.ingest", "raw_message", digest, source_type="virtual_device", details_json=details) as conn:
            # Retain the attempt first. On SQLite this is also the first write in
            # the transaction, so concurrent service instances serialize here
            # before deciding which attempt owns the business sequence.
            raw_id = self._insert_raw(
                conn, exact, digest, received_at, frame.device_id, frame.device_id, frame.sequence,
                frame.timestamp, kind, frame.payload, "received", replay_event_id=replay_event_id,
            )
            duplicate = conn.execute(
                "SELECT id FROM raw_messages WHERE device_id=? AND sequence_text=? "
                "AND id<>? AND status IN ('accepted','replayed') ORDER BY id LIMIT 1",
                (frame.device_id, str(frame.sequence), raw_id),
            ).fetchone()
            if duplicate:
                conn.execute("UPDATE raw_messages SET status='duplicate' WHERE id=?", (raw_id,))
                quality_ids.append(self._quality(
                    conn, "duplicate", device["well_id"], frame.device_id, raw_id,
                    {"sequence": str(frame.sequence), "accepted_raw_message_id": duplicate["id"]},
                ))
                return IngestResult(False, "duplicate", raw_id, frame.device_id, frame.sequence, kind, tuple(quality_ids), "duplicate")

            cursor = conn.execute("SELECT last_sequence FROM acquisition_cursors WHERE device_id=?", (frame.device_id,)).fetchone()
            last_sequence = cursor["last_sequence"] if cursor else None
            if last_sequence is not None and frame.sequence <= last_sequence:
                gap_rows = conn.execute(
                    "SELECT missing_sequences FROM data_quality_events "
                    "WHERE device_id=? AND kind='sequence_gap' AND status='open' "
                    "AND missing_from<=? AND missing_to>=?",
                    (frame.device_id, frame.sequence, frame.sequence),
                ).fetchall()
                is_known_missing = any(
                    frame.sequence in json.loads(row["missing_sequences"] or "[]")
                    for row in gap_rows
                )
                if not is_known_missing:
                    message = "sequence is behind the durable cursor and is not an open gap"
                    conn.execute(
                        "UPDATE raw_messages SET status='rejected',rejection_code='stale_sequence',rejection_detail=? WHERE id=?",
                        (message, raw_id),
                    )
                    quality_ids.append(self._quality(
                        conn, "stale_sequence", device["well_id"], frame.device_id, raw_id,
                        {"sequence": str(frame.sequence), "last_sequence": last_sequence},
                    ))
                    return IngestResult(
                        False, "rejected", raw_id, frame.device_id, frame.sequence, kind,
                        tuple(quality_ids), "stale_sequence",
                    )
            if last_sequence is not None and frame.sequence > last_sequence + 1:
                gap_size = frame.sequence - last_sequence - 1
                if gap_size > MAX_GAP_SPAN:
                    conn.execute("UPDATE raw_messages SET status='rejected', rejection_code='sequence_jump', rejection_detail=? WHERE id=?", ("sequence jump exceeds replay window", raw_id))
                    quality_ids.append(self._quality(conn, "sequence_jump", device["well_id"], frame.device_id, raw_id, {"from": last_sequence, "to": frame.sequence, "gap_size": gap_size}))
                    return IngestResult(False, "rejected", raw_id, frame.device_id, frame.sequence, kind, tuple(quality_ids), "sequence_jump")
                missing = list(range(last_sequence + 1, frame.sequence))
                quality_ids.append(self._quality(
                    conn, "sequence_gap", device["well_id"], frame.device_id, raw_id,
                    {"missing_sequences": missing}, missing_from=missing[0], missing_to=missing[-1], missing_sequences=missing,
                ))

            skew = received_at - frame.timestamp
            if skew > timedelta(minutes=15):
                quality_ids.append(self._quality(conn, "delay", device["well_id"], frame.device_id, raw_id, {"delay_seconds": round(skew.total_seconds(), 3)}))
            elif abs(skew.total_seconds()) > 30:
                quality_ids.append(self._quality(conn, "clock_drift", device["well_id"], frame.device_id, raw_id, {"skew_seconds": round(skew.total_seconds(), 3)}))

            if kind == "heartbeat":
                conn.execute(
                    "INSERT INTO device_heartbeats(device_id,raw_message_id,reported_at,status,source_type) VALUES(?,?,?,?,'virtual_device')",
                    (frame.device_id, raw_id, frame.timestamp.isoformat(), frame.payload["status"]),
                )
            elif kind == "telemetry":
                for metric, value in frame.payload["telemetry"].items():
                    conn.execute(
                        "INSERT INTO telemetry(well_id, device_id, measured_at, metric_code, value, unit, status, raw_message_id, source_type) VALUES(?, ?, ?, ?, ?, ?, 'valid', ?, 'virtual_device')",
                        (device["well_id"], frame.device_id, frame.timestamp.isoformat(), metric, value, METRICS[metric][2], raw_id),
                    )

            disposition = "replayed" if replay_event_id is not None else "accepted"
            conn.execute("UPDATE raw_messages SET status=? WHERE id=?", (disposition, raw_id))
            if last_sequence is None or frame.sequence > last_sequence:
                conn.execute(
                    "INSERT INTO acquisition_cursors(device_id,last_sequence_text,last_sequence,last_reported_at,source_type) VALUES(?,?,?,?, 'virtual_device') ON CONFLICT(device_id) DO UPDATE SET last_sequence_text=excluded.last_sequence_text,last_sequence=excluded.last_sequence,last_reported_at=excluded.last_reported_at,source_type='virtual_device'",
                    (frame.device_id, str(frame.sequence), frame.sequence, frame.timestamp.isoformat()),
                )
            self._restore_gap_sequence(conn, frame.device_id, frame.sequence, replay_event_id, received_at, quality_ids)
            return IngestResult(True, disposition, raw_id, frame.device_id, frame.sequence, kind, tuple(quality_ids))

    def _retain_rejection(
        self, exact, digest, received_at, code, message, *, device_id=None, claimed_device_id=None,
        sequence=None, reported_at=None, kind=None, payload=None, well_id=None,
    ):
        details = json.dumps({"digest": digest, "code": code, "message": message}, sort_keys=True)
        with self.database.transaction(None, "acquisition.reject", "raw_message", digest, source_type="virtual_device", details_json=details) as conn:
            raw_id = self._insert_raw(
                conn, exact, digest, received_at, device_id, claimed_device_id, sequence,
                reported_at, kind, payload, "rejected", rejection_code=code, rejection_detail=message,
            )
            event_id = self._quality(conn, code, well_id, device_id, raw_id, {"message": message})
        return IngestResult(False, "rejected", raw_id, device_id or claimed_device_id, sequence, kind, (event_id,), code)

    @staticmethod
    def _insert_raw(
        conn, exact, digest, received_at, device_id, claimed_device_id, sequence,
        reported_at, kind, payload, status, *, rejection_code=None, rejection_detail=None, replay_event_id=None,
    ):
        sequence_text = str(sequence) if sequence is not None else None
        sequence_number = sequence if isinstance(sequence, int) and sequence <= SQLITE_MAX_INTEGER else None
        payload_text = "" if payload is None else json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
        return conn.execute(
            """INSERT INTO raw_messages(
                device_id,claimed_device_id,received_at,payload,raw_frame,frame_digest,
                sequence_text,sequence_number,reported_at,frame_kind,rejection_code,rejection_detail,
                replay_event_id,status,source_type
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?, 'virtual_device')""",
            (device_id, claimed_device_id, received_at.isoformat(), payload_text, sqlite3.Binary(exact), digest,
             sequence_text, sequence_number, reported_at.isoformat() if reported_at else None, kind,
             rejection_code, rejection_detail, replay_event_id, status),
        ).lastrowid

    @staticmethod
    def _quality(conn, kind, well_id, device_id, raw_id, detail, *, missing_from=None, missing_to=None, missing_sequences=None):
        detail_text = json.dumps(detail, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        missing_text = None if missing_sequences is None else json.dumps(missing_sequences, separators=(",", ":"))
        return conn.execute(
            """INSERT INTO data_quality_events(
                well_id,device_id,raw_message_id,event_type,kind,detail,missing_from,missing_to,
                missing_sequences,status,source_type
            ) VALUES(?,?,?,?,?,?,?,?,?,'open','virtual_device')""",
            (well_id, device_id, raw_id, kind, kind, detail_text, missing_from, missing_to, missing_text),
        ).lastrowid

    @staticmethod
    def _restore_gap_sequence(conn, device_id, sequence, replay_event_id, received_at, quality_ids):
        rows = conn.execute(
            "SELECT id,missing_sequences,version FROM data_quality_events WHERE device_id=? AND kind='sequence_gap' AND status='open' AND missing_from<=? AND missing_to>=? ORDER BY id",
            (device_id, sequence, sequence),
        ).fetchall()
        for row in rows:
            missing = json.loads(row["missing_sequences"] or "[]")
            if sequence not in missing:
                continue
            missing.remove(sequence)
            if missing:
                conn.execute(
                    "UPDATE data_quality_events SET missing_from=?,missing_to=?,missing_sequences=?,version=version+1 WHERE id=? AND version=?",
                    (min(missing), max(missing), json.dumps(missing, separators=(",", ":")), row["id"], row["version"]),
                )
            else:
                resolution = json.dumps({"restored_by_raw_sequence": sequence, "replay_event_id": replay_event_id}, sort_keys=True)
                conn.execute(
                    "UPDATE data_quality_events SET status='repaired',missing_sequences='[]',resolution_detail=?,resolved_at=?,version=version+1 WHERE id=? AND version=?",
                    (resolution, received_at.isoformat(), row["id"], row["version"]),
                )
            quality_ids.append(row["id"])

    def _validate_frame(self, frame, received_at):
        if frame.timestamp > received_at + timedelta(minutes=5):
            raise _RejectedFrame("timestamp_future", "reported timestamp is too far in the future")
        if frame.timestamp < received_at - timedelta(days=7):
            raise _RejectedFrame("timestamp_out_of_range", "reported timestamp is too old")
        payload = frame.payload
        kind = self._payload_kind(payload)
        common = {"kind", "status", "connection_mode", "hardware_connected"}
        expected = common | ({"telemetry"} if kind == "telemetry" else {"electric_samples", "mechanism"} if kind == "electric_card" else set())
        if set(payload) != expected:
            raise _RejectedFrame("schema_error", "payload fields do not match frame kind")
        if payload["status"] not in TERMINAL_STATES or payload["connection_mode"] != "virtual" or payload["hardware_connected"] is not False:
            raise _RejectedFrame("schema_error", "invalid terminal metadata")
        if kind == "telemetry":
            telemetry = payload["telemetry"]
            if not isinstance(telemetry, dict) or set(telemetry) != set(METRICS):
                raise _RejectedFrame("schema_error", "telemetry fields are invalid")
            for name, value in telemetry.items():
                self._finite_range(value, *METRICS[name][:2], name)
        elif kind == "electric_card":
            samples, mechanism = payload["electric_samples"], payload["mechanism"]
            if not isinstance(samples, list) or not 8 <= len(samples) <= 512:
                raise _RejectedFrame("schema_error", "electric sample count is invalid")
            if not isinstance(mechanism, dict) or set(mechanism) != {"stroke_m", "strokes_per_minute"}:
                raise _RejectedFrame("schema_error", "mechanism fields are invalid")
            self._finite_range(mechanism["stroke_m"], .1, 10, "stroke_m")
            self._finite_range(mechanism["strokes_per_minute"], .1, 20, "strokes_per_minute")
            for sample in samples:
                if not isinstance(sample, dict) or set(sample) != {"phase", "current", "voltage", "power"}:
                    raise _RejectedFrame("schema_error", "electric sample fields are invalid")
                self._finite_range(sample["phase"], 0, 359.999999, "phase")
                self._finite_range(sample["current"], 0, 200, "current")
                self._finite_range(sample["voltage"], 200, 500, "voltage")
                self._finite_range(sample["power"], 0, 100, "power")
        return kind

    @staticmethod
    def _payload_kind(payload):
        kind = payload.get("kind") if isinstance(payload, dict) else None
        if kind not in {"heartbeat", "telemetry", "electric_card"}:
            raise _RejectedFrame("schema_error", "unknown frame kind")
        return kind

    @staticmethod
    def _claimed_kind(payload):
        return payload.get("kind") if isinstance(payload, dict) and isinstance(payload.get("kind"), str) else None

    @staticmethod
    def _finite_range(value, minimum, maximum, name):
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
            raise _RejectedFrame("range_outlier", f"{name} must be finite numeric data")
        if not minimum <= value <= maximum:
            raise _RejectedFrame("range_outlier", f"{name} is outside the engineering range")


class AcquisitionWorker:
    """Owned, stoppable consumer for simulator mailbox and fallback frames."""

    def __init__(self, service: AcquisitionService, registry, *, mailbox_size=64, poll_seconds=.1):
        if not isinstance(mailbox_size, int) or isinstance(mailbox_size, bool) or mailbox_size < 1:
            raise ValueError("mailbox_size must be a positive integer")
        if not isinstance(poll_seconds, (int, float)) or isinstance(poll_seconds, bool) or poll_seconds <= 0:
            raise ValueError("poll_seconds must be positive")
        self.service, self.registry, self.poll_seconds = service, registry, float(poll_seconds)
        self.mailbox = queue.Queue(maxsize=mailbox_size)
        self.registry.set_frame_sink(self.mailbox)
        self._stop = threading.Event()
        self._thread = None
        self._stats_lock = threading.Lock()
        self._stats = {"accepted": 0, "rejected": 0, "failed": 0, "batches": 0}

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self.registry.set_frame_sink(self.mailbox)
        self._thread = threading.Thread(target=self._run, name="acquisition-worker", daemon=False)
        self._thread.start()

    def stop(self):
        self.registry.set_frame_sink(None)
        self._stop.set()
        thread = self._thread
        if thread and thread is not threading.current_thread():
            # Database work is bounded by SQLite's five-second busy timeout.
            # Allow one in-flight frame and its failure audit to finish, while
            # the worker abandons the rest of a slow batch with a durable
            # delivery-failure summary between frames.
            thread.join(timeout=15)
            if thread.is_alive():
                raise RuntimeError("acquisition worker did not stop")

    def is_alive(self):
        return bool(self._thread and self._thread.is_alive())

    def stats(self):
        with self._stats_lock:
            return {
                **self._stats,
                "mailbox_queued": self.mailbox.qsize(),
                "mailbox_capacity": self.mailbox.maxsize,
            }

    def _run(self):
        while True:
            batch = ()
            try:
                batch = self.mailbox.get(timeout=self.poll_seconds)
            except queue.Empty:
                pass
            if batch:
                try:
                    self._process_batch(batch)
                finally:
                    self.mailbox.task_done()
            fallback = self.registry.drain_frames()
            if fallback:
                self._process_batch(fallback)
            for notice in self.registry.drain_delivery_failures():
                self._record_delivery_notice(notice)
            registry_stats = self.registry.frame_stats()
            if (
                self._stop.is_set() and self.mailbox.empty()
                and not registry_stats["queued"]
                and not registry_stats["delivery_failures_pending"]
            ):
                return

    def _process_batch(self, batch):
        with self._stats_lock:
            self._stats["batches"] += 1
        for index, raw in enumerate(batch):
            if self._stop.is_set():
                notice = self.registry.summarize_delivery_failure(
                    batch[index:], "worker_shutdown",
                )
                self._record_delivery_notice(notice)
                return
            try:
                result = self.service.ingest(raw)
            except Exception as error:
                with self._stats_lock:
                    self._stats["failed"] += 1
                self._record_failure(error)
                continue
            with self._stats_lock:
                self._stats["accepted" if result.accepted else "rejected"] += 1

    def _record_delivery_notice(self, notice):
        try:
            self.service.record_delivery_failure(notice)
        except Exception as error:
            with self._stats_lock:
                self._stats["failed"] += 1
            self._record_failure(error)

    def _record_failure(self, error):
        try:
            detail = str(error)[:1000]
            with self.service.database.transaction(None, "acquisition.worker_error", "acquisition_worker", None, source_type="system") as conn:
                conn.execute(
                    "INSERT INTO system_events(event_type,detail,status,source_type) VALUES('acquisition_worker_error',?,'open','system')",
                    (detail,),
                )
        except Exception:
            pass
