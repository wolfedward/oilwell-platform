"""Manual acknowledgement after committed reliable repository results."""

from datetime import datetime, timezone

from .contracts import CommittedResult, Observation
from .errors import ReliabilityError


class ReliableConsumer:
    def __init__(self, repository, *, clock=None):
        self.repository = repository
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    def handle_message(self, client, message):
        instant = self.clock()
        if not isinstance(instant, datetime) or instant.tzinfo is None or instant.utcoffset() is None:
            raise ReliabilityError("ingest.clock_invalid", "中心接收时钟必须包含时区")
        observed = Observation(topic=message.topic, payload=bytes(message.payload), qos=message.qos,
                               retain=message.retain, dup=message.dup, received_at=instant)
        result = self.repository.ingest(observed)
        if not isinstance(result, CommittedResult) or result.status not in {"stored", "quarantined"}:
            raise ReliabilityError("ingest.commit_unconfirmed", "未取得已提交的接收结果，未确认接收")
        if message.qos == 1:
            ack_code = client.ack(message.mid, message.qos)
            if ack_code not in (0, None):
                raise ReliabilityError("ingest.ack_failed", "接收确认尚未完成")
        return result
