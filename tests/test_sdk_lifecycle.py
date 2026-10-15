from threading import Event

import pytest
from test_sdk_sample_engine import PacketDevice, _calibration_packets, _observation, _raw_packet
from usb.core import USBTimeoutError

from powermonitor._vendor.pymonsoon import sampleEngine
from powermonitor.backends.base import BackendError
from powermonitor.backends.monsoon import MonsoonBackend
from powermonitor.models import Quality


def test_misaligned_channels_raise_instead_of_silently_truncating():
    backend = MonsoonBackend(device_factory=lambda: None, engine_factory=lambda _: None)
    with pytest.raises(BackendError, match="length"):
        backend._make_batch([[0, 0], [1], [], [], [3, 3], []], 0)


def test_stop_failure_keeps_engine_for_retry():
    class Engine:
        calls = 0

        def periodicStopSampling(self):
            self.calls += 1
            if self.calls == 1:
                raise OSError("temporary stop failure")

    backend = MonsoonBackend(device_factory=lambda: None, engine_factory=lambda _: None)
    engine = Engine()
    backend._engine = engine
    with pytest.raises(BackendError):
        backend.stop_sampling()
    backend.stop_sampling()
    assert engine.calls == 2


def test_calibration_without_measurements_times_out_even_with_usb_bytes():
    class CalibrationForever(PacketDevice):
        calls = 0

        def BulkRead(self):
            self.calls += 1
            return _raw_packet([_observation(0x10)])

    device = CalibrationForever([])
    now = [0.0]

    def clock():
        now[0] += 0.01
        return now[0]

    backend = MonsoonBackend(
        device_factory=lambda: device,
        engine_factory=sampleEngine.SampleEngine,
        clock=clock,
        startup_timeout_s=0.1,
    )
    backend.connect()
    with pytest.raises(BackendError, match="calibration.*timed out"):
        next(backend.sample_batches(Event()))
    assert device.calls < 30


def test_cancel_during_bulk_read_preserves_returned_packet():
    cancel = Event()
    measurement = _raw_packet([_observation(0)])

    class CancelAfterRead(PacketDevice):
        def BulkRead(self):
            packet = super().BulkRead()
            if packet == measurement:
                cancel.set()
            return packet

    device = CancelAfterRead(_calibration_packets() + [measurement])
    backend = MonsoonBackend(
        device_factory=lambda: device,
        engine_factory=sampleEngine.SampleEngine,
        batch_samples=250,
    )
    backend.connect()
    stream = backend.sample_batches(cancel)
    assert next(stream).records["index"].tolist() == [0]
    with pytest.raises(StopIteration):
        next(stream)
    backend.stop_sampling()


def test_usb_timeouts_retry_until_no_data_deadline():
    class TimeoutThenPacket(PacketDevice):
        calls = 0

        def BulkRead(self):
            self.calls += 1
            if self.calls == 11:
                raise USBTimeoutError("timed out")
            return super().BulkRead()

    device = TimeoutThenPacket(_calibration_packets() + [_raw_packet([_observation(0)])])
    now = [0.0]

    def clock():
        now[0] += 0.01
        return now[0]

    backend = MonsoonBackend(
        device_factory=lambda: device,
        engine_factory=sampleEngine.SampleEngine,
        no_data_timeout_s=0.1,
        batch_samples=1,
        clock=clock,
    )
    backend.connect()
    stream = backend.sample_batches(Event())
    assert next(stream).records["index"].tolist() == [0]
    stream.close()
    backend.stop_sampling()


def test_non_timeout_usb_failure_is_immediate():
    class BrokenRead(PacketDevice):
        calls = 0

        def BulkRead(self):
            self.calls += 1
            if self.calls == 11:
                raise OSError("device unplugged")
            return super().BulkRead()

    device = BrokenRead(_calibration_packets())
    backend = MonsoonBackend(
        device_factory=lambda: device,
        engine_factory=sampleEngine.SampleEngine,
        no_data_timeout_s=30,
    )
    backend.connect()
    with pytest.raises(OSError, match="device unplugged"):
        next(backend.sample_batches(Event()))
    assert device.calls == 11
    backend.stop_sampling()


