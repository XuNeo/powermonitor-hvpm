"""Control commands must prove configured OFF, preserve power, and never reset."""

from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from test_backend_monsoon import FakeDevice

from powermonitor._vendor.pymonsoon import Operations
from powermonitor.backends.base import BackendError
from powermonitor.backends.control import ControlMonsoonBackend
from powermonitor.backends.locked import LockedMonsoonBackend


@pytest.fixture
def control(monkeypatch):
    device = FakeDevice()
    configured = [0]
    reads = []

    def read(opcode, length):
        reads.append((opcode, length))
        return configured[0]

    def setup(serialno=None):
        device.Protocol = SimpleNamespace(DEVICE=device._handle, getValue=read)

    def set_voltage(value):
        device.voltages.append(value)
        configured[0] = int(value * Operations.Conversion.FLOAT_TO_INT)

    device.setup_usb = setup
    device.setVout = set_voltage
    dispose = Mock()
    monkeypatch.setattr("usb.util.dispose_resources", dispose)
    backend = ControlMonsoonBackend(device_factory=lambda: device, engine_factory=lambda _: None)
    backend.connect()
    yield backend, device, configured, reads, dispose
    backend.disconnect()
    assert not device.closed


def test_on_reads_off_before_writing_and_checks_result(control):
    backend, device, configured, reads, dispose = control
    backend.set_voltage(3.3)
    assert device.voltages == [3.3]
    assert reads == [(Operations.OpCodes.setMainVoltage, 4)] * 2
    assert backend.configured_voltage() == pytest.approx(3.3, abs=1e-6)
    backend.disconnect()
    assert configured[0] > 0
    dispose.assert_called_once_with(device._handle)


def test_already_on_requires_explicit_off_not_hidden_power_cycle(control):
    backend, device, configured, reads, dispose = control
    configured[0] = 4 * Operations.Conversion.FLOAT_TO_INT
    with pytest.raises(BackendError, match="explicitly run output off"):
        backend.set_voltage(3.3)
    assert device.voltages == []
    backend.disable_output()
    backend.set_voltage(3.3)
    assert device.voltages == [0, 3.3]


@pytest.mark.parametrize("raw", [0xFFFFFFFF, 1, -1, float("nan")])
def test_unrecognized_readback_blocks_on_without_writing(control, raw):
    backend, device, configured, *_ = control
    configured[0] = raw
    with pytest.raises(BackendError, match="unknown"):
        backend.set_voltage(3.3)
    assert device.voltages == []


def test_readback_mismatch_does_not_retry_or_turn_off(control):
    backend, device, configured, *_ = control
    device.setVout = lambda value: device.voltages.append(value)
    with pytest.raises(BackendError, match="readback mismatch"):
        backend.set_voltage(3.3)
    assert device.voltages == [3.3]


def test_readback_failure_never_falls_back_to_power_write(control):
    backend, device, *_ = control
    device.Protocol.getValue = Mock(side_effect=OSError("read failed"))
    with pytest.raises(BackendError, match="read failed"):
        backend.set_voltage(3.3)
    assert device.voltages == []


def test_status_reads_only_and_never_claims_measured_output(control):
    backend, device, configured, *_ = control
    configured[0] = 3 * Operations.Conversion.FLOAT_TO_INT
    device.fillStatusPacket()
    packet = device.statusPacket
    packet.protocolVersion = 1
    packet.powerupCurrentLimit = 0.5
    packet.runtimeCurrentLimit = 1.0
    packet.usbPassthroughMode = 0
    device.fillStatusPacket = lambda: None
    status = backend.read_status()
    assert status["configured_voltage_v"] == 3
    assert status["output_state"] == "unknown"
    assert status["runtime_current_limit_a"] == 1
    assert device.voltages == []


def test_gui_lease_spans_connect_and_cleanup(monkeypatch):
    calls = []
    device = FakeDevice()
    device.enumerateDevices = lambda: ["101"]
    lease = Mock()
    lease.acquire.side_effect = lambda: calls.append("lock")
    lease.release.side_effect = lambda: calls.append("unlock")
    dispose = Mock(side_effect=lambda _: calls.append("dispose"))
    monkeypatch.setattr("powermonitor.backends.locked.HardwareLease", lambda: lease)
    monkeypatch.setattr("usb.util.dispose_resources", dispose)
    backend = LockedMonsoonBackend(device_factory=lambda: device, engine_factory=lambda _: None)
    backend.connect()
    assert calls == ["lock"]
    backend.disconnect()
    assert calls == ["lock", "dispose", "unlock"]


def test_gui_failed_disposal_keeps_lease_for_retry(monkeypatch):
    device = FakeDevice()
    device.enumerateDevices = lambda: ["101"]
    lease = Mock()
    monkeypatch.setattr("powermonitor.backends.locked.HardwareLease", lambda: lease)
    dispose = Mock(side_effect=[OSError("dispose failed"), None])
    monkeypatch.setattr("usb.util.dispose_resources", dispose)
    backend = LockedMonsoonBackend(device_factory=lambda: device, engine_factory=lambda _: None)
    backend.connect()
    with pytest.raises(BackendError):
        backend.disconnect()
    lease.release.assert_not_called()
    backend.disconnect()
    lease.release.assert_called_once()


def test_gui_multiple_devices_never_autoconnects(monkeypatch):
    lease = Mock()
    device = FakeDevice()
    device.enumerateDevices = lambda: ["101", "202"]
    monkeypatch.setattr("powermonitor.backends.locked.HardwareLease", lambda: lease)
    backend = LockedMonsoonBackend(device_factory=lambda: device, engine_factory=lambda _: None)
    with pytest.raises(RuntimeError, match="multiple"):
        backend.connect()
    assert device.Protocol is None
    lease.release.assert_called_once()


def test_gui_partial_connect_retains_cleanup_retry_and_lease(monkeypatch):
    lease = Mock()
    device = FakeDevice(model=1)
    device.enumerateDevices = lambda: ["101"]
    dispose = Mock(side_effect=[OSError("dispose failed"), None])
    monkeypatch.setattr("powermonitor.backends.locked.HardwareLease", lambda: lease)
    monkeypatch.setattr("usb.util.dispose_resources", dispose)
    backend = LockedMonsoonBackend(device_factory=lambda: device, engine_factory=lambda _: None)
    with pytest.raises(BackendError, match="not an HVPM"):
        backend.connect()
    assert backend._device is device
    lease.release.assert_not_called()
    backend.disconnect()
    lease.release.assert_called_once()
