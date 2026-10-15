"""Deterministic Monsoon HVPM simulator."""

from __future__ import annotations

import time
from collections.abc import Generator
from threading import Event

import numpy as np

from powermonitor.backends.base import BackendError
from powermonitor.models import (
    SAMPLE_DTYPE,
    SAMPLE_PERIOD_S,
    SAMPLE_RATE_HZ,
    DeviceInfo,
    SampleBatch,
    validate_voltage,
)


class SimulatorBackend:
    """A deterministic backend with real-time paced batches and bounded stop latency."""

    def __init__(self, batch_size: int = 250) -> None:
        if batch_size <= 0:
            raise ValueError("batch_size must be positive")
        self.batch_size = batch_size
        self.connected = False
        self.output_enabled = False
        self.target_voltage_v = 3.3
        self._sampling = False

    def scan(self) -> list[DeviceInfo]:
        return [DeviceInfo(serial="SIM-HVPM-0001", firmware="simulator", backend="simulator")]

    def connect(self, serial: str | None = None) -> DeviceInfo:
        if serial not in (None, "SIM-HVPM-0001"):
            raise BackendError(f"Simulator device not found: {serial}")
        self.connected = True
        self.output_enabled = False
        return self.scan()[0]

    def disconnect(self) -> None:
        if self._sampling:
            raise BackendError("Cannot disconnect while sampling")
        self.connected = False
        self.output_enabled = False

    def set_voltage(self, voltage_v: float) -> None:
        self._require_connected()
        self.target_voltage_v = validate_voltage(voltage_v)
        self.output_enabled = True

    def disable_output(self) -> None:
        self._require_connected()
        self.output_enabled = False

    def sample_batches(self, cancel: Event) -> Generator[SampleBatch, None, None]:
        self._require_connected()
        self._sampling = True
        index = 0
        deadline = time.monotonic()
        try:
            while not cancel.is_set():
                count = self.batch_size
                records = np.empty(count, dtype=SAMPLE_DTYPE)
                indices = np.arange(index, index + count, dtype=np.uint64)
                seconds = indices.astype(np.float64) * SAMPLE_PERIOD_S
                current = 0.040 + 0.010 * np.sin(2 * np.pi * 7 * seconds)
                voltage = self.target_voltage_v if self.output_enabled else 0.0
                records["index"] = indices
                records["time_s"] = seconds
                records["host_elapsed_s"] = seconds
                records["current_a"] = current if self.output_enabled else 0.0
                records["voltage_v"] = voltage
                records["flags"] = 0
                records["dropped"] = 0
                index += count
                yield SampleBatch(records)
                deadline += count / SAMPLE_RATE_HZ
                wait_s = deadline - time.monotonic()
                if wait_s > 0 and cancel.wait(wait_s):
                    break
        finally:
            self._sampling = False

    def stop_sampling(self) -> None:
        self._sampling = False

    def _require_connected(self) -> None:
        if not self.connected:
            raise BackendError("Simulator is not connected")
