import threading
import time
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from powermonitor.acquisition import AcquisitionController
from powermonitor.backends.simulator import SimulatorBackend
from powermonitor.models import DeviceInfo
from powermonitor.storage import CaptureReader


def wait_for(controller, predicate, timeout=2.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        state = controller.state
        if predicate(state):
            return state
        time.sleep(0.005)
    raise AssertionError(f"timed out waiting for controller state: {controller.state}")


def test_simulator_controller_full_flow_and_independent_output(tmp_path):
    controller = AcquisitionController()
    try:
        controller.request("connect", backend="simulator")
        state = wait_for(controller, lambda value: value.connection == "connected")
        assert state.output == "off"

        controller.request("set_voltage", voltage_v=4.2)
        wait_for(controller, lambda value: value.target_voltage_v == 4.2)
        controller.request("enable_output", confirmed=True)
        wait_for(controller, lambda value: value.output == "on")

        capture = tmp_path / "capture.pmcap"
        controller.request("start", path=capture)
        wait_for(controller, lambda value: value.sample_count >= 250)
        controller.request("stop")
        state = wait_for(controller, lambda value: value.capture == "idle")
        assert state.output == "on"
        assert len(controller.recent_records()) >= 250

        controller.request("disable_output")
        state = wait_for(controller, lambda value: value.output == "off")
        assert not state.output_owned
        reader = CaptureReader(capture)
        assert reader.metadata["complete"]
        assert len(reader.records) == state.sample_count
    finally:
        assert controller.shutdown()


def test_real_style_unknown_requires_explicit_off_before_configuration():
    class RealStyleSimulator(SimulatorBackend):
        def connect(self, serial=None):
            super().connect(serial)
            return DeviceInfo(serial="real-ish", backend="monsoon")

    controller = AcquisitionController(
        backend_factories={"monsoon": RealStyleSimulator, "simulator": SimulatorBackend}
    )
    try:
        controller.request("connect", backend="monsoon")
        wait_for(controller, lambda value: value.connection == "connected")
        assert controller.state.output == "unknown"
        controller.request("set_voltage", voltage_v=5.0)
        wait_for(controller, lambda value: bool(value.error))
        assert controller.state.target_voltage_v == 3.3
        controller.request("disable_output")
        wait_for(controller, lambda value: value.output == "off")
        controller.request("set_voltage", voltage_v=5.0)
        wait_for(controller, lambda value: value.target_voltage_v == 5.0)
    finally:
        assert controller.shutdown()


def test_configuration_rejected_while_capture_runs(tmp_path):
    controller = AcquisitionController()
    try:
        controller.request("connect")
        wait_for(controller, lambda value: value.connection == "connected")
        controller.request("start", path=tmp_path / "running.pmcap")
        wait_for(controller, lambda value: value.capture == "running")
        controller.request("set_voltage", voltage_v=3.7)
        wait_for(controller, lambda value: "capture is active" in value.error)
        assert controller.state.target_voltage_v == 3.3
        controller.request("stop")
        wait_for(controller, lambda value: value.capture == "idle")
    finally:
        assert controller.shutdown()


def test_stop_immediately_after_start_cancels_pending_capture(tmp_path):
    controller = AcquisitionController()
    try:
        controller.request("connect")
        wait_for(controller, lambda value: value.connection == "connected")
        capture = tmp_path / "cancelled-before-start.pmcap"
        controller.request("start", path=capture)
        controller.request("stop")
        wait_for(controller, lambda value: value.capture == "idle")
        wait_for(controller, lambda value: value.recording_path is None)
        assert not capture.exists()
    finally:
        assert controller.shutdown()


def test_disable_output_immediately_after_start_cancels_then_turns_off(tmp_path):
    controller = AcquisitionController()
    try:
        controller.request("connect")
        wait_for(controller, lambda value: value.connection == "connected")
        controller.request("enable_output", confirmed=True)
        wait_for(controller, lambda value: value.output == "on")
        capture = tmp_path / "cancelled-and-off.pmcap"
        controller.request("start", path=capture)
        controller.request("disable_output")
        wait_for(controller, lambda value: value.output == "off")
        assert controller.state.capture == "idle"
        assert controller.state.recording_path is None
        assert not capture.exists()
    finally:
        assert controller.shutdown()


def test_disable_output_stops_capture_before_off(tmp_path):
    calls = []

    class OrderedBackend(SimulatorBackend):
        def stop_sampling(self):
            calls.append("stop")
            super().stop_sampling()

        def disable_output(self):
            calls.append("off")
            super().disable_output()

    controller = AcquisitionController(
        backend_factories={"simulator": OrderedBackend, "monsoon": OrderedBackend}
    )
    try:
        controller.request("connect")
        wait_for(controller, lambda value: value.connection == "connected")
        controller.request("enable_output", confirmed=True)
        wait_for(controller, lambda value: value.output == "on")
        controller.request("start", path=tmp_path / "ordered.pmcap")
        wait_for(controller, lambda value: value.capture == "running")
        controller.request("disable_output")
        wait_for(controller, lambda value: value.output == "off")
        assert calls[-2:] == ["stop", "off"]
    finally:
        assert controller.shutdown()


def test_close_error_is_preserved_as_unsafe():
    class CloseFailBackend(SimulatorBackend):
        def disconnect(self):
            raise OSError("close reset failed")

    controller = AcquisitionController(
        backend_factories={"simulator": CloseFailBackend, "monsoon": CloseFailBackend}
    )
    controller.request("connect")
    wait_for(controller, lambda value: value.connection == "connected")
    assert controller.shutdown()
    assert controller.state.connection == "error"
    assert controller.state.output == "unknown"
    assert "close reset failed" in controller.state.error


def test_shutdown_timeout_marks_output_unknown():
    class BlockingBackend(SimulatorBackend):
        def disconnect(self):
            time.sleep(0.2)
            super().disconnect()

    controller = AcquisitionController(
        backend_factories={"simulator": BlockingBackend, "monsoon": BlockingBackend}
    )
    controller.request("connect")
    wait_for(controller, lambda value: value.connection == "connected")
    assert not controller.shutdown(timeout=0.01)
    assert controller.state.output == "unknown"
    assert "shutdown timeout" in controller.state.error
    time.sleep(0.25)


def test_start_state_changes_only_when_worker_handles_command(tmp_path):
    entered = threading.Event()
    release = threading.Event()

    class BlockingScanBackend(SimulatorBackend):
        block_scan = False

        def scan(self):
            if self.block_scan:
                entered.set()
                assert release.wait(1.0)
            return super().scan()

    controller = AcquisitionController(
        backend_factories={"simulator": BlockingScanBackend, "monsoon": BlockingScanBackend}
    )
    try:
        controller.request("connect")
        wait_for(controller, lambda value: value.connection == "connected")
        BlockingScanBackend.block_scan = True
        controller.request("scan")
        assert entered.wait(1.0)
        controller.request("start", path=tmp_path / "queued.pmcap")
        assert controller.state.capture == "idle"
        assert controller.state.recording_path is None
        release.set()
        wait_for(controller, lambda value: value.capture == "running")
        controller.request("stop")
        wait_for(controller, lambda value: value.capture == "idle")
    finally:
        release.set()
        assert controller.shutdown()


def test_writer_factory_failure_restores_capture_state(tmp_path):
    def fail_writer(*args, **kwargs):
        raise OSError("cannot create capture")

    controller = AcquisitionController(writer_factory=fail_writer)
    try:
        controller.request("connect")
        wait_for(controller, lambda value: value.connection == "connected")
        controller.request("start", path=tmp_path / "unwritable.pmcap")
        state = wait_for(controller, lambda value: bool(value.error))
        assert state.capture == "error"
        assert state.recording_path is None
        controller.request("disconnect")
        wait_for(controller, lambda value: value.connection == "disconnected")
    finally:
        assert controller.shutdown()


def test_disconnected_start_does_not_leave_capture_starting(tmp_path):
    controller = AcquisitionController()
    try:
        controller.request("start", path=tmp_path / "disconnected.pmcap")
        state = wait_for(controller, lambda value: bool(value.error))
        assert state.capture == "error"
        assert state.recording_path is None
        controller.request("connect")
        wait_for(controller, lambda value: value.connection == "connected")
    finally:
        assert controller.shutdown()


def test_capture_error_allows_disconnect_and_marks_output_unknown(tmp_path):
    class SamplingFailBackend(SimulatorBackend):
        def sample_batches(self, cancel):
            raise OSError("device unplugged")
            yield

    controller = AcquisitionController(
        backend_factories={"simulator": SamplingFailBackend, "monsoon": SamplingFailBackend}
    )
    try:
        controller.request("connect")
        wait_for(controller, lambda value: value.connection == "connected")
        controller.request("enable_output", confirmed=True)
        wait_for(controller, lambda value: value.output == "on")
        controller.request("start", path=tmp_path / "failed.pmcap")
        state = wait_for(controller, lambda value: value.capture == "error")
        assert state.connection == "error"
        assert state.output == "unknown"
        assert state.output_owned
        controller.request("disconnect")
        wait_for(controller, lambda value: value.connection == "disconnected")
    finally:
        assert controller.shutdown()


def test_stop_failure_is_unsafe_and_generator_is_closed(tmp_path):
    closed = threading.Event()

    class StopFailBackend(SimulatorBackend):
        def sample_batches(self, cancel):
            try:
                yield from super().sample_batches(cancel)
            finally:
                closed.set()

        def stop_sampling(self):
            raise OSError("stop failed")

    controller = AcquisitionController(
        backend_factories={"simulator": StopFailBackend, "monsoon": StopFailBackend}
    )
    try:
        controller.request("connect")
        wait_for(controller, lambda value: value.connection == "connected")
        controller.request("enable_output", confirmed=True)
        wait_for(controller, lambda value: value.output == "on")
        controller.request("start", path=tmp_path / "stop-failed.pmcap")
        wait_for(controller, lambda value: value.capture == "running")
        controller.request("stop")
        state = wait_for(controller, lambda value: value.capture == "error")
        assert closed.is_set()
        assert state.connection == "error"
        assert state.output == "unknown"
        assert state.output_owned
    finally:
        assert controller.shutdown()


def test_enable_failure_tracks_cleanup_obligation_and_unknown_output():
    class EnableFailBackend(SimulatorBackend):
        def set_voltage(self, voltage_v):
            super().set_voltage(voltage_v)
            raise OSError("enable acknowledgement lost")

    backend = EnableFailBackend()
    controller = AcquisitionController(
        backend_factories={"simulator": lambda: backend, "monsoon": lambda: backend}
    )
    try:
        controller.request("connect")
        wait_for(controller, lambda value: value.connection == "connected")
        controller.request("enable_output", confirmed=True)
        state = wait_for(controller, lambda value: bool(value.error))
        assert backend.output_enabled
        assert state.output == "unknown"
        assert state.output_owned
        controller.request("disable_output")
        state = wait_for(controller, lambda value: value.output == "off")
        assert not backend.output_enabled
        assert not state.output_owned
    finally:
        assert controller.shutdown()


def test_state_notifications_are_coalesced_during_unpolled_capture(tmp_path):
    controller = AcquisitionController()
    try:
        controller.request("connect")
        wait_for(controller, lambda value: value.connection == "connected")
        controller.poll_events()
        controller.request("start", path=tmp_path / "events.pmcap")
        wait_for(controller, lambda value: value.sample_count >= 1000)
        events = controller.poll_events()
        assert len([event for event in events if event.kind == "state"]) <= 1
        controller.request("stop")
        wait_for(controller, lambda value: value.capture == "idle")
    finally:
        assert controller.shutdown()


def test_shutdown_success_does_not_claim_output_off():
    controller = AcquisitionController()
    controller.request("connect")
    wait_for(controller, lambda value: value.connection == "connected")
    controller.request("enable_output", confirmed=True)
    wait_for(controller, lambda value: value.output == "on")
    assert controller.shutdown()
    assert controller.state.output == "unknown"
    assert controller.state.connection == "disconnected"


@pytest.mark.parametrize("cleanup", ["disconnect", "shutdown"])
def test_default_monsoon_factory_uses_capture_safe_cleanup(monkeypatch, cleanup):
    from powermonitor.backends.locked import LockedMonsoonBackend

    handle = Mock()
    device = Mock()
    device.Protocol = SimpleNamespace(DEVICE=handle)
    device.statusPacket = SimpleNamespace(hardwareModel=2, serialNumber=101, firmwareVersion=32)
    factory = Mock(
        side_effect=lambda **options: LockedMonsoonBackend(
            device_factory=lambda: device, engine_factory=lambda _: None, **options
        )
    )
    dispose = Mock()
    lease = Mock()
    monkeypatch.setattr("powermonitor.backends.locked.HardwareLease", lambda: lease)
    device.enumerateDevices.return_value = [101]
    monkeypatch.setattr("powermonitor.acquisition.LockedMonsoonBackend", factory)
    monkeypatch.setattr("usb.util.dispose_resources", dispose)
    controller = AcquisitionController()
    try:
        controller.request("connect", backend="monsoon")
        wait_for(controller, lambda state: state.connection == "connected")
        factory.assert_called_once_with()
        lease.acquire.assert_called_once()
        if cleanup == "disconnect":
            controller.request("disconnect")
            wait_for(controller, lambda state: state.connection == "disconnected")
        else:
            assert controller.shutdown()
        dispose.assert_called_once_with(handle)
        lease.release.assert_called_once()
        device.closeDevice.assert_not_called()
        handle.reset.assert_not_called()
        device.setVout.assert_not_called()
    finally:
        assert controller.shutdown()


def test_shutdown_retains_failed_session_for_hardware_lease_lifetime():
    from powermonitor.backends.base import BackendError

    class CloseFail(SimulatorBackend):
        def disconnect(self):
            raise BackendError("close failed")

    backend = CloseFail()
    controller = AcquisitionController(backend_factories={"simulator": lambda: backend})
    controller.request("connect")
    wait_for(controller, lambda state: state.connection == "connected")
    assert controller.shutdown()
    assert controller._backend is backend
    assert controller.state.connection == "error"
