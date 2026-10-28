from types import SimpleNamespace
from unittest.mock import Mock, call

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


@pytest.fixture
def dispose_resources(monkeypatch):
    dispose = Mock()
    monkeypatch.setattr("usb.util.dispose_resources", dispose)
    return dispose


@pytest.fixture
def capture_device():
    device = FakeDevice(handle=Mock())
    yield device
    assert not device.closed
    device._handle.reset.assert_not_called()
    assert device.voltages == []


@pytest.mark.parametrize("options", [{}, {"reset_on_close": True}], ids=["default", "explicit"])
def test_disconnect_preserves_sdk_close_behavior(options, dispose_resources):
    from powermonitor._vendor.pymonsoon import pmapi

    device = FakeDevice(handle=Mock())
    backend = MonsoonBackend(
        device_factory=lambda: device, engine_factory=lambda _: None, **options
    )
    backend.connect()
    protocol = device.Protocol
    assert protocol is not None
    protocol.stopSampling = Mock()
    device.closeDevice = Mock(side_effect=lambda: pmapi.USB_protocol.closeDevice(protocol))

    backend.disconnect()

    device.closeDevice.assert_called_once_with()
    protocol.stopSampling.assert_called_once_with()
    device._handle.reset.assert_called_once_with()
    dispose_resources.assert_called_once_with(device._handle)
    assert device.voltages == []


@pytest.mark.parametrize("failure", ["setup", "status", "model"])
@pytest.mark.parametrize("dispose_fails", [False, True])
def test_capture_connect_failure_only_disposes_usb_resources(
    failure, dispose_fails, capture_device, dispose_resources
):
    device = capture_device
    backend = MonsoonBackend(
        device_factory=lambda: device, engine_factory=lambda _: None, reset_on_close=False
    )
    connect_error = OSError("connect failed")
    if failure == "setup":
        def fail_setup(serialno=None):
            device.Protocol = SimpleNamespace(DEVICE=device._handle)
            raise connect_error

        device.setup_usb = fail_setup
    elif failure == "status":
        device.fillStatusPacket = Mock(side_effect=connect_error)
    else:
        device._model = 1
    if dispose_fails:
        dispose_resources.side_effect = OSError("dispose failed")

    expected = BackendError if failure == "model" else OSError
    message = "not an HVPM" if failure == "model" else "connect failed"
    with pytest.raises(expected, match=message) as error:
        backend.connect()
    if failure != "model":
        assert error.value is connect_error
    backend.disconnect()
    dispose_resources.assert_called_once_with(device._handle)


@pytest.mark.parametrize("connected", [False, True], ids=["connect-failed", "disconnect"])
@pytest.mark.parametrize("missing", ["Protocol", "DEVICE"])
@pytest.mark.parametrize("is_none", [False, True], ids=["absent", "none"])
def test_capture_close_skips_missing_usb_handles(
    connected, missing, is_none, capture_device, dispose_resources
):
    device = capture_device
    backend = MonsoonBackend(
        device_factory=lambda: device, engine_factory=lambda _: None, reset_on_close=False
    )
    if connected:
        backend.connect()
    else:
        device.setup_usb()
        device.setup_usb = Mock()
    owner = device if missing == "Protocol" else device.Protocol
    if is_none:
        setattr(owner, missing, None)
    else:
        delattr(owner, missing)

    if not connected:
        with pytest.raises(BackendError, match="USB device handle"):
            backend.connect()
    backend.disconnect()
    dispose_resources.assert_not_called()


@pytest.mark.parametrize("dispose_fails", [False, True])
def test_capture_disconnect_disposes_without_reset_and_preserves_retry(
    dispose_fails, capture_device, dispose_resources
):
    device = capture_device
    backend = MonsoonBackend(
        device_factory=lambda: device, engine_factory=lambda _: None, reset_on_close=False
    )
    backend.connect()
    if dispose_fails:
        close_error = OSError("dispose failed")
        dispose_resources.side_effect = close_error
        with pytest.raises(
            BackendError, match="Unable to close Monsoon device: dispose failed"
        ) as error:
            backend.disconnect()
        assert error.value.__cause__ is close_error
        with pytest.raises(BackendError, match="already connected"):
            backend.connect()
        dispose_resources.side_effect = None

    backend.disconnect()
    backend.disconnect()
    assert dispose_resources.call_args_list == [call(device._handle)] * (2 if dispose_fails else 1)


def test_default_connect_failure_preserves_original_error_when_close_fails():
    device = FakeDevice(close_error=OSError("reset failed"))
    connect_error = OSError("status failed")
    device.fillStatusPacket = Mock(side_effect=connect_error)
    backend = MonsoonBackend(device_factory=lambda: device, engine_factory=lambda _: None)

    with pytest.raises(OSError, match="status failed") as error:
        backend.connect()

    assert error.value is connect_error
    assert device.closed
    assert device.voltages == []


def test_capture_disconnect_allows_reconnect(capture_device, dispose_resources):
    backend = MonsoonBackend(
        device_factory=lambda: capture_device,
        engine_factory=lambda _: None,
        reset_on_close=False,
    )
    for _ in range(3):
        backend.connect()
        backend.disconnect()
    assert dispose_resources.call_args_list == [call(capture_device._handle)] * 3
