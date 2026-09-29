"""Atomic exact-byte waveform intake. No I/O is performed during import."""
from dataclasses import dataclass, field
from datetime import timezone
from hashlib import sha256
from uuid import UUID, uuid4
import psycopg
from psycopg.pq import TransactionStatus
from psycopg.types.json import Jsonb

from .capacity import prepare_transaction, lock_gateway, charge, row
from .config import CapacityLimits
from .contracts import (Observation, Candidate, ManifestFrame, ChunkFrame, WaveReceipt,
                        ACK_SCHEMA, VALIDATOR_VERSION, MAX_CHUNK_BYTES, MAX_CAPTURE_RAW_BYTES,
                        MAX_I32, MAX_I64, valid_prefix)
from .errors import ContractError, WaveformError
from .samples import iter_samples
from .wire import classify, parse_frame


@dataclass(frozen=True)
class StoredMessage:
    fact_id: UUID
    outcome_id: UUID
    intake_gateway_id: str
    status: str
    reason_code: str
    duplicate: bool
    receipt_id: UUID | None


@dataclass(frozen=True)
class PendingStoredMessage:
    """Transaction-local provenance, never a durable acknowledgment.

    commit_pending owns the outer commit after callers append import lines.
    A whole-transaction rollback, a different transaction, or a failed/uncertain
    commit prevents conversion. Callers must discard pending results from any
    rolled-back savepoint: existing duplicate provenance can survive that
    rollback, so its presence does not prove this particular store survived.
    """
    fact_id: UUID
    outcome_id: UUID
    intake_gateway_id: str
    status: str
    reason_code: str
    duplicate: bool
    receipt_id: UUID | None
    _connection: object = field(repr=False, compare=False)
    _transaction_id: str = field(repr=False)
    _source_ref: UUID = field(repr=False)


def _one(cursor, statement, params=()):
    cursor.execute(statement, params)
    return row(cursor)


def _candidate(observation, prefix, gateway):
    if type(observation) is not Observation:
        raise WaveformError("observation_invalid", "Expected an Observation")
    if len(observation.payload) > MAX_CHUNK_BYTES:
        raise WaveformError("payload_capacity", "Message exceeds the quarantine hard limit")
    if observation.transport == "mqtt":
        if (not isinstance(observation.topic, str) or len(observation.topic.encode("utf-8")) > 65535
                or "\x00" in observation.topic or type(observation.qos) is not int or observation.qos not in (0, 1, 2)
                or type(observation.retain) is not bool or type(observation.dup) is not bool):
            raise WaveformError("observation_invalid", "Invalid MQTT observation shape")
        candidate = classify(observation, prefix=prefix)
    elif observation.transport == "file":
        if any(value is not None for value in (observation.topic, observation.qos, observation.retain, observation.dup)):
            raise WaveformError("observation_invalid", "Invalid file observation shape")
        try:
            frame = parse_frame(observation.payload)
            candidate = Candidate(frame, frame.key, None, VALIDATOR_VERSION)
        except ContractError as error:
            candidate = Candidate(None, None, error.code, VALIDATOR_VERSION)
    else:
        raise WaveformError("observation_invalid", "Unsupported observation transport")
    if candidate.frame and candidate.frame.key.gateway_id != gateway:
        return Candidate(candidate.frame, None, "identity_mismatch", VALIDATOR_VERSION)
    return candidate


def _validate_chunk(frame, manifest):
    if frame.manifest_sha256 != manifest.raw_sha256:
        return "reference_conflict"
    stream = next((s for s in manifest.streams if s.stream_id == frame.stream_id), None)
    if stream is None:
        return "reference_conflict"
    start = frame.chunk_index * stream.chunk_frames
    count = min(stream.chunk_frames, stream.frame_count - start)
    if (frame.chunk_index >= stream.chunk_count or count <= 0 or frame.sample_start != start
            or frame.sample_count != count or len(frame.data) != count * stream.frame_bytes):
        return "layout_invalid"
    try:
        for _ in iter_samples(stream, frame.data, sample_start=start):
            pass
    except ContractError as error:
        return error.code
    return None


