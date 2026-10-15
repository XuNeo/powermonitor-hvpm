"""Shared data contracts; all stored measurements use SI units."""

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from enum import IntFlag
from pathlib import Path
from typing import Any

import numpy as np

SAMPLE_RATE_HZ = 5000
SAMPLE_PERIOD_S = 1 / SAMPLE_RATE_HZ
SDK_REVISION = "1afab10eac4468bd54f0f2256a9ed870eb5346c4"
SAMPLE_DTYPE = np.dtype(
    [
        ("index", "<u8"),
        ("time_s", "<f8"),
        ("host_elapsed_s", "<f8"),
        ("current_a", "<f8"),
        ("voltage_v", "<f8"),
        ("flags", "<u2"),
        ("dropped", "<u2"),
    ]
)


class Quality(IntFlag):
    GOOD = 0
    LOSS_UNKNOWN = 1
    GAP_BEFORE = 2
    DEVICE_ERROR = 4
    INVALID = 8


@dataclass(frozen=True)
class DeviceInfo:
    serial: str
    name: str = "Monsoon HVPM"
    firmware: str = "unknown"
    backend: str = "monsoon"


@dataclass(frozen=True)
class SampleBatch:
    records: np.ndarray

    def __post_init__(self) -> None:
        a = self.records
        if a.dtype != SAMPLE_DTYPE or a.ndim != 1:
            raise ValueError("Samples must be a one-dimensional SAMPLE_DTYPE array")
        if len(a) == 0:
            return
        t = a["time_s"]
        if not np.all(np.isfinite(t)) or np.any(t < 0):
            raise ValueError("Sample time must be finite and nonnegative")
        if np.any(np.diff(t) < SAMPLE_PERIOD_S * (1 - 1e-6)):
            raise ValueError("Sample support intervals must not overlap")
        if np.any(a["index"][1:] <= a["index"][:-1]):
            raise ValueError("Sample indices must increase")

    def __len__(self) -> int:
        return len(self.records)

    @classmethod
    def empty(cls) -> "SampleBatch":
        return cls(np.empty(0, dtype=SAMPLE_DTYPE))

    @property
    def power_w(self) -> np.ndarray:
        return self.records["current_a"] * self.records["voltage_v"]


@dataclass(frozen=True)
class ControllerState:
    backend: str = "simulator"
    connection: str = "disconnected"
    output: str = "unknown"
    capture: str = "idle"
    target_voltage_v: float = 3.3
    device: DeviceInfo | None = None
    recording_path: Path | None = None
    sample_count: int = 0
    error: str = ""
    output_owned: bool = False


@dataclass(frozen=True)
class ControllerEvent:
    kind: str
    payload: Any = None


def validate_voltage(value: float) -> float:
    try:
        voltage = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError("Voltage must be a number") from exc
    if not voltage.is_finite() or not Decimal("0.8") <= voltage <= Decimal("13.5"):
        raise ValueError("HVPM voltage must be between 0.80 and 13.50 V")
    if voltage % Decimal("0.01") != 0:
        raise ValueError("HVPM voltage must use 0.01 V steps")
    return float(voltage)