def test_drop_on_calibration_packet_marks_pending_measurements():
    packets = _calibration_packets()
    packets += [
        _raw_packet([_observation(0)]),
        _raw_packet([_observation(0x10)], dropped=1),
    ]
    cancel = Event()

    class CancelAfterDrop(PacketDevice):
        def BulkRead(self):
            packet = super().BulkRead()
            if packet == packets[-1]:
                cancel.set()
            return packet

    device = CancelAfterDrop(packets)
    backend = MonsoonBackend(
        device_factory=lambda: device,
        engine_factory=sampleEngine.SampleEngine,
        batch_samples=250,
    )
    backend.connect()
    stream = backend.sample_batches(cancel)
    records = next(stream).records
    assert records["index"].tolist() == [0]
    assert records["flags"][0] & int(Quality.LOSS_UNKNOWN)
    with pytest.raises(StopIteration):
        next(stream)
    backend.stop_sampling()


def test_drop_without_any_writable_sample_is_explicit_error():
    packets = _calibration_packets() + [_raw_packet([_observation(0x10)], dropped=1)]
    cancel = Event()

    class CancelAfterDrop(PacketDevice):
        def BulkRead(self):
            packet = super().BulkRead()
            if packet == packets[-1]:
                cancel.set()
            return packet

    device = CancelAfterDrop(packets)
    backend = MonsoonBackend(
        device_factory=lambda: device,
        engine_factory=sampleEngine.SampleEngine,
        batch_samples=250,
    )
    backend.connect()
    with pytest.raises(BackendError, match="sample loss.*no measurement"):
        next(backend.sample_batches(cancel))
    backend.stop_sampling()


def test_drop_after_flushed_batch_without_pending_sample_is_explicit_error():
    packets = _calibration_packets()
    packets += [
        _raw_packet([_observation(0)]),
        _raw_packet([_observation(0x10)], dropped=1),
    ]
    cancel = Event()

    class CancelAfterDrop(PacketDevice):
        def BulkRead(self):
            packet = super().BulkRead()
            if packet == packets[-1]:
                cancel.set()
            return packet

    device = CancelAfterDrop(packets)
    backend = MonsoonBackend(
        device_factory=lambda: device,
        engine_factory=sampleEngine.SampleEngine,
        batch_samples=1,
    )
    backend.connect()
    stream = backend.sample_batches(cancel)
    assert next(stream).records["index"].tolist() == [0]
    with pytest.raises(BackendError, match="sample loss.*no measurement"):
        next(stream)
    backend.stop_sampling()


def test_packet_coalescing_preserves_all_samples():
    packets = _calibration_packets()
    packets += [_raw_packet([_observation(0, base=i)]) for i in range(10)]
    device = PacketDevice(packets)
    backend = MonsoonBackend(
        device_factory=lambda: device, engine_factory=sampleEngine.SampleEngine, batch_samples=5
    )
    backend.connect()
    stream = backend.sample_batches(Event())
    first, second = next(stream).records, next(stream).records
    assert first["index"].tolist() == list(range(5))
    assert second["index"].tolist() == list(range(5, 10))
    stream.close()
    backend.stop_sampling()


def test_read_failure_retains_pending_original_samples_before_error():
    packets = _calibration_packets() + [_raw_packet([_observation(0)])]
    device = PacketDevice(packets)
    backend = MonsoonBackend(
        device_factory=lambda: device, engine_factory=sampleEngine.SampleEngine, batch_samples=250
    )
    backend.connect()
    stream = backend.sample_batches(Event())
    assert next(stream).records["index"].tolist() == [0]
    with pytest.raises(RuntimeError, match="fixture exhausted"):
        next(stream)
    backend.stop_sampling()
