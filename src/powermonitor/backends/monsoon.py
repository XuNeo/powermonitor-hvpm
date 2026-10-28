"""Safe adapter for the pinned official Monsoon HVPM SDK."""

from __future__ import annotations

import hashlib
import inspect
import time
from collections.abc import Callable, Generator
from threading import Event
from typing import Any

import numpy as np

from powermonitor.backends.base import AcquisitionCancelled, BackendError
from powermonitor.models import (
    SAMPLE_DTYPE,
    SAMPLE_PERIOD_S,
    SDK_REVISION,
    DeviceInfo,
    Quality,
    SampleBatch,
    validate_voltage,
)

# The upstream package reports 0.1.88 at the approved commit. These hashes pin the
# vendored implementation after the documented mechanical import and whitespace
# changes; the package version alone cannot identify this private packet path.
_EXPECTED_SDK = {
    "SampleEngine._SampleEngine__sampleLoop": (
        "2905404ad523cf958cf7d31f8df6cfec8239bd14a461d81d4de6109cc3bbc293"
    ),
    "SampleEngine.periodicStartSampling": (
        "9dbdb9f555b989a3d5a3dc978e6059109ef3e4e680df7ff3ba62033cca71ea12"
    ),
    "SampleEngine.getSamples": ("ecea3062316a79e18552090ef2548600648e175d4333d50a2992fe5093467bc8"),
    "SampleEngine.periodicStopSampling": (
        "2ab25a2f2fa1bc3b0277d2c32f9d077e89acfb68a36a97e9a43c46c600b7623f"
    ),
    "HVPM.Monsoon.swizzlePacket": (
        "dfd355e7fd9ca4705268416121b71ab61f15369e1ec92c77fb0f877a0054dc78"
    ),
}
_ONE_PACKET = 65


def _source_hash(value: Any) -> str:
    source = inspect.getsource(value).replace("\r\n", "\n").encode()
    return hashlib.sha256(source).hexdigest()


def verify_sdk_revision(sample_engine_class: type[Any], device_class: type[Any]) -> None:
    """Reject an SDK whose private packet path differs from the approved revision."""

    symbols = {
        "SampleEngine._SampleEngine__sampleLoop": getattr(
            sample_engine_class, "_SampleEngine__sampleLoop", None
        ),
        "SampleEngine.periodicStartSampling": getattr(
            sample_engine_class, "periodicStartSampling", None
        ),
        "SampleEngine.getSamples": getattr(sample_engine_class, "getSamples", None),
        "SampleEngine.periodicStopSampling": getattr(
            sample_engine_class, "periodicStopSampling", None
        ),
        "HVPM.Monsoon.swizzlePacket": getattr(device_class, "swizzlePacket", None),
    }
    for name, symbol in symbols.items():
        if symbol is None or _source_hash(symbol) != _EXPECTED_SDK[name]:
            raise BackendError(
                f"Unsupported Monsoon SDK implementation; expected revision {SDK_REVISION}"
            )


