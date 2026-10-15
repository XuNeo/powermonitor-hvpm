import json
import threading
import time

import numpy as np

from powermonitor.acquisition import AcquisitionController
from powermonitor.backends.simulator import SimulatorBackend
from powermonitor.models import SAMPLE_DTYPE, SDK_REVISION, DeviceInfo, SampleBatch
from powermonitor.storage import CaptureReader


def wait_until(controller, predicate):
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.005)
    raise AssertionError(controller.state)


def test_rejected_enable_does_not_claim_output_ownership():
    backend = SimulatorBackend()
    controller = AcquisitionController(backend_factories={"simulator": lambda: backend})
    try:
        controller.request("connect")
        wait_until(controller, lambda: controller.state.connection == "connected")
        controller.request("enable_output", confirmed=False)
        wait_until(controller, lambda: bool(controller.state.error))
        assert not controller.state.output_owned
        assert controller.state.output == "off"
        assert not backend.output_enabled
    finally:
        controller.shutdown()


def test_disconnect_cancels_pending_start_before_serial_close(tmp_path):
    entered = threading.Event()
    release = threading.Event()

    class BlockingScanBackend(SimulatorBackend):
        block_scan = False

        def scan(self):
            if self.block_scan:
                entered.set()
                assert release.wait(1.0)
            return super().scan()

    backend = BlockingScanBackend()
    controller = AcquisitionController(backend_factories={"simulator": lambda: backend})
    try:
        controller.request("connect")
        wait_until(controller, lambda: controller.state.connection == "connected")
        backend.block_scan = True
        controller.request("scan")
        assert entered.wait(1.0)
        capture = tmp_path / "must-not-start.pmcap"
        controller.request("start", path=capture)
        controller.request("disconnect")
        release.set()
        wait_until(controller, lambda: controller.state.connection == "disconnected")
        assert controller.state.capture == "idle"
        assert not capture.exists()
    finally:
        release.set()
        controller.shutdown()


def test_stop_keeps_final_returned_batch_complete(tmp_path):
    entered = threading.Event()
    release = threading.Event()
    records = np.zeros(7, dtype=SAMPLE_DTYPE)
    records["index"] = np.arange(7)
    records["time_s"] = np.arange(7) / 5000
    records["host_elapsed_s"] = 1.0
    records["current_a"] = 0.001
    records["voltage_v"] = 3.3

    class FinalBatchAfterStop(SimulatorBackend):
        def sample_batches(self, cancel):
            entered.set()
            assert release.wait(1.0)
            yield SampleBatch(records)

    backend = FinalBatchAfterStop()
    controller = AcquisitionController(backend_factories={"simulator": lambda: backend})
    try:
        controller.request("connect")
        wait_until(controller, lambda: controller.state.connection == "connected")
        capture = tmp_path / "final-batch.pmcap"
        controller.request("start", path=capture)
        assert entered.wait(1.0)
        controller.request("stop")
        release.set()
        wait_until(controller, lambda: controller.state.capture == "idle")
        reader = CaptureReader(capture)
        assert reader.records["index"].tolist() == list(range(7))
        assert controller.state.sample_count == 7
    finally:
        release.set()
        controller.shutdown()


def test_capture_metadata_records_device_sdk_voltage_and_output_state(tmp_path):
    class RealStyleSimulator(SimulatorBackend):
        def connect(self, serial=None):
            super().connect(serial)
            return DeviceInfo(
                serial="HVPM-17",
                name="Monsoon HVPM",
                firmware="32",
                backend="monsoon",
            )

    backend = RealStyleSimulator()
    controller = AcquisitionController(backend_factories={"monsoon": lambda: backend})
    try:
        controller.request("connect", backend="monsoon")
        wait_until(controller, lambda: controller.state.connection == "connected")
        controller.request("disable_output")
        wait_until(controller, lambda: controller.state.output == "off")
        controller.request("set_voltage", voltage_v=4.2)
        wait_until(controller, lambda: controller.state.target_voltage_v == 4.2)
        controller.request("enable_output", confirmed=True)
        wait_until(controller, lambda: controller.state.output == "on")
        capture = tmp_path / "metadata.pmcap"
        controller.request("start", path=capture)
        wait_until(controller, lambda: controller.state.sample_count >= 1)
        controller.request("stop")
        wait_until(controller, lambda: controller.state.capture == "idle")

        metadata = CaptureReader(capture).metadata
        assert metadata["device_serial"] == "HVPM-17"
        assert metadata["device_name"] == "Monsoon HVPM"
        assert metadata["device_firmware"] == "32"
        assert metadata["sdk_revision"] == SDK_REVISION
        assert metadata["target_voltage_v"] == 4.2
        assert metadata["output_command_state"] == "on"
    finally:
        controller.shutdown()


def test_capture_error_event_preserves_original_failure(tmp_path):
    class FailingSampler(SimulatorBackend):
        def sample_batches(self, cancel):
            yield from ()
            raise OSError("USB read failed")

    backend = FailingSampler()
    controller = AcquisitionController(backend_factories={"simulator": lambda: backend})
    try:
        controller.request("connect")
        wait_until(controller, lambda: controller.state.connection == "connected")
        capture = tmp_path / "failed-event.pmcap"
        controller.request("start", path=capture)
        wait_until(controller, lambda: controller.state.capture == "error")
        events = [json.loads(line) for line in (capture / "events.jsonl").read_text().splitlines()]
        error = next(event for event in events if event["kind"] == "capture_error")
        assert error["payload"]["message"] == "USB read failed"
        assert controller.state.error == "USB read failed"
    finally:
        controller.shutdown()


def test_explicit_off_retries_owned_device_after_capture_error(tmp_path):
    class FailingSampler(SimulatorBackend):
        def sample_batches(self, cancel):
            yield from ()
            raise OSError("USB read failed")

    backend = FailingSampler()
    controller = AcquisitionController(backend_factories={"simulator": lambda: backend})
    try:
        controller.request("connect")
        wait_until(controller, lambda: controller.state.connection == "connected")
        controller.request("enable_output", confirmed=True)
        wait_until(controller, lambda: controller.state.output == "on")
        controller.request("start", path=tmp_path / "capture.pmcap")
        wait_until(controller, lambda: controller.state.capture == "error")
        assert controller.state.output == "unknown"
        controller.request("disable_output")
        wait_until(controller, lambda: controller.state.output == "off")
        assert not backend.output_enabled
    finally:
        controller.shutdown()
