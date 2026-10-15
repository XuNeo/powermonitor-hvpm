"""Backend interface and shared acquisition errors."""

from __future__ import annotations

from collections.abc import Generator
from pathlib import Path
from threading import Event
from typing import Protocol

from powermonitor.models import DeviceInfo, SampleBatch


class BackendError(RuntimeError):
    """A backend operation could not be completed safely."""


class AcquisitionCancelled(BackendError):
    """Acquisition was cancelled by the controller."""


class Backend(Protocol):
    def scan(self) -> list[DeviceInfo]: ...

    def connect(self, serial: str | None = None) -> DeviceInfo: ...

    def disconnect(self) -> None: ...

    def set_voltage(self, voltage_v: float) -> None: ...

    def disable_output(self) -> None: ...

    def sample_batches(self, cancel: Event) -> Generator[SampleBatch, None, None]: ...

    def stop_sampling(self) -> None: ...


BackendFactory = type[Backend]
CapturePath = Path
