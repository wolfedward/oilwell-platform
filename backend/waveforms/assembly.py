"""Recoverable assembly of immutable, validated slots; no import-time I/O."""
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from time import monotonic
from uuid import UUID, uuid4, uuid5, NAMESPACE_URL

import psycopg
from psycopg.types.json import Jsonb

from .capacity import prepare_transaction, lock_gateway, charge, row
from .config import CapacityLimits
from .contracts import CaptureKey, ManifestFrame, ChunkFrame, WaveReceipt, ACK_SCHEMA
from .errors import ContractError, WaveformError
from .samples import iter_samples
from .wire import parse_frame

LEASE_SECONDS = 30
MAX_LEASE_SECONDS = 300
QUALITY_NAMES = ("good", "missing", "clipped", "bad", "uncertain")


class _LeaseExpired(Exception):
    """Internal savepoint rollback; never a durable input failure."""


@dataclass(frozen=True)
class AssemblyLease:
    key: CaptureKey
    capture_pk: UUID
    worker_id: UUID
    token: UUID
    generation: int
    version: int
    expires_at: datetime
    claimed_at: datetime
    _started: float
    _deadline: float


@dataclass(frozen=True)
class Completion:
    key: CaptureKey
    manifest_sha256: str
    status: str
    reason_code: str
    completed_at: datetime


def _one(cursor, query, params=()):
    cursor.execute(query, params)
    return row(cursor)


def _revision(cursor):
    cursor.execute("UPDATE waveforms.state_head SET version=version+1 WHERE singleton=1")


def _key(capture):
    return CaptureKey(capture["gateway_id"], capture["device_id"], capture["capture_id"])


def _completion(capture, result):
    return Completion(_key(capture), capture["manifest_sha256"], result["status"],
                      result["reason_code"], result["completed_at"])


def _live(lease, now, until):
    elapsed = monotonic()
    return (now is not None and lease.claimed_at <= now < min(until, lease.claimed_at + timedelta(seconds=MAX_LEASE_SECONDS))
            and lease._started <= elapsed < min(lease._deadline, lease._started + MAX_LEASE_SECONDS))


