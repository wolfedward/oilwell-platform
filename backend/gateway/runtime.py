"""One-cycle orchestration for acquisition, durable queuing, and MQTT uplink."""

from dataclasses import dataclass
import uuid

from .contracts import UplinkBatch
from .mqtt_uplink import MqttUplink
from .poller import ModbusPoller, PollError
from .spool import DurableSpool


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
        self.spool = DurableSpool(config.spool_database)
        factory = poller_factory or (lambda gateway_id, device: ModbusPoller(gateway_id, device))
        self.pollers = tuple(factory(config.gateway_id, device) for device in config.devices)
        self.uplink = (uplink_factory or MqttUplink)(config.mqtt, self.spool)
        self._sequences = {device.device_id: 0 for device in config.devices}
        self._closed = False

    def close(self):
        if not self._closed:
            self.spool.close()
            self._closed = True

    def run_cycle(self) -> CycleResult:
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
            ):
                messages_enqueued += 1
            samples_collected += len(result.samples)
        uplink_error = None
        try:
            publish_result = self.uplink.publish_pending()
            messages_published = publish_result["published"]
        except Exception as error:
            # The durable queue is the availability boundary. A broker outage is
            # surfaced to operations but must not stop subsequent Modbus polls.
            uplink_error = str(error)
        return CycleResult(
            devices_polled=len(self.pollers),
            samples_collected=samples_collected,
            messages_enqueued=messages_enqueued,
            messages_published=messages_published,
            poll_errors=tuple(poll_errors),
            uplink_error=uplink_error,
        )
