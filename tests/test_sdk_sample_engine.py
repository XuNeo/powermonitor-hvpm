import inspect
import struct
from collections import deque
from threading import Event
from types import SimpleNamespace

import numpy as np
import pytest

from powermonitor._vendor.pymonsoon import HVPM, sampleEngine
from powermonitor._vendor.pymonsoon import SDK_REVISION as VENDORED_SDK_REVISION
from powermonitor.backends.base import BackendError
from powermonitor.backends.monsoon import MonsoonBackend, verify_sdk_revision
from powermonitor.models import SDK_REVISION, Quality


def _observation(sample_type, *, base=0, voltage_raw=13200):
    if sample_type == 0x10:  # nonzero zero-calibration values
        return [1000, 1000, 1000, 1000, 1000, 1000, voltage_raw, 0, sample_type, 0]
    if sample_type == 0x30:
        return [2000, 2000, 2000, 2000, 2000, 2000, voltage_raw, 0, sample_type, 0]
    return [1500 + base, 1500 + base, 1500, 1500, 1500, 1500, voltage_raw, 0, 0, 0]


def _raw_packet(observations, *, dropped=0, flags=0):
    values = [dropped, flags, len(observations)]
    for observation in observations:
        values.extend(observation)
    values.extend([0] * (33 - len(values)))
    swizzled = struct.pack("HBBHHHHHHHHBBHHHHHHHHBBHHHHHHHHBB", *values)
    raw = bytearray(64)
    raw[0:4] = swizzled[0:4]
    for index, value in enumerate(swizzled[4:]):
        raw[4 + index] = value
    # HVPM.swizzlePacket swaps each pair after byte four.
    for index in range(4, 62, 2):
        raw[index], raw[index + 1] = raw[index + 1], raw[index]
    return bytes(raw)


class PacketDevice:
    fineThreshold = 64000
    auxFineThreshold = 30000
    mainvoltageScale = 4
    usbVoltageScale = 2

    def __init__(self, packets):
        self.packets = deque(packets)
        self.Protocol = SimpleNamespace(DEVICE=object())
        self.statusPacket = SimpleNamespace(
            hardwareModel=2,
            serialNumber=17,
            firmwareVersion=32,
            mainCoarseScale=1.0,
            mainFineScale=1000.0,
            usbCoarseScale=1.0,
            usbFineScale=1000.0,
            auxCoarseScale=1.0,
            auxFineScale=1000.0,
            mainCoarseZeroOffset=0.0,
            mainFineZeroOffset=0.0,
            usbCoarseZeroOffset=0.0,
            usbFineZeroOffset=0.0,
        )
        self.stop_count = 0

    def setup_usb(self, serialno=None):
        pass

    def fillStatusPacket(self):
        pass

    def StartSampling(self, calTime, maxTime):
        pass

    def stopSampling(self):
        self.stop_count += 1

    def BulkRead(self):
        if not self.packets:
            raise RuntimeError("packet fixture exhausted")
        return self.packets.popleft()

    padding = np.zeros(64)

    def swizzlePacket(self, packet):
        swizzle = getattr(HVPM.Monsoon, "swizzlePacket")
        return swizzle(self, np.frombuffer(packet, dtype=np.uint8))

    def setVout(self, value):
        pass

    def closeDevice(self):
        pass


def _calibration_packets():
    packets = []
    for _ in range(5):
        packets.append(_raw_packet([_observation(0x10)]))
        packets.append(_raw_packet([_observation(0x30)]))
    return packets


def test_pinned_sdk_private_loop_signature_and_source():
    verify_sdk_revision(sampleEngine.SampleEngine, HVPM.Monsoon)
    signature = inspect.signature(getattr(sampleEngine.SampleEngine, "_SampleEngine__sampleLoop"))
    assert list(signature.parameters)[:4] == ["self", "S", "Samples", "ProcessRate"]
    assert SDK_REVISION == "1afab10eac4468bd54f0f2256a9ed870eb5346c4"
    assert VENDORED_SDK_REVISION == SDK_REVISION


def test_real_sample_engine_calibrates_converts_and_keeps_cross_batch_samples():
    packets = _calibration_packets()
    packets += [
        _raw_packet([_observation(0, base=0), _observation(0, base=100)]),
        _raw_packet([_observation(0, base=200), _observation(0, base=300)]),
    ]
    device = PacketDevice(packets)
    backend = MonsoonBackend(
        device_factory=lambda: device,
        engine_factory=sampleEngine.SampleEngine,
        batch_samples=1,
    )
    backend.connect()
    cancel = Event()
    stream = backend.sample_batches(cancel)
    first = next(stream).records.copy()
    second = next(stream).records.copy()
    cancel.set()
    list(stream)
    backend.stop_sampling()

    combined = np.concatenate((first, second))
    assert combined["index"].tolist() == [0, 1, 2, 3]
    assert np.allclose(combined["current_a"], [0.0005, 0.0006, 0.0007, 0.0008])
    assert np.allclose(combined["voltage_v"], 3.3)
    assert np.allclose(combined["time_s"], np.arange(4) / 5000)
    assert np.all(np.diff(combined["host_elapsed_s"]) >= 0)


def test_real_sample_engine_propagates_dropped_change_and_sticky_error():
    packets = _calibration_packets()
    packets += [
        _raw_packet([_observation(0)], dropped=1),
        _raw_packet([_observation(0)], dropped=2),
        _raw_packet([_observation(0)], dropped=2, flags=0x10),
    ]
    device = PacketDevice(packets)
    backend = MonsoonBackend(
        device_factory=lambda: device,
        engine_factory=sampleEngine.SampleEngine,
        batch_samples=1,
    )
    backend.connect()
    stream = backend.sample_batches(Event())
    first = next(stream).records
    second = next(stream).records
    assert first["flags"][0] & int(Quality.LOSS_UNKNOWN)
    assert second["flags"][0] & int(Quality.LOSS_UNKNOWN)
    error_batch = next(stream).records
    assert error_batch["flags"][0] & int(Quality.DEVICE_ERROR)
    with pytest.raises(BackendError, match="sticky device error"):
        next(stream)


def test_cancel_before_calibration_does_not_read_packets():
    device = PacketDevice([])
    backend = MonsoonBackend(
        device_factory=lambda: device,
        engine_factory=sampleEngine.SampleEngine,
        batch_samples=1,
    )
    backend.connect()
    cancel = Event()
    cancel.set()
    assert list(backend.sample_batches(cancel)) == []
