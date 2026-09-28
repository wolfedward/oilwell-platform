"""One-cycle orchestration for acquisition, durable queuing, and MQTT uplink."""

from dataclasses import dataclass
import uuid

from .contracts import UplinkBatch
from .mqtt_uplink import MqttUplink
from .poller import ModbusPoller, PollError
from .spool import DurableSpool
from .reliable_spool import ReliableSpool
from .reliable_uplink import ReliableUplink
from .spool_migrate import read_spool_version
from backend.ingest_reliability.errors import ReliabilityError


@dataclass(frozen=True)
class CycleResult:
    devices_polled: int
    samples_collected: int
    messages_enqueued: int
    messages_published: int
    poll_errors: tuple[PollError, ...]
    uplink_error: str | None = None


class GatewayRuntime:
    """Keeps field acquisition useful even while the upstream broker is unavailable."""

    def __init__(
        self,
        config,
        *,
        boot_id: str | None = None,
        poller_factory=None,
        uplink_factory=None,
    ):
        self.config = config
        self.boot_id = boot_id or str(uuid.uuid4())
        self._closed = False
        self.spool = self.uplink = None
        self.reliable_mode = False
        try:
            version = read_spool_version(config.spool_database)
            mode = getattr(config, 'confirmation_mode', 'broker')
            if mode not in ('broker', 'application') or version not in (0, 1, 2):
                raise ReliabilityError('ingest.spool_config_invalid', '确认模式或队列版本不受支持')
            if mode == 'application' and version != 2:
                raise ReliabilityError('ingest.spool_migration_required', '应用确认需要先显式初始化或迁移独立队列')
            self.reliable_mode = version == 2
            if self.reliable_mode:
                self.spool = ReliableSpool(config.spool_database, limits=config.spool_limits,
                                          receipt_timeout_seconds=config.receipt_timeout_seconds,
                                          base_backoff_seconds=config.base_backoff_seconds,
                                          max_backoff_seconds=config.max_backoff_seconds)
            else:
                self.spool = DurableSpool(config.spool_database)
            factory = poller_factory or ModbusPoller
            self.pollers = tuple(factory(config.gateway_id, device) for device in config.devices)
            self._sequences = {device.device_id: 0 for device in config.devices}
            if self.reliable_mode:
                self.uplink = (uplink_factory or ReliableUplink)(config.mqtt, self.spool,
                                                               gateway_id=config.gateway_id, prefix=config.topic_prefix)
                self.uplink.start()
            else:
                self.uplink = (uplink_factory or MqttUplink)(config.mqtt, self.spool)
        except BaseException:
            self.close()
            raise

    def close(self):
        if not self._closed:
            if self.reliable_mode and self.uplink is not None:
                # An unsuccessful join must keep the queue owned and open.
                self.uplink.close()
            if self.spool is not None:
                self.spool.close()
            self._closed = True

    def run_cycle(self) -> CycleResult:
        if self._closed:
            raise ReliabilityError('ingest.gateway_closed', '网关已关闭，不能继续采集')
        samples_collected = messages_enqueued = messages_published = 0
        poll_errors = []
        for poller in self.pollers:
            device_id = poller.device.device_id if hasattr(poller, "device") else None
            if device_id is None:
                # Injected pollers used by tests and adapters may not expose their device.
                device_id = next(iter(self._sequences)) if len(self._sequences) == 1 else None
            if device_id not in self._sequences:
                raise RuntimeError("poller device is not registered in the gateway configuration")
            self._sequences[device_id] += 1
            result = poller.poll(sequence=self._sequences[device_id])
            poll_errors.extend(result.errors)
            if not result.samples:
                continue
            batch = UplinkBatch.create(
                self.config.gateway_id,
                result.device_id,
                self.boot_id,
                result.samples,
            )
            if self.spool.enqueue(
                batch.message_id,
                batch.topic(self.config.topic_prefix),
                batch.to_json().encode("utf-8"),
                qos=1,
                **({'confirmation_mode': self.config.confirmation_mode} if self.reliable_mode else {}),
            ):
                messages_enqueued += 1
            samples_collected += len(result.samples)
        uplink_error = None
        try:
            if self.reliable_mode:
                self.uplink.wake()
                snapshot = self.uplink.snapshot()
                messages_published = snapshot['broker_published_since_last_snapshot']
                uplink_error = snapshot['error_code']
            else:
                publish_result = self.uplink.publish_pending()
                messages_published = publish_result["published"]
        except Exception as error:
            # The durable queue is the availability boundary. A broker outage is
            # surfaced to operations but must not stop subsequent Modbus polls.
            uplink_error = (error.code if isinstance(error, ReliabilityError) else 'ingest.uplink_unavailable') if self.reliable_mode else str(error)
        return CycleResult(
            devices_polled=len(self.pollers),
            samples_collected=samples_collected,
            messages_enqueued=messages_enqueued,
            messages_published=messages_published,
            poll_errors=tuple(poll_errors),
            uplink_error=uplink_error,
        )
