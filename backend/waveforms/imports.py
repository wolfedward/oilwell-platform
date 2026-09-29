"""Resumable exact-byte file provenance, committed with receiver raw intake."""
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from tempfile import TemporaryFile
from uuid import UUID, uuid4

import psycopg
from psycopg.types.json import Jsonb

from .capacity import lock_gateway, prepare_transaction, row
from .commands import import_metadata, import_request_hash, lock_import_guard
from .config import CapacityLimits
from .contracts import ManifestFrame, Observation, valid_prefix
from .errors import ContractError, WaveformError
from .files import scan_file
from .repository import WaveformRepository
from .wire import parse_frame


@dataclass(frozen=True)
class ImportSession:
    import_id: UUID
    version: int
    state: str
    missing_lines: tuple[int, ...]


@dataclass(frozen=True)
class ImportProgress(ImportSession):
    fact_id: UUID
    outcome_id: UUID
    receipt_id: None = None


@dataclass(frozen=True)
class ImportResult(ImportSession):
    pass


def _fail(code, message):
    raise WaveformError("waveform.import_"+code, message, 409)


def _one(cursor, query, params=()):
    cursor.execute(query, params)
    return row(cursor)


class ImportService:
    def __init__(self, connect, *, limits: CapacityLimits, prefix: str):
        if not callable(connect) or type(limits) is not CapacityLimits or not valid_prefix(prefix):
            raise ValueError("Explicit receiver connection, capacities and prefix required")
        self._connect, self._limits, self._prefix = connect, limits, prefix

    def create(self, metadata: dict, command) -> ImportSession:
        metadata = import_metadata(metadata)
        return self._execute("create", None, command, metadata=metadata)

    def append(self, id: UUID, line: int, payload: bytes, command) -> ImportProgress:
        return self._execute("append", id, command, line=line, payload=payload)

    def seal(self, id: UUID, command) -> ImportResult:
        return self._execute("seal", id, command)

    def cancel(self, id: UUID, command) -> ImportResult:
        return self._execute("cancel", id, command)

    def _execute(self, operation, target, command, **body):
        digest = import_request_hash(operation, target, command, **body)
        if digest != command.request_hash:
            _fail("request_hash", "Command does not bind the actual request")
        try:
            with self._connect() as conn:
                try:
                    cursor = conn.cursor()
                    prepare_transaction(cursor)
                    actor = command.actor
                    lock_import_guard(cursor, "command", actor.domain, actor.user_id, str(command.key))
                    receipt = _one(cursor, "SELECT request_hash,result FROM waveforms.operation_receipts WHERE actor_domain=%s AND actor_id=%s AND idempotency_key=%s",
                                   (actor.domain, actor.user_id, command.key))
                    if receipt:
                        if receipt["request_hash"] != digest:
                            _fail("idempotency_conflict", "Idempotency key already binds a different request")
                        conn.rollback()
                        return self._decode(receipt["result"])
                    session = None
                    if operation == "create":
                        gateway = body["metadata"]["gateway_id"]
                        creator = asdict(actor)
                    else:
                        session = _one(cursor, "SELECT * FROM waveforms.imports WHERE import_id=%s", (target,))
                        if session is None:
                            _fail("not_found", "Import session not found")
                        gateway, creator = session["intake_gateway_id"], session["actor"]
                    if gateway not in self._limits.gateways:
                        _fail("gateway", "Import gateway has no configured capacity")
                    # One immutable creator guard, even for a different operator.
                    # Order: command -> creator -> gateway -> session -> repository
                    # capture/job -> state_head. Never acquire guards after rows.
                    lock_import_guard(cursor, "creator", creator["domain"], creator["user_id"])
                    lock_gateway(cursor, gateway, self._limits)
                    if session is not None:
                        session = _one(cursor, "SELECT * FROM waveforms.imports WHERE import_id=%s FOR UPDATE", (target,))
                        if session["version"] != command.expected_version:
                            _fail("version", "Import version changed")
                        if session["state"] != "recorded":
                            _fail("terminal", "Import session is terminal")
                    pending = repo = None
                    changed = True
                    if operation == "create":
                        session = self._create(cursor, body["metadata"], command)
                        result = self._result(cursor, session, ImportSession)
                    elif operation == "append":
                        result, pending, repo, changed = self._append(cursor, session, body["line"], body["payload"])
                    else:
                        if operation == "seal":
                            self._validate_file(cursor, session)
                        session = self._advance(cursor, session, "complete" if operation == "seal" else "cancelled")
                        result = self._result(cursor, session, ImportResult)
                    if changed:
                        self._record(cursor, session, operation, command, result)
                    self._receipt(cursor, session, command, digest, result)
                    if pending is not None:
                        repo.commit_pending(conn, pending)
                    else:
                        prepare_transaction(cursor)
                        conn.commit()
                    return result
                except BaseException:
                    # Discard every pending object, including a pre-existing
                    # duplicate whose provenance survives a local savepoint.
                    conn.rollback()
                    raise
        except psycopg.Error as error:
            raise WaveformError("waveform.import_unavailable", "Import transaction did not return a durable result") from error

    def _create(self, cursor, metadata, command):
        actor = command.actor
        cursor.execute("SELECT count(*) FROM waveforms.imports WHERE actor->>'domain'=%s AND actor->>'user_id'=%s AND state='recorded'",
                       (actor.domain, actor.user_id))
        if cursor.fetchone()[0] >= 4:
            _fail("session_limit", "Creator already has four open sessions")
        cursor.execute("SELECT count(*) FROM waveforms.imports WHERE intake_gateway_id=%s AND device_id=%s AND capture_id=%s",
                       (metadata["gateway_id"], metadata["device_id"], metadata["capture_id"]))
        if cursor.fetchone()[0] >= 8:
            _fail("source_limit", "Capture already has eight source sessions")
        return _one(cursor, "INSERT INTO waveforms.imports(import_id,intake_gateway_id,actor,file_sha256,file_bytes,state,line_count,recorded_at,final_lf,device_id,capture_id) VALUES(%s,%s,%s,%s,%s,'recorded',%s,CURRENT_TIMESTAMP,%s,%s,%s) RETURNING *",
                    (uuid4(), metadata["gateway_id"], Jsonb(asdict(actor)), metadata["file_sha256"], metadata["bytes"],
                     metadata["line_count"], metadata["final_lf"], metadata["device_id"], metadata["capture_id"]))

    def _append(self, cursor, session, line, payload):
        if line >= session["line_count"]:
            _fail("line", "Line is outside this file")
        if b"\r" in payload or b"\n" in payload or payload.startswith(b"\xef\xbb\xbf"):
            raise ContractError("json_invalid")
        frame = parse_frame(payload)
        if ((frame.key.gateway_id, frame.key.device_id, frame.key.capture_id) !=
                (session["intake_gateway_id"], session["device_id"], session["capture_id"])):
            _fail("identity", "Uploaded capture differs from persisted session")
        if (line == 0) != isinstance(frame, ManifestFrame):
            raise ContractError("layout_invalid")
        # Sole public zero-based -> SQL one-based conversion.
        line_number = line + 1
        existing = _one(cursor, "SELECT l.fact_id,l.outcome_id,f.raw_payload FROM waveforms.import_lines l JOIN waveforms.facts f USING(fact_id,intake_gateway_id) WHERE l.import_id=%s AND l.line_number=%s",
                        (session["import_id"], line_number))
        if existing:
            if bytes(existing["raw_payload"]) != payload:
                _fail("line_conflict", "Line already contains different exact bytes")
            result = self._result(cursor, session, ImportProgress, existing["fact_id"], existing["outcome_id"])
            return result, None, None, False
        cursor.execute("SELECT coalesce(sum(f.payload_bytes),0) FROM waveforms.import_lines l JOIN waveforms.facts f USING(fact_id,intake_gateway_id) WHERE l.import_id=%s", (session["import_id"],))
        raw_total = cursor.fetchone()[0] + len(payload)
        if raw_total > 33554432 or raw_total + session["line_count"]-1+int(session["final_lf"]) > session["file_bytes"]:
            _fail("file_bytes", "Uploaded bytes exceed declared file size")
        repo = WaveformRepository(self._connect, limits=self._limits, prefix=self._prefix,
                                  ingress_gateway_id=session["intake_gateway_id"])
        pending = repo.store_in_transaction(cursor, Observation("file", None, payload, datetime.now(timezone.utc),
                                                                session["import_id"], None, None, None))
        if pending.status != "stored" or pending.receipt_id is not None:
            _fail("line_rejected", "Uploaded line is not accepted content")
        cursor.execute("INSERT INTO waveforms.import_lines(import_id,intake_gateway_id,line_number,fact_id,outcome_id) VALUES(%s,%s,%s,%s,%s)",
                       (session["import_id"], session["intake_gateway_id"], line_number, pending.fact_id, pending.outcome_id))
        session.update(self._advance(cursor, session, "recorded"))
        return self._result(cursor, session, ImportProgress, pending.fact_id, pending.outcome_id), pending, repo, True

    def _validate_file(self, cursor, session):
        cursor.execute("SELECT line_number,fact_id FROM waveforms.import_lines WHERE import_id=%s ORDER BY line_number", (session["import_id"],))
        lines = cursor.fetchall()
        if [line for line, _ in lines] != list(range(1, session["line_count"]+1)):
            _fail("incomplete", "File still has missing lines")
        # Disk-backed, seekable and bounded: scan_file needs a second pass for
        # stream digests. Fetch only one exact payload at a time from PostgreSQL.
        with TemporaryFile(mode="w+b") as source:
            for line, fact in lines:
                cursor.execute("SELECT raw_payload FROM waveforms.facts WHERE fact_id=%s", (fact,))
                source.write(bytes(cursor.fetchone()[0]))
                if line < session["line_count"] or session["final_lf"]:
                    source.write(b"\n")
            scanned = scan_file(source)
        if (scanned.sha256 != session["file_sha256"] or scanned.size_bytes != session["file_bytes"]
                or scanned.final_lf != session["final_lf"]):
            _fail("file_digest", "Exact file size or digest differs")

    def _advance(self, cursor, session, state):
        return _one(cursor, "UPDATE waveforms.imports SET state=%s,version=version+1 WHERE import_id=%s RETURNING *",
                    (state, session["import_id"]))

    def _result(self, cursor, session, result_type, *extra):
        cursor.execute("SELECT line_number FROM waveforms.import_lines WHERE import_id=%s", (session["import_id"],))
        present = {line-1 for line, in cursor.fetchall()}
        missing = tuple(line for line in range(session["line_count"]) if line not in present)
        state = {"recorded": "pending", "complete": "sealed", "failed": "failed", "cancelled": "cancelled"}[session["state"]]
        return result_type(session["import_id"], session["version"], state, missing, *extra)

    def _record(self, cursor, session, operation, command, result):
        cursor.execute("INSERT INTO waveforms.import_events(import_id,intake_gateway_id,version,state,action,actor,reason) VALUES(%s,%s,%s,%s,%s,%s,%s)",
                       (session["import_id"], session["intake_gateway_id"], session["version"], session["state"], operation, Jsonb(asdict(command.actor)), command.reason))
        cursor.execute("INSERT INTO waveforms.audit(intake_gateway_id,fact_id,actor,action,details) VALUES(%s,%s,%s,%s,%s)",
                       (session["intake_gateway_id"], getattr(result, "fact_id", None), Jsonb(asdict(command.actor)), "imports."+operation,
                        Jsonb({"import_id": str(session["import_id"]), "version": str(session["version"]), "reason": command.reason})))
        cursor.execute("UPDATE waveforms.state_head SET version=version+1 WHERE singleton=1")

    def _receipt(self, cursor, session, command, digest, result):
        data = asdict(result)
        for key, value in data.items():
            if isinstance(value, UUID): data[key] = str(value)
        data["version"] = str(result.version)
        data["type"] = type(result).__name__
        cursor.execute("INSERT INTO waveforms.operation_receipts(intake_gateway_id,actor_domain,actor_id,idempotency_key,request_hash,result) VALUES(%s,%s,%s,%s,%s,%s)",
                       (session["intake_gateway_id"], command.actor.domain, command.actor.user_id, command.key, digest, Jsonb(data)))

    @staticmethod
    def _decode(data):
        data = dict(data)
        result_type = {"ImportSession": ImportSession, "ImportProgress": ImportProgress, "ImportResult": ImportResult}[data.pop("type")]
        for key in ("import_id", "fact_id", "outcome_id"):
            if key in data: data[key] = UUID(data[key])
        data["version"] = int(data["version"])
        data["missing_lines"] = tuple(data["missing_lines"])
        return result_type(**data)
