"""Receipt requests are mutable metadata; receipt bytes never are."""

from datetime import datetime

from .errors import ReliabilityError


MAX_COUNTER = 9_223_372_036_854_775_807


def schedule_receipt(cur, receipt_id, instant: datetime) -> None:
    """Schedule within the caller's transaction, without swallowing a lease."""
    if not isinstance(instant, datetime) or instant.tzinfo is None or instant.utcoffset() is None:
        raise ReliabilityError("ingest.clock_invalid", "回执请求时间必须包含时区")
    cur.execute("SELECT requested_generation,sent_generation,lease_token,version FROM ingest_reliability.deliveries "
                "WHERE receipt_id=%s FOR UPDATE", (receipt_id,))
    row = cur.fetchone()
    if row is None:
        cur.execute("INSERT INTO ingest_reliability.deliveries(receipt_id,next_attempt_at,requested_at) VALUES (%s,%s,%s)",
                    (receipt_id, instant, instant))
        return
    requested, sent, token, version = row
    if requested > sent and token is None:
        # Do not let duplicate telemetry bypass a publisher's failure backoff.
        return
    if requested == MAX_COUNTER or version == MAX_COUNTER:
        raise ReliabilityError("ingest.generation_exhausted", "回执请求代次或版本已达上限，未确认本次请求")
    cur.execute("UPDATE ingest_reliability.deliveries SET requested_generation=requested_generation+1,"
                "requested_at=CASE WHEN requested_generation=sent_generation THEN %s ELSE requested_at END,"
                "next_attempt_at=least(next_attempt_at,%s),version=version+1 WHERE receipt_id=%s",
                (instant, instant, receipt_id))