class WaveformRepository:
    def __init__(self, connect, *, limits: CapacityLimits, prefix: str, ingress_gateway_id: str):
        if (not callable(connect) or type(limits) is not CapacityLimits or not valid_prefix(prefix)
                or ingress_gateway_id not in limits.gateways):
            raise ValueError("Explicit connection, prefix, and configured intake capacity are required")
        self._connect, self._limits, self._prefix, self._gateway = connect, limits, prefix, ingress_gateway_id

    def ingest(self, observation):
        # Reject programming/API shape failures before acquiring a connection.
        _candidate(observation, self._prefix, self._gateway)
        try:
            with self._connect() as connection:
                pending = self.store_in_transaction(connection.cursor(), observation)
                return self.commit_pending(connection, pending)
        except psycopg.Error as error:
            raise WaveformError("waveform.unavailable", "Waveform transaction did not return a durable result") from error

    def commit_pending(self, connection, pending):
        """Commit one result or a sequence in its original outer transaction.

        Task9 may append import_lines and pass all pending lines together.
        Never call inside a psycopg transaction() context that owns commit.
        Discard results from rolled-back savepoint scopes. The provenance check
        detects missing rows, not rollback of updates to pre-existing duplicates.
        """
        many = isinstance(pending, (list, tuple))
        items = tuple(pending) if many else (pending,)
        if not items or connection.info.transaction_status != TransactionStatus.INTRANS:
            raise WaveformError("waveform.transaction_required", "Pending results require their active transaction")
        with connection.cursor() as cursor:
            cursor.execute("SELECT pg_current_xact_id()::text")
            transaction_id = cursor.fetchone()[0]
            if any(type(item) is not PendingStoredMessage or item._connection is not connection
                   or item._transaction_id != transaction_id for item in items):
                raise WaveformError("waveform.transaction_required", "Pending results belong to another transaction")
            for item in items:
                cursor.execute("SELECT 1 FROM waveforms.observations WHERE outcome_id=%s AND fact_id=%s AND intake_gateway_id=%s AND source_ref=%s",
                               (item.outcome_id, item.fact_id, item.intake_gateway_id, item._source_ref))
                if cursor.fetchone() is None:
                    raise WaveformError("waveform.transaction_required", "Pending provenance was rolled back")
            prepare_transaction(cursor)
        connection.commit()
        results = tuple(StoredMessage(item.fact_id, item.outcome_id, item.intake_gateway_id,
                                     item.status, item.reason_code, item.duplicate, item.receipt_id) for item in items)
        return results if many else results[0]

    def store_in_transaction(self, cursor, observation):
        candidate = _candidate(observation, self._prefix, self._gateway)
        prepare_transaction(cursor)
        # The guard has started the caller-owned outer transaction. The nested
        # savepoint undoes this store even if a caller catches a Python quota
        # error and later commits other work in that outer transaction.
        with cursor.connection.transaction():
            return self._store(cursor, observation, candidate)

    def _store(self, cursor, observation, candidate):
        budget = lock_gateway(cursor, self._gateway, self._limits)
        deltas = dict(used_raw_bytes=0, reserved_raw_bytes=0, used_facts=0,
                      used_incomplete_captures=0, used_orphan_bytes=0, used_quarantine_bytes=0)
        digest, billed = sha256(observation.payload).hexdigest(), max(1024, len(observation.payload))
        fact = _one(cursor, "SELECT * FROM waveforms.facts WHERE intake_gateway_id=%s AND payload_sha256=%s", (self._gateway, digest))
        if fact is not None and bytes(fact["raw_payload"]) != observation.payload:
            raise WaveformError("waveform.digest_collision", "Exact bytes differ for an existing digest")
        new_fact = fact is None
        if new_fact:
            fact_id = uuid4()
            cursor.execute("INSERT INTO waveforms.facts(fact_id,intake_gateway_id,raw_payload,payload_sha256,payload_bytes,received_at) VALUES(%s,%s,%s,%s,%s,%s)",
                           (fact_id, self._gateway, observation.payload, digest, len(observation.payload), observation.received_at))
            deltas.update(used_raw_bytes=billed, used_facts=1)
        else:
            fact_id = fact["fact_id"]
        frame, key, reason = candidate.frame, candidate.key, candidate.reason_code
        capture = manifest = first_fact = None
        if key and reason is None:
            capture = _one(cursor, "SELECT * FROM waveforms.captures WHERE gateway_id=%s AND device_id=%s AND capture_id=%s FOR UPDATE",
                           (key.gateway_id, key.device_id, key.capture_id))
            if capture and capture["manifest_fact_id"]:
                saved = _one(cursor, "SELECT raw_payload FROM waveforms.facts WHERE fact_id=%s", (capture["manifest_fact_id"],))
                manifest = parse_frame(bytes(saved["raw_payload"]))
            if isinstance(frame, ManifestFrame):
                if manifest and capture["manifest_fact_id"] != fact_id:
                    reason, first_fact = "idempotency_conflict", capture["manifest_fact_id"]
            else:
                if frame.chunk_index >= 512:
                    reason = "layout_invalid"
                elif manifest:
                    reason = _validate_chunk(frame, manifest)
                if capture and reason is None:
                    if manifest:
                        # Only validated content can establish the immutable
                        # first accepted slot; invalid orphan evidence cannot.
                        existing = _one(cursor, "SELECT fact_id FROM waveforms.chunk_slots WHERE capture_pk=%s AND stream_id=%s AND chunk_index=%s",
                                        (capture["capture_pk"], frame.stream_id, frame.chunk_index))
                    else:
                        existing = _one(cursor, "SELECT fact_id FROM waveforms.chunks WHERE capture_pk=%s AND manifest_sha256=%s AND stream_id=%s AND chunk_index=%s ORDER BY fact_id LIMIT 1",
                                        (capture["capture_pk"], frame.manifest_sha256, frame.stream_id, frame.chunk_index))
                    if existing and existing["fact_id"] != fact_id:
                        reason, first_fact = "idempotency_conflict", existing["fact_id"]
                    elif not existing:
                        # Retained orphans never spend the accepted manifest's
                        # slot allowance. Before a manifest, bound candidates
                        # separately; their raw/orphan quotas still apply later.
                        if manifest:
                            count = _one(cursor, "SELECT count(*) AS n FROM waveforms.chunk_slots WHERE capture_pk=%s", (capture["capture_pk"],))["n"]
                        else:
                            count = _one(cursor, "SELECT count(*) AS n FROM waveforms.chunks WHERE capture_pk=%s", (capture["capture_pk"],))["n"]
                        if count >= 512:
                            reason = "layout_invalid"
        status, reason = ("quarantined", reason) if reason else ("stored", "accepted")
        outcome_id, new_outcome = self._outcome(cursor, fact_id, frame, key, status, reason, billed, deltas)
        changed = new_fact or new_outcome
        if status == "stored":
            if capture is None:
                capture = dict(capture_pk=uuid4(), manifest_fact_id=None, used_raw_bytes=0,
                               reserved_raw_bytes=0, orphan_bytes=0, state="collecting")
                cursor.execute("INSERT INTO waveforms.captures(capture_pk,intake_gateway_id,gateway_id,device_id,capture_id,first_seen,last_valid_at) VALUES(%s,%s,%s,%s,%s,%s,%s)",
                               (capture["capture_pk"], self._gateway, key.gateway_id, key.device_id, key.capture_id, observation.received_at, observation.received_at))
                deltas["used_incomplete_captures"] += 1
            changed = self._accept(cursor, capture, frame, fact_id, outcome_id, billed, observation, budget, deltas) or changed
        elif capture:
            changed = self._conflict(cursor, capture, fact_id, first_fact, reason, observation.received_at) or changed
        changed = self._observe(cursor, fact_id, outcome_id, observation) or changed
        receipt_id, receipt_changed = self._receipt(cursor, fact_id, outcome_id, candidate, status, reason, digest, observation)
        changed = receipt_changed or changed
        if (status == "stored" and observation.transport == "mqtt" and isinstance(frame, ManifestFrame)
                and capture["state"] in ("complete", "invalid")):
            changed = self._request_capture_receipt(cursor, capture["capture_pk"], observation.received_at) or changed
        charge(cursor, self._gateway, budget, **deltas)
        # Intake, bounded counters, provenance and outbox are one visible revision.
        if changed:
            cursor.execute("UPDATE waveforms.state_head SET version=version+1 WHERE singleton=1")
        cursor.execute("SELECT pg_current_xact_id()::text")
        return PendingStoredMessage(fact_id, outcome_id, self._gateway, status, reason, not new_outcome,
                                    receipt_id, cursor.connection, cursor.fetchone()[0], observation.source_ref)

    def _outcome(self, cursor, fact_id, frame, key, status, reason, billed, deltas):
        # Parsed content identity is deterministic for an immutable outcome.
        # Receipt eligibility remains separately bound to the validated topic.
        key = frame.key if frame and frame.key.gateway_id == self._gateway else None
        existing = _one(cursor, "SELECT outcome_id FROM waveforms.message_outcomes WHERE fact_id=%s AND status=%s AND reason_code=%s AND validator_version=%s",
                        (fact_id, status, reason, VALIDATOR_VERSION))
        if existing:
            return existing["outcome_id"], False
        if status == "quarantined":
            charged = _one(cursor, "SELECT 1 AS found FROM waveforms.message_outcomes WHERE fact_id=%s AND status='quarantined' LIMIT 1", (fact_id,))
            if not charged:
                deltas["used_quarantine_bytes"] += billed
        outcome_id = uuid4()
        cursor.execute("INSERT INTO waveforms.message_outcomes(outcome_id,fact_id,intake_gateway_id,validator_version,status,reason_code,kind,gateway_id,device_id,capture_id) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                       (outcome_id, fact_id, self._gateway, VALIDATOR_VERSION, status, reason,
                        ("manifest" if isinstance(frame, ManifestFrame) else "chunk") if frame else None,
                        key.gateway_id if key else None, key.device_id if key else None, key.capture_id if key else None))
        return outcome_id, True

    def _accept(self, cursor, capture, frame, fact_id, outcome_id, billed, observation, budget, deltas):
        pk = capture["capture_pk"]
        old_reserved, old_orphan = (capture[name] for name in ("reserved_raw_bytes", "orphan_bytes"))
        changed = False
        if isinstance(frame, ManifestFrame):
            if capture["manifest_fact_id"] is None:
                capture["manifest_fact_id"] = fact_id
                capture["used_raw_bytes"] += billed
                capture["reserved_raw_bytes"] = MAX_CAPTURE_RAW_BYTES - capture["used_raw_bytes"]
                cursor.execute("UPDATE waveforms.captures SET manifest_fact_id=%s,manifest_outcome_id=%s,manifest_sha256=%s WHERE capture_pk=%s", (fact_id, outcome_id, frame.raw_sha256, pk))
                for stream, specification in zip(frame.streams, frame.body["streams"]):
                    cursor.execute("INSERT INTO waveforms.streams(capture_pk,intake_gateway_id,stream_id,manifest_fact_id,specification,chunk_count,decoded_bytes,data_sha256) VALUES(%s,%s,%s,%s,%s,%s,%s,%s)",
                                   (pk, self._gateway, stream.stream_id, fact_id, Jsonb(specification), stream.chunk_count, stream.decoded_bytes, stream.data_sha256))
                cursor.execute("SELECT c.fact_id,f.raw_payload,f.billed_bytes FROM waveforms.chunks c JOIN waveforms.facts f USING(fact_id) WHERE c.capture_pk=%s ORDER BY c.stream_id,c.chunk_index,c.fact_id LIMIT 513", (pk,))
                candidates = cursor.fetchall()
                if len(candidates) > 512:
                    raise WaveformError("waveform.invariant", "Capture candidate bound was exceeded")
                for orphan_id, payload, orphan_bill in candidates:
                    orphan = parse_frame(bytes(payload))
                    invalid = _validate_chunk(orphan, frame)
                    if invalid:
                        self._outcome(cursor, orphan_id, orphan, orphan.key, "quarantined", invalid, orphan_bill, deltas)
                        self._conflict(cursor, capture, orphan_id, None, invalid, observation.received_at)
                    else:
                        self._slot(cursor, pk, orphan_id, orphan)
                        capture["orphan_bytes"] -= orphan_bill
                changed = True
        else:
            exists = _one(cursor, "SELECT fact_id FROM waveforms.chunks WHERE fact_id=%s", (fact_id,))
            if not exists:
                cursor.execute("INSERT INTO waveforms.chunks(fact_id,outcome_id,capture_pk,intake_gateway_id,manifest_sha256,stream_id,chunk_index,sample_start,sample_count,decoded_bytes,data_sha256) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                               (fact_id, outcome_id, pk, self._gateway, frame.manifest_sha256, frame.stream_id, frame.chunk_index, frame.sample_start, frame.sample_count, len(frame.data), frame.data_sha256))
                capture["used_raw_bytes"] += billed
                if capture["manifest_fact_id"]:
                    capture["reserved_raw_bytes"] -= billed
                    self._slot(cursor, pk, fact_id, frame)
                else:
                    capture["orphan_bytes"] += billed
                changed = True
        if (capture["used_raw_bytes"] > MAX_CAPTURE_RAW_BYTES or capture["reserved_raw_bytes"] < 0
                or capture["orphan_bytes"] > budget["orphan_bytes_per_capture"]):
            raise WaveformError("waveform.capacity_exhausted", "Capture waveform capacity is exhausted")
        if changed:
            deltas["reserved_raw_bytes"] += capture["reserved_raw_bytes"] - old_reserved
            deltas["used_orphan_bytes"] += capture["orphan_bytes"] - old_orphan
            cursor.execute("UPDATE waveforms.captures SET used_raw_bytes=%s,reserved_raw_bytes=%s,orphan_bytes=%s,last_valid_at=greatest(last_valid_at,%s),version=version+1 WHERE capture_pk=%s",
                           (capture["used_raw_bytes"], capture["reserved_raw_bytes"], capture["orphan_bytes"], observation.received_at, pk))
            if capture["manifest_fact_id"]:
                counts = _one(cursor, "SELECT (SELECT coalesce(sum(chunk_count),0) FROM waveforms.streams WHERE capture_pk=%s) AS expected,(SELECT count(*) FROM waveforms.chunk_slots WHERE capture_pk=%s) AS actual", (pk, pk))
                if counts["expected"] == counts["actual"]:
                    cursor.execute("INSERT INTO waveforms.assembly_jobs(capture_pk,intake_gateway_id,next_attempt_at) VALUES(%s,%s,%s) ON CONFLICT DO NOTHING", (pk, self._gateway, observation.received_at))
        return changed

    def _slot(self, cursor, pk, fact_id, frame):
        # Capture FOR UPDATE (or its just-inserted row) serializes all chunk keys.
        # Immutable unique slot/candidate keys are the final concurrent guard.
        cursor.execute("INSERT INTO waveforms.chunk_slots(capture_pk,intake_gateway_id,stream_id,chunk_index,fact_id) VALUES(%s,%s,%s,%s,%s)",
                       (pk, self._gateway, frame.stream_id, frame.chunk_index, fact_id))

    def _conflict(self, cursor, capture, fact_id, first_fact, reason, recorded_at):
        cursor.execute("INSERT INTO waveforms.capture_conflicts(conflict_id,capture_pk,intake_gateway_id,fact_id,first_fact_id,reason_code,recorded_at) VALUES(%s,%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING",
                       (uuid4(), capture["capture_pk"], self._gateway, fact_id, first_fact, reason, recorded_at))
        return cursor.rowcount > 0

    def _observe(self, cursor, fact_id, outcome_id, observation):
        old = _one(cursor, "SELECT * FROM waveforms.observations WHERE outcome_id=%s AND source_ref=%s", (outcome_id, observation.source_ref))
        if old:
            if old["transport"] != observation.transport:
                raise WaveformError("observation_invalid", "A source identity cannot change transport")
            anomaly = int((old["topic"], old["qos"], old["retain"], old["dup"]) != (observation.topic, observation.qos, observation.retain, observation.dup))
            values = (max(old["last_seen"], observation.received_at), min(MAX_I32, old["duplicate_count"]+1),
                      min(MAX_I32, old["anomaly_count"]+anomaly), old["counters_saturated"]
                      or old["duplicate_count"] >= MAX_I32-1 or old["anomaly_count"]+anomaly >= MAX_I32)
            if values == tuple(old[name] for name in ("last_seen", "duplicate_count", "anomaly_count", "counters_saturated")):
                return False
            cursor.execute("UPDATE waveforms.observations SET last_seen=greatest(last_seen,%s),duplicate_count=%s,anomaly_count=%s,counters_saturated=%s WHERE observation_id=%s",
                           (*values, old["observation_id"]))
        else:
            cursor.execute("INSERT INTO waveforms.observations(observation_id,fact_id,outcome_id,intake_gateway_id,transport,source_ref,topic,qos,retain,dup,first_seen,last_seen) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                           (uuid4(), fact_id, outcome_id, self._gateway, observation.transport, observation.source_ref,
                            observation.topic, observation.qos, observation.retain, observation.dup, observation.received_at, observation.received_at))
        return True

    def _receipt(self, cursor, fact_id, outcome_id, candidate, status, reason, digest, observation):
        key, frame = candidate.key, candidate.frame
        if observation.transport != "mqtt" or key is None:
            return None, False
        existing = _one(cursor, "SELECT receipt_id FROM waveforms.receipts WHERE outcome_id=%s", (outcome_id,))
        if existing:
            receipt_id = existing["receipt_id"]
            changed = self._schedule_delivery(cursor, receipt_id, observation.received_at)
        else:
            receipt_id = uuid4()
            manifest_sha = frame.raw_sha256 if isinstance(frame, ManifestFrame) else frame.manifest_sha256
            chunk = frame if isinstance(frame, ChunkFrame) else None
            receipt = WaveReceipt(ACK_SCHEMA, receipt_id, key.gateway_id, key.device_id, key.capture_id,
                                  "message", manifest_sha, digest, chunk.stream_id if chunk else None,
                                  chunk.chunk_index if chunk else None, status, reason,
                                  observation.received_at.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ"))
            cursor.execute("INSERT INTO waveforms.receipts(receipt_id,intake_gateway_id,fact_id,outcome_id,scope,topic,payload,recorded_at) VALUES(%s,%s,%s,%s,'message',%s,%s,%s)",
                           (receipt_id, self._gateway, fact_id, outcome_id, f"{self._prefix}/v2/{key.gateway_id}/{key.device_id}/waveform-ack", receipt.to_bytes(), observation.received_at))
            cursor.execute("INSERT INTO waveforms.deliveries(receipt_id,intake_gateway_id,requested_at,next_attempt_at) VALUES(%s,%s,%s,%s)", (receipt_id, self._gateway, observation.received_at, observation.received_at))
            changed = True
        return receipt_id, changed

    def _schedule_delivery(self, cursor, receipt_id, received_at):
        delivery = _one(cursor, "SELECT requested_generation,sent_generation FROM waveforms.deliveries WHERE receipt_id=%s FOR UPDATE", (receipt_id,))
        if delivery is None:
            raise WaveformError("waveform.invariant", "Receipt delivery is missing")
        if delivery["sent_generation"] == MAX_I64:
            raise WaveformError("waveform.capacity_exhausted", "Receipt delivery generations are exhausted")
        cursor.execute("UPDATE waveforms.deliveries SET requested_generation=least(requested_generation,%s)+1,requested_at=greatest(requested_at,%s),next_attempt_at=least(next_attempt_at,%s),version=least(version,%s)+1 WHERE receipt_id=%s AND (requested_generation<%s OR requested_at<%s OR next_attempt_at>%s)",
                       (MAX_I64-1, received_at, received_at, MAX_I64-1, receipt_id, MAX_I64, received_at, received_at))
        return cursor.rowcount > 0

    def _request_capture_receipt(self, cursor, capture_pk, received_at):
        """R24: accepted MQTT manifest provenance permits sealed-result ACK work.

        The call site just persisted an MQTT observation referencing the stored
        outcome of this exact accepted manifest. Task5 must check that same
        predicate before constructing a capture receipt from completion.
        """
        receipt = _one(cursor, "SELECT receipt_id FROM waveforms.receipts WHERE capture_pk=%s", (capture_pk,))
        if receipt:
            return self._schedule_delivery(cursor, receipt["receipt_id"], received_at)
        cursor.execute("INSERT INTO waveforms.assembly_jobs(capture_pk,intake_gateway_id,next_attempt_at) VALUES(%s,%s,%s) ON CONFLICT DO NOTHING", (capture_pk, self._gateway, received_at))
        if cursor.rowcount:
            return True
        job = _one(cursor, "SELECT finished_generation FROM waveforms.assembly_jobs WHERE capture_pk=%s FOR UPDATE", (capture_pk,))
        if job["finished_generation"] == MAX_I64:
            raise WaveformError("waveform.capacity_exhausted", "Assembly request generations are exhausted")
        cursor.execute("UPDATE waveforms.assembly_jobs SET requested_generation=requested_generation+1,next_attempt_at=least(next_attempt_at,%s),version=version+1 WHERE capture_pk=%s AND finished_generation=requested_generation AND requested_generation<%s",
                       (received_at, capture_pk, MAX_I64))
        return cursor.rowcount > 0
