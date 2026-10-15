from types import SimpleNamespace

import pytest

from powermonitor.backends.base import BackendError
from powermonitor.backends.monsoon import MonsoonBackend


class FakeDevice:
    def __init__(self, *, handle=object(), model=2, close_error=None):
        self.Protocol = None
        self._handle = handle
        self._model = model
        self._close_error = close_error
        self.voltages = []
        self.closed = False

    def enumerateDevices(self):
        return [101, 202]

    def setup_usb(self, serialno=None):
        self.Protocol = SimpleNamespace(DEVICE=self._handle)

    def fillStatusPacket(self):
        self.statusPacket = SimpleNamespace(
            hardwareModel=self._model, serialNumber=101, firmwareVersion=32
        )

    def setVout(self, value):
        self.voltages.append(value)

    def closeDevice(self):
        self.closed = True
        if self._close_error:
            raise self._close_error


def test_scan_does_not_connect_or_power_device():
    device = FakeDevice()
    backend = MonsoonBackend(device_factory=lambda: device, engine_factory=lambda _: None)
    assert [info.serial for info in backend.scan()] == ["101", "202"]
    assert device.Protocol is None
    assert device.voltages == []


def test_connect_requires_usb_handle_and_hvpm_status():
    no_handle = FakeDevice(handle=None)
    backend = MonsoonBackend(device_factory=lambda: no_handle, engine_factory=lambda _: None)
    with pytest.raises(BackendError, match="USB device handle"):
        backend.connect()

    wrong_model = FakeDevice(model=1)
    backend = MonsoonBackend(device_factory=lambda: wrong_model, engine_factory=lambda _: None)
    with pytest.raises(BackendError, match="not an HVPM"):
        backend.connect()
    assert wrong_model.closed


def test_voltage_commands_are_only_explicit_calls():
    device = FakeDevice()
    backend = MonsoonBackend(device_factory=lambda: device, engine_factory=lambda _: None)
    backend.connect()
    assert device.voltages == []
    backend.set_voltage(4.2)
    backend.disable_output()
    assert device.voltages == [4.2, 0]


def test_disconnect_surfaces_close_error_and_retries_same_handle():
    device = FakeDevice(close_error=OSError("reset failed"))
    backend = MonsoonBackend(device_factory=lambda: device, engine_factory=lambda _: None)
    backend.connect()
    with pytest.raises(BackendError, match="reset failed"):
        backend.disconnect()

    device._close_error = None
    backend.disconnect()
    assert device.closed
