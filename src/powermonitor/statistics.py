"""Window statistics on original sample support intervals, never plot data."""

import math
from collections.abc import Iterable
from dataclasses import dataclass

import numpy as np

from .models import SAMPLE_PERIOD_S, Quality, SampleBatch


@dataclass(frozen=True)
class WindowStatistics:
    start_s: float
    end_s: float
    sample_count: int
    valid_sample_count: int
    valid_duration_s: float
    coverage: float
    mean_current_a: float | None
    min_current_a: float | None
    max_current_a: float | None
    mean_voltage_v: float | None
    mean_power_w: float | None
    charge_c: float | None
    energy_j: float | None
    invalid_count: int
    loss_unknown: bool
    gap_count: int

    @property
    def duration_s(self) -> float:
        return self.end_s - self.start_s

    @property
    def charge_mah(self) -> float | None:
        return None if self.charge_c is None else self.charge_c / 3.6

    @property
    def energy_mwh(self) -> float | None:
        return None if self.energy_j is None else self.energy_j / 3.6


def compute_statistics(
    records: np.ndarray | Iterable[np.ndarray], start: float, end: float
) -> WindowStatistics:
    """Integrate fixed-width samples over the half-open interval [start, end).

    All reported means share coverage: current, voltage and their product must
    be finite and the record must not be marked INVALID or DEVICE_ERROR. A
    LOSS_UNKNOWN flag does not discard received values, but makes their time
    basis uncertain. Missing periods are never bridged or silently filled.
    """
    if not math.isfinite(start) or not math.isfinite(end) or start < 0 or end <= start:
        raise ValueError("Select a finite, positive-duration time window")
    chunks = (records,) if isinstance(records, np.ndarray) else records
    sample_count = valid_count = invalid_count = gap_count = 0
    duration_parts, charge_parts, voltage_parts, energy_parts = [], [], [], []
    minimum, maximum = math.inf, -math.inf
    loss_unknown = False
    previous_time = previous_index = None
    for chunk in chunks:
        SampleBatch(chunk)
        if not len(chunk):
            continue
        if previous_time is not None:
            if chunk["time_s"][0] < previous_time + SAMPLE_PERIOD_S * (1 - 1e-6):
                raise ValueError("Sample chunks overlap or are out of order")
            if chunk["index"][0] <= previous_index:
                raise ValueError("Sample indices overlap across chunks")
        # The first overlapping support can start just before the requested window.
        t = chunk["time_s"]
        weights = np.minimum(t + SAMPLE_PERIOD_S, end) - np.maximum(t, start)
        selected = weights > 1e-12
        gaps = np.zeros(len(chunk), dtype=bool)
        gaps[1:] = np.diff(t) > SAMPLE_PERIOD_S * (1 + 1e-6)
        if previous_time is not None:
            gaps[0] = t[0] - previous_time > SAMPLE_PERIOD_S * (1 + 1e-6)
        gaps |= (chunk["flags"] & int(Quality.GAP_BEFORE)) != 0
        previous_time, previous_index = float(t[-1]), int(chunk["index"][-1])
        if not np.any(selected):
            continue
        a, w = chunk[selected], weights[selected]
        sample_count += len(a)
        gap_count += int(np.count_nonzero(gaps & selected))
        loss_unknown |= bool(np.any(a["flags"] & int(Quality.LOSS_UNKNOWN)))
        current, voltage = a["current_a"], a["voltage_v"]
        with np.errstate(over="ignore", invalid="ignore"):
            power = current * voltage
        valid = np.isfinite(current) & np.isfinite(voltage) & np.isfinite(power)
        valid &= (a["flags"] & int(Quality.INVALID | Quality.DEVICE_ERROR)) == 0
        valid_count += int(np.count_nonzero(valid))
        invalid_count += int(np.count_nonzero(~valid))
        if not np.any(valid):
            continue
        w, current, voltage, power = w[valid], current[valid], voltage[valid], power[valid]
        duration_parts.append(float(np.sum(w)))
        charge_parts.append(float(np.sum(current * w)))
        voltage_parts.append(float(np.sum(voltage * w)))
        energy_parts.append(float(np.sum(power * w)))
        minimum = min(minimum, float(np.min(current)))
        maximum = max(maximum, float(np.max(current)))
    duration = math.fsum(duration_parts)
    charge = math.fsum(charge_parts) if duration else None
    energy = math.fsum(energy_parts) if duration else None
    return WindowStatistics(
        start_s=start,
        end_s=end,
        sample_count=sample_count,
        valid_sample_count=valid_count,
        valid_duration_s=duration,
        coverage=min(1.0, duration / (end - start)),
        mean_current_a=charge / duration if charge is not None and duration else None,
        min_current_a=minimum if duration else None,
        max_current_a=maximum if duration else None,
        mean_voltage_v=math.fsum(voltage_parts) / duration if duration else None,
        mean_power_w=energy / duration if energy is not None and duration else None,
        charge_c=charge,
        energy_j=energy,
        invalid_count=invalid_count,
        loss_unknown=loss_unknown,
        gap_count=gap_count,
    )