class Assembler:
    def __init__(self, connect, *, clock, limits: CapacityLimits):
        if not callable(connect) or (clock is not None and not callable(clock)) or type(limits) is not CapacityLimits:
            raise ValueError("Explicit connection, UTC clock, and configured capacity are required")
        self._connect, self._clock, self._limits = connect, clock, limits

    def _now(self, cursor):
        if self._clock is None:
            cursor.execute("SELECT clock_timestamp()")
            now = cursor.fetchone()[0].astimezone(timezone.utc)
        else:
            now = self._clock()
        if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() != timedelta(0):
            raise ValueError("Clock must return an aware UTC datetime")
        return now

    def _time(self, cursor, gateway, *, observed=None):
        """Called only after the gateway lock, before capture/job locks."""
        now = self._now(cursor) if observed is None else observed
        health = _one(cursor, "SELECT * FROM waveforms.worker_health WHERE component='assembler' AND gateway_id=%s", (gateway,))
        if health and now < health["last_seen"]:
            cursor.execute("SELECT capture_pk FROM waveforms.assembly_jobs WHERE intake_gateway_id=%s AND lease_token IS NOT NULL ORDER BY capture_pk LIMIT 65", (gateway,))
            active = [value[0] for value in cursor.fetchall()]
            if len(active) > 64:
                raise WaveformError("waveform.invariant", "Assembly ownership bound exceeded")
            # Gateway ownership prevents a concurrent producer or assembler from
            # changing this set. Lock every capture before any job, in UUID order.
            for pk in active:
                cursor.execute("SELECT capture_pk FROM waveforms.captures WHERE capture_pk=%s FOR UPDATE", (pk,))
            for pk in active:
                cursor.execute("SELECT capture_pk FROM waveforms.assembly_jobs WHERE capture_pk=%s FOR UPDATE", (pk,))
                cursor.execute("UPDATE waveforms.assembly_jobs SET lease_token=NULL,lease_generation=NULL,lease_until=NULL,next_attempt_at=greatest(next_attempt_at,%s),last_error_code='clock_rollback',version=version+1 WHERE capture_pk=%s", (health["last_seen"], pk))
            if active or health["error_code"] != "clock_rollback":
                cursor.execute("UPDATE waveforms.worker_health SET last_failure=%s,error_code='clock_rollback' WHERE worker_id=%s", (now, health["worker_id"]))
                _revision(cursor)
            return None
        cursor.execute("INSERT INTO waveforms.worker_health(worker_id,component,gateway_id,last_seen) VALUES(%s,'assembler',%s,%s) ON CONFLICT(component,gateway_id) DO UPDATE SET last_seen=greatest(waveforms.worker_health.last_seen,excluded.last_seen),error_code=NULL",
                       (uuid5(NAMESPACE_URL, "waveforms/assembler/" + gateway), gateway, now))
        if health and health["error_code"] == "clock_rollback":
            _revision(cursor)
        return now

    def claim(self, worker_id: UUID, *, gateway_id: str | None = None):
        if type(worker_id) is not UUID or (gateway_id is not None and gateway_id not in self._limits.gateways):
            raise ValueError("Worker UUID and configured gateway are required")
        gateways = (gateway_id,) if gateway_id is not None else tuple(sorted(self._limits.gateways))
        try:
            for gateway in gateways:
                with self._connect() as connection:
                    cursor = connection.cursor()
                    prepare_transaction(cursor)
                    lock_gateway(cursor, gateway, self._limits)
                    now = self._time(cursor, gateway)
                    if now is None:
                        continue
                    cursor.execute("SELECT capture_pk FROM waveforms.assembly_jobs WHERE intake_gateway_id=%s AND lease_token IS NOT NULL LIMIT 65", (gateway,))
                    outstanding = len(cursor.fetchall())
                    if outstanding > 64:
                        raise WaveformError("waveform.invariant", "Assembly ownership bound exceeded")
                    candidate = _one(cursor, "SELECT capture_pk FROM waveforms.assembly_jobs WHERE intake_gateway_id=%s AND requested_generation>finished_generation AND next_attempt_at<=%s AND (lease_token IS NULL OR lease_until<=%s) AND (%s OR lease_token IS NOT NULL) ORDER BY next_attempt_at,capture_pk LIMIT 1", (gateway, now, now, outstanding < 64))
                    if candidate is None:
                        continue
                    capture = _one(cursor, "SELECT * FROM waveforms.captures WHERE capture_pk=%s FOR UPDATE", (candidate["capture_pk"],))
                    job = _one(cursor, "SELECT * FROM waveforms.assembly_jobs WHERE capture_pk=%s FOR UPDATE", (capture["capture_pk"],))
                    token, started = uuid4(), monotonic()
                    until = now + timedelta(seconds=LEASE_SECONDS)
                    cursor.execute("UPDATE waveforms.assembly_jobs SET lease_token=%s,lease_generation=requested_generation,lease_until=%s,next_attempt_at=%s,attempts=least(attempts,2147483646)+1,last_error_code=NULL,version=version+1 WHERE capture_pk=%s RETURNING version", (token, until, now, capture["capture_pk"]))
                    version = cursor.fetchone()[0]
                    lease = AssemblyLease(_key(capture), capture["capture_pk"], worker_id, token,
                                          job["requested_generation"], version, until, now,
                                          started, started + LEASE_SECONDS)
                return lease
            return None
        except psycopg.Error as error:
            raise WaveformError("waveform.unavailable", "Assembly claim did not return a durable result") from error

    def _locked(self, cursor, lease):
        prepare_transaction(cursor)
        budget = lock_gateway(cursor, lease.key.gateway_id, self._limits)
        now = self._time(cursor, lease.key.gateway_id)
        if now is None:
            return None
        capture = _one(cursor, "SELECT * FROM waveforms.captures WHERE capture_pk=%s FOR UPDATE", (lease.capture_pk,))
        job = _one(cursor, "SELECT * FROM waveforms.assembly_jobs WHERE capture_pk=%s FOR UPDATE", (lease.capture_pk,))
        if (capture is None or job is None or _key(capture) != lease.key
                or (job["lease_token"], job["lease_generation"], job["version"]) != (lease.token, lease.generation, lease.version)
                or job["finished_generation"] >= lease.generation
                or job["next_attempt_at"] != lease.claimed_at
                or not _live(lease, now, job["lease_until"])):
            return None
        return capture, job, budget, now

    def renew(self, lease: AssemblyLease):
        if type(lease) is not AssemblyLease:
            raise ValueError("Expected an AssemblyLease")
        try:
            with self._connect() as connection:
                cursor = connection.cursor()
                locked = self._locked(cursor, lease)
                if locked is None:
                    return None
                _, _, _, now = locked
                until = min(now + timedelta(seconds=LEASE_SECONDS), lease.claimed_at + timedelta(seconds=MAX_LEASE_SECONDS))
                cursor.execute("UPDATE waveforms.assembly_jobs SET lease_until=%s,version=version+1 WHERE capture_pk=%s RETURNING version", (until, lease.capture_pk))
                renewed = replace(lease, expires_at=until, version=cursor.fetchone()[0],
                                  _deadline=min(monotonic() + (until-now).total_seconds(), lease._started + MAX_LEASE_SECONDS))
            return renewed
        except psycopg.Error as error:
            raise WaveformError("waveform.unavailable", "Assembly renewal did not return a durable result") from error

    def _read_fact(self, connection, fact_id):
        # One exact raw fact per execute: ordinary cursors buffer query results.
        with connection.transaction():
            connection.execute("SET LOCAL statement_timeout='15s'")
            record = connection.execute("SELECT raw_payload FROM waveforms.facts WHERE fact_id=%s", (fact_id,)).fetchone()
        if record is None:
            raise WaveformError("waveform.invariant", "Accepted raw fact is missing")
        return bytes(record[0])

    def _verify(self, lease, capture, slots, streams):
        counts = dict.fromkeys(QUALITY_NAMES, 0)
        result = {"quality_counts": counts}
        reason = None
        with self._connect() as connection:
            try:
                manifest = parse_frame(self._read_fact(connection, capture["manifest_fact_id"]))
                if (not isinstance(manifest, ManifestFrame) or manifest.key != lease.key
                        or manifest.raw_sha256 != capture["manifest_sha256"]
                        or len(slots) != sum(stream.chunk_count for stream in manifest.streams)
                        or len(streams) != len(manifest.streams)):
                    return lease, "layout_invalid", result
                by_slot = {(stream_id, index): fact_id for stream_id, index, fact_id in slots}
                indexed = {stream[0]: stream[1:] for stream in streams}
                for stream in sorted(manifest.streams, key=lambda item: item.stream_id):
                    if indexed.get(stream.stream_id) != (capture["manifest_fact_id"], stream.chunk_count, stream.decoded_bytes, stream.data_sha256):
                        return lease, "layout_invalid", result
                    digest = sha256()
                    for index in range(stream.chunk_count):
                        if monotonic() >= lease._deadline - 10:
                            lease = self.renew(lease)
                            if lease is None:
                                return None, None, result
                        fact = by_slot.get((stream.stream_id, index))
                        if fact is None:
                            return lease, "layout_invalid", result
                        frame = parse_frame(self._read_fact(connection, fact))
                        start = index * stream.chunk_frames
                        count = min(stream.chunk_frames, stream.frame_count - start)
                        if (not isinstance(frame, ChunkFrame) or frame.key != manifest.key
                                or frame.manifest_sha256 != manifest.raw_sha256 or frame.stream_id != stream.stream_id
                                or frame.chunk_index != index or frame.sample_start != start
                                or frame.sample_count != count or len(frame.data) != count * stream.frame_bytes):
                            return lease, "layout_invalid", result
                        for sample in iter_samples(stream, frame.data, sample_start=start):
                            counts[QUALITY_NAMES[sample.quality]] += 1
                        digest.update(frame.data)
                        # Release this decoded chunk before parsing the next one.
                        del frame
                    if digest.hexdigest() != stream.data_sha256:
                        reason = "digest_mismatch"
            except ContractError as error:
                reason = "digest_mismatch" if error.code == "digest_mismatch" else "layout_invalid"
        return lease, reason, result

    def _receipt(self, cursor, capture, completion, now):
        eligible = _one(cursor, "SELECT EXISTS(SELECT 1 FROM waveforms.observations o JOIN waveforms.message_outcomes m ON m.outcome_id=o.outcome_id AND m.fact_id=o.fact_id AND m.intake_gateway_id=o.intake_gateway_id WHERE o.outcome_id=%s AND o.fact_id=%s AND o.intake_gateway_id=%s AND o.transport='mqtt' AND m.status='stored' AND m.kind='manifest') AS yes", (capture["manifest_outcome_id"], capture["manifest_fact_id"], capture["intake_gateway_id"]))
        if not eligible["yes"]:
            return
        existing = _one(cursor, "SELECT receipt_id FROM waveforms.receipts WHERE capture_pk=%s", (capture["capture_pk"],))
        if existing:
            return
        message = _one(cursor, "SELECT topic FROM waveforms.receipts WHERE outcome_id=%s AND scope='message'", (capture["manifest_outcome_id"],))
        if message is None:
            raise WaveformError("waveform.invariant", "Accepted MQTT manifest receipt is missing")
        receipt_id = uuid4()
        payload = WaveReceipt(ACK_SCHEMA, receipt_id, completion.key.gateway_id, completion.key.device_id,
                              completion.key.capture_id, "capture", completion.manifest_sha256, None, None, None,
                              completion.status, completion.reason_code,
                              now.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")).to_bytes()
        cursor.execute("INSERT INTO waveforms.receipts(receipt_id,intake_gateway_id,capture_pk,scope,topic,payload,recorded_at) VALUES(%s,%s,%s,'capture',%s,%s,%s)",
                       (receipt_id, capture["intake_gateway_id"], capture["capture_pk"], message["topic"], payload, now))
        cursor.execute("INSERT INTO waveforms.deliveries(receipt_id,intake_gateway_id,requested_at,next_attempt_at) VALUES(%s,%s,%s,%s)", (receipt_id, capture["intake_gateway_id"], now, now))

    def run(self, lease: AssemblyLease):
        """Return only committed results; lost ownership returns None.

        Transient database/commit failures raise waveform.unavailable and leave
        pending work recoverable. No exception is converted to input invalidity.
        """
        if type(lease) is not AssemblyLease:
            raise ValueError("Expected an AssemblyLease")
        try:
            with self._connect() as connection:
                cursor = connection.cursor()
                locked = self._locked(cursor, lease)
                if locked is None:
                    return None
                capture = locked[0]
                terminal = _one(cursor, "SELECT * FROM waveforms.completion WHERE capture_pk=%s", (lease.capture_pk,))
                cursor.execute("SELECT stream_id,chunk_index,fact_id FROM waveforms.chunk_slots WHERE capture_pk=%s ORDER BY stream_id,chunk_index LIMIT 513", (lease.capture_pk,))
                slots = cursor.fetchall()
                cursor.execute("SELECT stream_id,manifest_fact_id,chunk_count,decoded_bytes,data_sha256 FROM waveforms.streams WHERE capture_pk=%s ORDER BY stream_id LIMIT 9", (lease.capture_pk,))
                streams = cursor.fetchall()
            reason, result = None, None
            if terminal is None:
                lease, reason, result = self._verify(lease, capture, slots, streams)
                if lease is None:
                    return None
            with self._connect() as connection:
                cursor = connection.cursor()
                locked = self._locked(cursor, lease)
                if locked is None:
                    return None
                capture, job, budget, now = locked
                try:
                    with connection.transaction():
                        completion = self._seal(cursor, lease, capture, budget, now, reason, result)
                        checked_at = self._now(cursor)
                        if checked_at < now or not _live(lease, checked_at, job["lease_until"]):
                            raise _LeaseExpired()
                        cursor.execute("UPDATE waveforms.worker_health SET last_seen=greatest(last_seen,%s) WHERE component='assembler' AND gateway_id=%s", (checked_at, lease.key.gateway_id))
                except _LeaseExpired:
                    # Release all locks before gateway-wide recovery, so its
                    # ordered capture-before-job lock sequence is preserved.
                    connection.rollback()
                    prepare_transaction(cursor)
                    lock_gateway(cursor, lease.key.gateway_id, self._limits)
                    # Preserve the observed high-water even if the transaction
                    # which first saw it had to be rolled back.
                    cursor.execute("UPDATE waveforms.worker_health SET last_seen=greatest(last_seen,%s) WHERE component='assembler' AND gateway_id=%s", (now, lease.key.gateway_id))
                    self._time(cursor, lease.key.gateway_id, observed=checked_at)
                    return None
            return completion
        except psycopg.Error as error:
            raise WaveformError("waveform.unavailable", "Assembly did not return a durable result") from error

    def _seal(self, cursor, lease, capture, budget, now, reason, result):
        terminal = _one(cursor, "SELECT * FROM waveforms.completion WHERE capture_pk=%s", (lease.capture_pk,))
        if terminal is None:
            anchored = _one(cursor, "SELECT EXISTS(SELECT 1 FROM waveforms.capture_conflicts e WHERE e.capture_pk=%s AND e.reason_code='idempotency_conflict' AND (e.first_fact_id=%s OR EXISTS(SELECT 1 FROM waveforms.chunk_slots s WHERE s.capture_pk=e.capture_pk AND s.fact_id=e.first_fact_id))) AS yes", (lease.capture_pk, capture["manifest_fact_id"]))
            reason = reason or ("idempotency_conflict" if anchored["yes"] else "accepted")
            status = "complete" if reason == "accepted" else "invalid"
            cursor.execute("INSERT INTO waveforms.completion(capture_pk,intake_gateway_id,manifest_fact_id,status,reason_code,result,completed_at) VALUES(%s,%s,%s,%s,%s,%s,%s)",
                           (lease.capture_pk, lease.key.gateway_id, capture["manifest_fact_id"], status, reason, Jsonb(result), now))
            cursor.execute("UPDATE waveforms.captures SET state=%s,reserved_raw_bytes=0,version=version+1 WHERE capture_pk=%s", (status, lease.capture_pk))
            charge(cursor, lease.key.gateway_id, budget, reserved_raw_bytes=-capture["reserved_raw_bytes"], used_incomplete_captures=-1)
            terminal = dict(status=status, reason_code=reason, completed_at=now)
        completion = _completion(capture, terminal)
        self._receipt(cursor, capture, completion, now)
        cursor.execute("UPDATE waveforms.assembly_jobs SET finished_generation=%s,lease_token=NULL,lease_generation=NULL,lease_until=NULL,last_error_code=NULL,version=version+1 WHERE capture_pk=%s", (lease.generation, lease.capture_pk))
        cursor.execute("UPDATE waveforms.worker_health SET last_success=%s WHERE component='assembler' AND gateway_id=%s", (now, lease.key.gateway_id))
        _revision(cursor)
        return completion