class MonsoonBackend:
    """HVPM main-channel adapter; every method is called by one owning thread."""

    def __init__(
        self,
        *,
        device_factory: Callable[[], Any] | None = None,
        engine_factory: Callable[[Any], Any] | None = None,
        reset_on_close: bool = True,
        no_data_timeout_s: float = 5.0,
        startup_timeout_s: float = 15.0,
        batch_samples: int = 250,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if device_factory is None or engine_factory is None:
            from powermonitor._vendor.pymonsoon import HVPM, sampleEngine

            verify_sdk_revision(sampleEngine.SampleEngine, HVPM.Monsoon)
            device_factory = device_factory or HVPM.Monsoon
            engine_factory = engine_factory or sampleEngine.SampleEngine
        self._device_factory = device_factory
        self._engine_factory = engine_factory
        self._reset_on_close = reset_on_close
        if no_data_timeout_s <= 0 or startup_timeout_s <= 0 or batch_samples < 1:
            raise ValueError("Sampling deadlines and batch size must be positive")
        self._no_data_timeout_s = no_data_timeout_s
        self._startup_timeout_s = startup_timeout_s
        self._batch_samples = batch_samples
        self._startup_deadline: float | None = None
        self._clock = clock
        self._device: Any | None = None
        self._engine: Any | None = None
        self._sampling = False
        self._cancel: Event | None = None
        self._sample_index = 0
        self._sampling_started = 0.0
        self._last_dropped: int | None = None
        self._loss_unknown = False
        self._last_error_state = False

    def scan(self) -> list[DeviceInfo]:
        device = self._device_factory()
        try:
            serials = device.enumerateDevices()
        except Exception as exc:
            raise BackendError(f"Unable to enumerate Monsoon devices: {exc}") from exc
        return [DeviceInfo(serial=str(serial), backend="monsoon") for serial in serials]

    def connect(self, serial: str | None = None) -> DeviceInfo:
        if self._device is not None:
            raise BackendError("Monsoon device is already connected")
        device = self._device_factory()
        try:
            device.setup_usb(serialno=serial)
            protocol = getattr(device, "Protocol", None)
            if protocol is None or getattr(protocol, "DEVICE", None) is None:
                raise BackendError("Monsoon SDK returned without a USB device handle")
            device.fillStatusPacket()
            status = device.statusPacket
            from powermonitor._vendor.pymonsoon import Operations

            if status.hardwareModel != Operations.HardwareModel.HVPM:
                raise BackendError(
                    f"Connected device is not an HVPM (hardware model {status.hardwareModel!r})"
                )
            info = DeviceInfo(
                serial=str(status.serialNumber),
                firmware=str(status.firmwareVersion),
                backend="monsoon",
            )
        except Exception:
            self._best_effort_close(device)
            raise
        self._device = device
        return info

    def disconnect(self) -> None:
        if self._sampling:
            raise BackendError("Cannot disconnect while sampling")
        device = self._device
        if device is None:
            return
        try:
            self._close_device(device)
        except Exception as exc:
            raise BackendError(f"Unable to close Monsoon device: {exc}") from exc
        self._device = None
        self._engine = None

    def set_voltage(self, voltage_v: float) -> None:
        device = self._require_device()
        voltage = validate_voltage(voltage_v)
        try:
            device.setVout(voltage)
        except Exception as exc:
            raise BackendError(f"Unable to enable HVPM output: {exc}") from exc

    def disable_output(self) -> None:
        device = self._require_device()
        try:
            device.setVout(0)
        except Exception as exc:
            raise BackendError(f"Unable to disable HVPM output: {exc}") from exc

    def sample_batches(self, cancel: Event) -> Generator[SampleBatch, None, None]:
        device = self._require_device()
        engine: Any = self._engine_factory(device)
        engine.ConsoleOutput(False)
        self._install_cancellable_bulk_read(device, cancel)
        self._engine = engine
        self._cancel = cancel
        self._sample_index = 0
        self._last_dropped = None
        self._loss_unknown = False
        self._last_error_state = False
        self._sampling_started = self._clock()
        self._startup_deadline = self._sampling_started + self._startup_timeout_s
        self._sampling = True
        pending: list[np.ndarray] = []
        pending_count = 0
        unrecorded_loss = False
        try:
            if cancel.is_set():
                raise AcquisitionCancelled("Sampling cancelled before startup")
            startup_result = engine.periodicStartSampling()
            self._startup_deadline = None
            if startup_result is False:
                raise BackendError("Monsoon calibration failed")
            last_measurement = self._clock()
            while True:
                if cancel.is_set():
                    break
                before_error = bool(getattr(engine, "_SampleEngine__errorState", False))
                one_packet = [[0] * _ONE_PACKET]
                engine._SampleEngine__sampleLoop(0, one_packet, 1)
                samples = engine.getSamples()
                after_dropped = int(getattr(engine, "dropped", 0))
                after_error = bool(getattr(engine, "_SampleEngine__errorState", False))
                if before_error or after_error:
                    self._last_error_state = True
                loss_changed = (self._last_dropped is None and after_dropped != 0) or (
                    self._last_dropped is not None and after_dropped != self._last_dropped
                )
                if loss_changed:
                    self._loss_unknown = True
                    unrecorded_loss = True
                    for records in pending:
                        records["flags"] |= int(Quality.LOSS_UNKNOWN)
                self._last_dropped = after_dropped
                batch = self._make_batch(samples, after_dropped)
                if len(batch):
                    last_measurement = self._clock()
                    pending.append(batch.records)
                    pending_count += len(batch)
                elif self._clock() - last_measurement >= self._no_data_timeout_s:
                    raise BackendError("Timed out waiting for calibrated HVPM measurements")
                if pending and (
                    pending_count >= self._batch_samples
                    or self._last_error_state
                    or cancel.is_set()
                ):
                    output = SampleBatch(np.concatenate(pending))
                    pending, pending_count = [], 0
                    if np.any(output.records["flags"] & int(Quality.LOSS_UNKNOWN)):
                        unrecorded_loss = False
                    yield output
                if self._last_error_state:
                    raise BackendError("HVPM reported a sticky device error")
                if cancel.is_set():
                    if unrecorded_loss:
                        raise BackendError(
                            "HVPM reported sample loss but no measurement was available to record"
                        )
                    break
        except AcquisitionCancelled:
            pass
        except Exception as exc:
            if pending:
                retained = np.concatenate(pending)
                retained["flags"] |= int(Quality.LOSS_UNKNOWN)
                pending = []
                yield SampleBatch(retained)
                unrecorded_loss = False
            if unrecorded_loss and "sample loss" not in str(exc):
                raise BackendError(
                    f"HVPM reported sample loss but no measurement was available to record; {exc}"
                ) from exc
            raise
        finally:
            self._startup_deadline = None
            self._restore_bulk_read(device)
            self._sampling = False
            self._cancel = None
        if pending:
            yield SampleBatch(np.concatenate(pending))

    def stop_sampling(self) -> None:
        engine = self._engine
        if engine is None:
            return
        try:
            engine.periodicStopSampling()
        except Exception as exc:
            raise BackendError(f"Unable to stop Monsoon sampling: {exc}") from exc
        self._engine = None

    def _make_batch(self, samples: Any, dropped: int) -> SampleBatch:
        if not samples or len(samples) <= 4:
            return SampleBatch.empty()
        host_times = np.asarray(samples[0], dtype=np.float64)
        current_ma = np.asarray(samples[1], dtype=np.float64)
        voltage_v = np.asarray(samples[4], dtype=np.float64)
        if not (len(host_times) == len(current_ma) == len(voltage_v)):
            raise BackendError("Monsoon sample channel lengths do not match")
        count = len(host_times)
        if count == 0:
            return SampleBatch.empty()
        start = self._sample_index
        indices = np.arange(start, start + count, dtype=np.uint64)
        records = np.empty(count, dtype=SAMPLE_DTYPE)
        records["index"] = indices
        records["time_s"] = indices.astype(np.float64) * SAMPLE_PERIOD_S
        records["host_elapsed_s"] = host_times[:count]
        records["current_a"] = current_ma[:count] / 1000.0
        records["voltage_v"] = voltage_v[:count]
        flags = Quality.GOOD
        if self._loss_unknown:
            flags |= Quality.LOSS_UNKNOWN
        if self._last_error_state:
            flags |= Quality.DEVICE_ERROR
        invalid = ~np.isfinite(records["current_a"]) | ~np.isfinite(records["voltage_v"])
        records["flags"] = int(flags)
        records["flags"][invalid] |= int(Quality.INVALID)
        records["dropped"] = min(max(dropped, 0), np.iinfo(np.uint16).max)
        self._sample_index += count
        return SampleBatch(records)

    def _install_cancellable_bulk_read(self, device: Any, cancel: Event) -> None:
        original = device.BulkRead
        last_data = self._clock()

        from usb.core import USBTimeoutError

        def bulk_read() -> Any:
            nonlocal last_data
            while True:
                now = self._clock()
                if cancel.is_set():
                    raise AcquisitionCancelled("Sampling cancelled")
                if self._startup_deadline is not None and now >= self._startup_deadline:
                    raise BackendError("HVPM calibration startup timed out")
                if now - last_data >= self._no_data_timeout_s:
                    raise BackendError("Timed out waiting for HVPM sample data")
                try:
                    data = original()
                except USBTimeoutError as exc:
                    now = self._clock()
                    if cancel.is_set():
                        raise AcquisitionCancelled("Sampling cancelled") from exc
                    if self._startup_deadline is not None and now >= self._startup_deadline:
                        raise BackendError("HVPM calibration startup timed out") from exc
                    if now - last_data >= self._no_data_timeout_s:
                        raise BackendError("Timed out waiting for HVPM sample data") from exc
                    continue
                if len(data):
                    last_data = self._clock()
                    return data
                if self._clock() - last_data >= self._no_data_timeout_s:
                    raise BackendError("Timed out waiting for HVPM sample data")
                return data

        self._original_bulk_read = original
        device.BulkRead = bulk_read

    def _restore_bulk_read(self, device: Any) -> None:
        original = getattr(self, "_original_bulk_read", None)
        if original is not None:
            device.BulkRead = original
            del self._original_bulk_read

    def _require_device(self) -> Any:
        if self._device is None:
            raise BackendError("Monsoon device is not connected")
        return self._device

    def _close_device(self, device: Any) -> None:
        protocol = getattr(device, "Protocol", None)
        handle = getattr(protocol, "DEVICE", None)
        if handle is None:
            return
        if self._reset_on_close:
            device.closeDevice()
        else:
            # The SDK close path resets USB; capture must only release host resources.
            from usb.util import dispose_resources

            dispose_resources(handle)

    def _best_effort_close(self, device: Any) -> None:
        try:
            self._close_device(device)
        except Exception:
            pass
