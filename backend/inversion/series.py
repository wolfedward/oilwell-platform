"""Exact selected samples; reference channels cannot enter prediction inputs."""

from dataclasses import dataclass
from fractions import Fraction
from uuid import UUID

from .contracts import digest, fields, identity, integer, name, fail


@dataclass(frozen=True)
class EvidencePin:
    id: UUID
    content_sha256: str
    review_version: int

    @classmethod
    def from_wire(cls, value):
        fields(value, {"id", "content_sha256", "review_version"})
        return cls(identity(value["id"]), digest(value["content_sha256"]), integer(value["review_version"], minimum=1))

    def __post_init__(self):
        if type(self.id) is not UUID: fail()
        digest(self.content_sha256); integer(self.review_version, minimum=1)


@dataclass(frozen=True)
class ProducerEndpoint:
    """Stable physical installation; never a recorder/capture/boot locator."""
    gateway_id: str
    device_id: str

    @classmethod
    def from_wire(cls,value):
        fields(value,{'gateway_id','device_id'})
        return cls(value['gateway_id'],value['device_id'])

    def __post_init__(self):
        name(self.gateway_id); name(self.device_id)


@dataclass(frozen=True)
class ChannelPin:
    """gateway/device/capture locate accepted bytes; producer identifies installation.

    Legacy absent producer uses the locator endpoint, still subject to actual
    archived/current stream and approved evidence agreement. No body is rewritten.
    """
    gateway_id: str
    device_id: str
    capture_id: UUID
    stream_id: str
    channel_id: str
    manifest_sha256: str
    calibration_pin: EvidencePin
    sampling_pin: EvidencePin
    first_index: int
    last_index: int
    sync_pin: EvidencePin | None = None
    producer_endpoint: ProducerEndpoint | None = None

    @property
    def physical_endpoint(self):
        return self.producer_endpoint or ProducerEndpoint(self.gateway_id,self.device_id)

    @classmethod
    def from_wire(cls, value):
        if type(value) is not dict: fail()
        optional={'sync_pin','producer_endpoint'}
        fields(value, (set(cls.__dataclass_fields__)-optional) | (optional & value.keys()))
        return cls(name(value["gateway_id"]), name(value["device_id"]), identity(value["capture_id"]),
                   name(value["stream_id"]), name(value["channel_id"]), digest(value["manifest_sha256"]),
                   EvidencePin.from_wire(value["calibration_pin"]), EvidencePin.from_wire(value["sampling_pin"]),
                   integer(value["first_index"]), integer(value["last_index"]),
                   EvidencePin.from_wire(value['sync_pin']) if value.get('sync_pin') is not None else None,
                   ProducerEndpoint.from_wire(value['producer_endpoint']) if value.get('producer_endpoint') is not None else None)

    def __post_init__(self):
        name(self.gateway_id); name(self.device_id); name(self.stream_id); name(self.channel_id)
        if type(self.capture_id) is not UUID or type(self.calibration_pin) is not EvidencePin or type(self.sampling_pin) is not EvidencePin: fail()
        digest(self.manifest_sha256)
        if self.sync_pin is not None and type(self.sync_pin) is not EvidencePin: fail()
        if self.producer_endpoint is not None and type(self.producer_endpoint) is not ProducerEndpoint: fail()
        integer(self.first_index); integer(self.last_index, minimum=self.first_index)


@dataclass(frozen=True)
class Sample:
    time_ns: Fraction
    index: int
    value_si: Fraction | None
    quality: int

    def __post_init__(self):
        if type(self.time_ns) is not Fraction or self.value_si is not None and type(self.value_si) is not Fraction:
            fail()
        integer(self.index); integer(self.quality, maximum=4)
        if (self.quality == 0) != (self.value_si is not None):
            fail()


@dataclass(frozen=True)
class SampleSeries:
    role: str
    samples: tuple[Sample, ...]
    source: ChannelPin

    def __post_init__(self):
        if type(self.role) is not str or self.role not in {"active_power", "crank_angle", "load", "displacement"} or type(self.samples) is not tuple or not 1 <= len(self.samples) <= 32768 or type(self.source) is not ChannelPin:
            fail()
        previous_time = None; previous_index = None
        for sample in self.samples:
            if (type(sample) is not Sample or previous_time is not None and sample.time_ns <= previous_time
                or previous_index is not None and sample.index <= previous_index
                or not self.source.first_index <= sample.index <= self.source.last_index):
                fail()
            previous_time = sample.time_ns; previous_index = sample.index


@dataclass(frozen=True)
class CycleWindow:
    start_ns: Fraction
    end_ns: Fraction
    physical_cycle_id: str

    def __post_init__(self):
        if type(self.start_ns) is not Fraction or type(self.end_ns) is not Fraction or self.start_ns >= self.end_ns:
            fail()
        name(self.physical_cycle_id)


@dataclass(frozen=True)
class PredictionInputs:
    power: SampleSeries
    crank_angle: SampleSeries
    cycle: CycleWindow
    power_window_ns: int
    time_bound_ns: Fraction

    def __post_init__(self):
        if type(self.power) is not SampleSeries or self.power.role != "active_power" or type(self.crank_angle) is not SampleSeries or self.crank_angle.role != "crank_angle" or type(self.cycle) is not CycleWindow or type(self.time_bound_ns) is not Fraction or self.time_bound_ns < 0:
            fail()
        integer(self.power_window_ns, minimum=1)


@dataclass(frozen=True)
class ReferenceInputs:
    load: SampleSeries
    displacement: SampleSeries

    def __post_init__(self):
        if type(self.load) is not SampleSeries or self.load.role != "load" or type(self.displacement) is not SampleSeries or self.displacement.role != "displacement":
            fail()
