"""Fault injection for sampling-only connection and USB cleanup."""

from threading import Event
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from test_backend_monsoon import FakeDevice

from powermonitor._vendor.pymonsoon import HVPM, Operations, pmapi, sampleEngine
from powermonitor.backends.base import BackendError
from powermonitor.backends.sampling import SamplingOnlyMonsoonBackend, _SamplingDevice


@pytest.fixture
def disposal(monkeypatch):
    dispose = Mock()
    monkeypatch.setattr("usb.util.dispose_resources", dispose)
    return dispose


def backend_for(device):
    return SamplingOnlyMonsoonBackend(
        device_factory=lambda: device,
        engine_factory=lambda _: None,
    )


def test_connect_disconnect_preserve_power_and_never_reset(disposal):
    device = FakeDevice(handle=Mock())
    backend = backend_for(device)
    info = backend.connect("101")
    assert info.serial == "101"
    assert device.voltages == []
    backend.disconnect()
    backend.disconnect()
    disposal.assert_called_once_with(device._handle)
    assert device.Protocol.DEVICE is None
    assert not device.closed
    device._handle.reset.assert_not_called()


@pytest.mark.parametrize("operation", [lambda b: b.set_voltage(3.3), lambda b: b.disable_output()])
def test_power_operations_are_forbidden(operation, disposal):
    device = FakeDevice()
    backend = backend_for(device)
    backend.connect()
    with pytest.raises(BackendError, match="forbidden"):
        operation(backend)
    assert device.voltages == []
    backend.disconnect()


def test_failed_status_disposes_without_sdk_close(disposal):
    device = FakeDevice(model=1)
    backend = backend_for(device)
    with pytest.raises(BackendError, match="not an HVPM"):
        backend.connect()
    disposal.assert_called_once_with(device._handle)
    assert device.Protocol.DEVICE is None
    assert not device.closed
    assert backend._device is None


def test_partial_connect_keeps_fresh_protocol_for_cleanup(disposal, monkeypatch):
    handle = Mock()
    protocols = []

    class Protocol:
        def __init__(self):
            self.DEVICE = None
            protocols.append(self)

        def Connect(self, model, serial):
            assert model == Operations.HardwareModel.HVPM
            self.DEVICE = handle
            raise OSError("partial USB connect failure")

    monkeypatch.setattr(pmapi, "USB_protocol", Protocol)
    device = _SamplingDevice()
    backend = backend_for(device)
    with pytest.raises(OSError, match="partial USB"):
        backend.connect("101")
    assert device.Protocol is protocols[0]
    assert device.Protocol.DEVICE is None
    disposal.assert_called_once_with(handle)
    handle.reset.assert_not_called()


def test_devices_do_not_share_sdk_default_protocol(monkeypatch):
    protocols = []

    class Protocol:
        def __init__(self):
            protocols.append(self)

        def Connect(self, model, serial):
            self.serial = serial

    monkeypatch.setattr(pmapi, "USB_protocol", Protocol)
    first, second = _SamplingDevice(), _SamplingDevice()
    first.setup_usb("101")
    second.setup_usb("202")
    assert first.Protocol is not second.Protocol
    assert first.Protocol.serial == "101"
    assert second.Protocol.serial == "202"
    with pytest.raises(BackendError, match="fresh"):
        first.setup_usb(Protocol=object())


def test_failed_connection_disposal_retains_device_for_retry(disposal):
    disposal.side_effect = [OSError("dispose failed"), None]
    device = FakeDevice(model=1)
    backend = backend_for(device)
    with pytest.raises(BackendError, match="not an HVPM"):
        backend.connect()
    assert backend._device is device
    assert device.Protocol.DEVICE is device._handle
    backend.disconnect()
    assert backend._device is None
    assert device.Protocol.DEVICE is None
    assert disposal.call_count == 2
    assert not device.closed


def test_failed_disconnect_retains_handle_for_retry(disposal):
    disposal.side_effect = [OSError("dispose failed"), None]
    device = FakeDevice()
    backend = backend_for(device)
    backend.connect()
    with pytest.raises(BackendError, match="dispose failed"):
        backend.disconnect()
    assert backend._device is device
    assert device.Protocol.DEVICE is device._handle
    backend.disconnect()
    assert device.Protocol.DEVICE is None


def test_engine_must_stop_before_disconnect_even_after_generator_closes(disposal):
    device = FakeDevice()
    backend = backend_for(device)
    backend.connect()
    engine = Mock()
    engine.periodicStopSampling.side_effect = [OSError("STOP failed"), None]
    backend._engine = engine
    with pytest.raises(BackendError, match="stop sampling first"):
        backend.disconnect()
    with pytest.raises(BackendError, match="STOP failed"):
        backend.stop_sampling()
    with pytest.raises(BackendError, match="stop sampling first"):
        backend.disconnect()
    disposal.assert_not_called()
    backend.stop_sampling()
    backend.disconnect()
    disposal.assert_called_once_with(device._handle)


def test_pinned_sampler_runs_without_power_or_reset(disposal):
    from test_sdk_sample_engine import (
        PacketDevice,
        _calibration_packets,
        _observation,
        _raw_packet,
    )

    device = PacketDevice(_calibration_packets() + [_raw_packet([_observation(0)])])
    device.setVout = Mock(side_effect=AssertionError("voltage control forbidden"))
    device.closeDevice = Mock(side_effect=AssertionError("USB reset forbidden"))
    backend = SamplingOnlyMonsoonBackend(
        device_factory=lambda: device,
        engine_factory=sampleEngine.SampleEngine,
    )
    backend.connect()
    cancel = Event()
    stream = backend.sample_batches(cancel)
    # A packet read followed by cancellation yields pending real measurements.
    original_read = device.BulkRead

    def read_then_cancel():
        data = original_read()
        if not device.packets:
            cancel.set()
        return data

    device.BulkRead = read_then_cancel
    samples = next(stream)
    assert len(samples) == 1
    stream.close()
    backend.stop_sampling()
    backend.disconnect()
    device.setVout.assert_not_called()
    device.closeDevice.assert_not_called()


def test_sampler_verification_cannot_be_bypassed_by_mock_factories(monkeypatch):
    verification = Mock(side_effect=BackendError("Unsupported SDK"))
    monkeypatch.setattr("powermonitor.backends.sampling.verify_sdk_revision", verification)
    with pytest.raises(BackendError, match="Unsupported SDK"):
        backend_for(FakeDevice())
    verification.assert_called_once_with(sampleEngine.SampleEngine, HVPM.Monsoon)


def test_mock_factories_are_paired():
    with pytest.raises(ValueError, match="together"):
        SamplingOnlyMonsoonBackend(device_factory=lambda: SimpleNamespace())
