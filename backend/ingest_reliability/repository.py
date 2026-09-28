"""Atomic receipt-aware acceptance and permanent-input isolation."""

from datetime import timezone
from hashlib import sha256
from uuid import uuid4

import psycopg

from backend.center.postgres import IdempotencyConflict, insert_batch
from .contracts import CapacityLimits, CommittedResult, Receipt, valid_name, valid_prefix
from .errors import ReliabilityError
from .facts import MAX_COUNTER, schedule_receipt
from .wire import classify


FACT_FIELDS = ("fact_id", "topic", "raw_payload", "payload_sha256", "status", "reason_code",
               "gateway_id", "device_id", "message_id")


class ReliableRepository:
    def __init__(self, connect, *, prefix, receipt_gateways, limits):
        if not callable(connect) or not valid_prefix(prefix) or not isinstance(limits, CapacityLimits):
            raise ValueError("invalid reliable receiver configuration")
        if not isinstance(receipt_gateways, frozenset) or any(not valid_name(item) for item in receipt_gateways):
            raise ValueError("receipt gateways must be an explicit frozenset of valid names")
        self.connect, self.prefix = connect, prefix
        self.receipt_gateways, self.limits = receipt_gateways, limits

    def ingest(self, observation):
        candidate = classify(observation, prefix=self.prefix)
        try:
            with self.connect() as conn:
                with conn.cursor() as cur:
                    cur.execute("SET LOCAL lock_timeout='1s'")
                    cur.execute("SET LOCAL statement_timeout='3s'")
                    cur.execute("SET LOCAL idle_in_transaction_session_timeout='10s'")
                    cur.execute("SET LOCAL search_path=pg_catalog,public,pg_temp")
                    cur.execute("SET LOCAL timezone='UTC'")
                    cur.execute("SELECT total_bytes,quarantine_bytes,used_total_bytes,used_quarantine_bytes "
                                "FROM ingest_reliability.capacity WHERE singleton=1 FOR UPDATE")
                    capacity = cur.fetchone()
                    if capacity is None:
                        raise ReliabilityError("ingest.capacity_missing", "接收容量配置缺失")
                    if capacity[:2] != (self.limits.total_bytes, self.limits.quarantine_bytes):
                        raise ReliabilityError("ingest.capacity_mismatch", "接收容量配置与已部署限额不一致")
                    result = self._store_locked(cur, observation, candidate, capacity)
            # A commit exception, including an uncertain outcome, must prevent
            # callers from treating this as durable success and issuing ACK.
            return result
        except psycopg.Error:
            raise ReliabilityError("ingest.storage_unavailable", "中心持久保存未确认成功，请保留原报文重试") from None

    @staticmethod
    def _legacy(cur, correlation):
        if correlation is None:
            return None
        cur.execute("SELECT topic,gateway_id,device_id,payload_sha256 FROM public.ingest_messages WHERE message_id=%s",
                    (correlation.message_id,))
        return cur.fetchone()

    @staticmethod
    def _legacy_matches(row, observation, correlation, digest):
        return correlation is not None and row == (observation.topic, correlation.gateway_id, correlation.device_id, digest)

    @staticmethod
    def _observe_again(cur, fact, observation):
        anomalous = observation.qos != 1 or observation.retain
        cur.execute(
            "UPDATE ingest_reliability.fact_observations SET last_seen=greatest(last_seen,%s),"
            "duplicate_count=CASE WHEN duplicate_count=%s THEN duplicate_count ELSE duplicate_count+1 END,"
            "transport_anomaly_count=CASE WHEN %s AND transport_anomaly_count<%s THEN transport_anomaly_count+1 ELSE transport_anomaly_count END,"
            "counters_saturated=counters_saturated OR duplicate_count>=%s OR (%s AND transport_anomaly_count>=%s) WHERE fact_id=%s",
            (observation.received_at, MAX_COUNTER, anomalous, MAX_COUNTER, MAX_COUNTER-1, anomalous, MAX_COUNTER-1, fact["fact_id"]))
        if cur.rowcount != 1:
            raise ReliabilityError("ingest.fact_incomplete", "既有接收事实缺少观察记录，未确认本次报文")
        if fact["status"] == "quarantined":
            cur.execute("SELECT ingest_reliability.reopen_repeated_case(%s,%s)", (fact["fact_id"], observation.received_at))
            cur.fetchone()

    def _receipt(self, cur, fact, instant):
        cur.execute("SELECT receipt_id FROM ingest_reliability.receipts WHERE fact_id=%s", (fact["fact_id"],))
        existing = cur.fetchone()
        if existing:
            receipt_id = existing[0]
        elif fact["gateway_id"] is not None and fact["gateway_id"] in self.receipt_gateways:
            receipt_id = uuid4()
            receipt = Receipt(
                schema="OWP-INGEST-ACK/1.0", receipt_id=str(receipt_id), gateway_id=fact["gateway_id"],
                device_id=fact["device_id"], message_id=fact["message_id"], payload_sha256=fact["payload_sha256"],
                status=fact["status"], reason_code=fact["reason_code"],
                recorded_at=instant.astimezone(timezone.utc).isoformat(timespec="microseconds"))
            cur.execute("INSERT INTO ingest_reliability.receipts "
                        "(receipt_id,fact_id,topic,payload,gateway_id,device_id,message_id,payload_sha256,status,reason_code,recorded_at) "
                        "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                        (receipt_id, fact["fact_id"], receipt.topic(self.prefix), receipt.to_bytes(), receipt.gateway_id,
                         receipt.device_id, receipt.message_id, receipt.payload_sha256, receipt.status, receipt.reason_code, instant))
        else:
            return None
        schedule_receipt(cur, receipt_id, instant)
        return receipt_id

    def _store_locked(self, cur, observation, candidate, capacity):
        digest = sha256(observation.payload).hexdigest()
        topic_digest = sha256(observation.topic.encode("utf-8")).hexdigest()
        cur.execute("SELECT " + ",".join(FACT_FIELDS) + " FROM ingest_reliability.facts WHERE topic_sha256=%s AND payload_sha256=%s",
                    (topic_digest, digest))
        row = cur.fetchone()
        cur.execute("SELECT clock_timestamp()")
        instant = cur.fetchone()[0]
        if row is not None:
            fact = dict(zip(FACT_FIELDS, row))
            if fact["topic"] != observation.topic or fact["raw_payload"] != observation.payload:
                raise ReliabilityError("ingest.digest_collision", "原文摘要匹配但精确内容不一致，已停止确认")
            self._observe_again(cur, fact, observation)
            receipt_id = self._receipt(cur, fact, instant)
            return CommittedResult(fact["fact_id"], fact["status"], receipt_id, True)

        correlation, reason = candidate.correlation, candidate.reason_code
        legacy = self._legacy(cur, correlation)
        if self._legacy_matches(legacy, observation, correlation, digest):
            # This proves a prior acceptance even if this observation has changed
            # QoS/retain flags, or the current validator has grown stricter.
            reason = None
        elif legacy is not None:
            reason = reason or "idempotency_conflict"
        elif reason is None:
            if candidate.batch is None or correlation is None:
                raise ReliabilityError("ingest.classifier_invariant", "输入校验结果不完整，未确认接收")
            try:
                inserted = insert_batch(cur, candidate.batch, topic=observation.topic, payload_sha256=digest,
                                        raw_payload=observation.payload, received_at=observation.received_at)
                # Legacy ingestion does not acquire the new capacity lock. It
                # can win this ID concurrently, so recheck its identity too.
                if not inserted and not self._legacy_matches(self._legacy(cur, correlation), observation, correlation, digest):
                    reason = "idempotency_conflict"
            except IdempotencyConflict:
                reason = "idempotency_conflict"

        status = "quarantined" if reason else "stored"
        reason = reason or "accepted"
        charge = max(1, len(observation.payload))
        quarantine_charge = charge if status == "quarantined" else 0
        if capacity[2] + charge > capacity[0] or capacity[3] + quarantine_charge > capacity[1]:
            raise ReliabilityError("ingest.capacity_exhausted", "中心原文保存额度不足，未确认本次报文")
        cur.execute("UPDATE ingest_reliability.capacity SET used_total_bytes=used_total_bytes+%s,"
                    "used_quarantine_bytes=used_quarantine_bytes+%s WHERE singleton=1", (charge, quarantine_charge))
        fact_id = uuid4()
        gateway_id, device_id, message_id = ((correlation.gateway_id, correlation.device_id, correlation.message_id)
                                            if correlation else (None, None, None))
        cur.execute("INSERT INTO ingest_reliability.facts "
                    "(fact_id,topic,topic_sha256,raw_payload,payload_sha256,payload_bytes,qos,retain,dup,received_at,"
                    "validator_version,status,reason_code,accepted_message_id,gateway_id,device_id,message_id) "
                    "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                    (fact_id, observation.topic, topic_digest, observation.payload, digest, len(observation.payload),
                     observation.qos, observation.retain, observation.dup, observation.received_at, candidate.validator_version,
                     status, reason, message_id if status == "stored" else None, gateway_id, device_id, message_id))
        cur.execute("INSERT INTO ingest_reliability.fact_observations(fact_id,first_seen,last_seen,transport_anomaly_count) "
                    "VALUES (%s,%s,%s,%s)", (fact_id, observation.received_at, observation.received_at,
                                              int(observation.qos != 1 or observation.retain)))
        if status == "quarantined":
            cur.execute("INSERT INTO ingest_reliability.cases(fact_id,updated_at) VALUES (%s,%s)", (fact_id, instant))
        fact = dict(zip(FACT_FIELDS, (fact_id, observation.topic, observation.payload, digest, status, reason,
                                     gateway_id, device_id, message_id)))
        receipt_id = self._receipt(cur, fact, instant)
        return CommittedResult(fact_id, status, receipt_id, False)
