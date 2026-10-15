"""Small formatting helpers for the Qt UI."""

from __future__ import annotations

import math


def format_time_s(value: float | None, *, decimals: int = 6) -> str:
    if value is None or not math.isfinite(value):
        return "n/a"
    return f"{value:.{decimals}f} s"


def format_ratio(value: float | None) -> str:
    if value is None or not math.isfinite(value):
        return "n/a"
    return f"{value * 100:.2f} %"


def format_current_a(value: float | None) -> str:
    if value is None or not math.isfinite(value):
        return "n/a"
    magnitude = abs(value)
    if magnitude < 1e-3:
        return f"{value * 1e6:.3f} uA"
    if magnitude < 1:
        return f"{value * 1e3:.3f} mA"
    return f"{value:.6f} A"


def format_voltage_v(value: float | None) -> str:
    if value is None or not math.isfinite(value):
        return "n/a"
    return f"{value:.4f} V"


def format_power_w(value: float | None) -> str:
    if value is None or not math.isfinite(value):
        return "n/a"
    magnitude = abs(value)
    if magnitude < 1e-3:
        return f"{value * 1e6:.3f} uW"
    if magnitude < 1:
        return f"{value * 1e3:.3f} mW"
    return f"{value:.6f} W"


def format_charge_c(value: float | None) -> str:
    if value is None or not math.isfinite(value):
        return "n/a"
    return f"{value:.9f} C"


def format_charge_mah(value: float | None) -> str:
    if value is None or not math.isfinite(value):
        return "n/a"
    return f"{value:.6f} mAh"


def format_energy_j(value: float | None) -> str:
    if value is None or not math.isfinite(value):
        return "n/a"
    return f"{value:.9f} J"


def format_energy_mwh(value: float | None) -> str:
    if value is None or not math.isfinite(value):
        return "n/a"
    return f"{value:.6f} mWh"


def format_count(value: int | None) -> str:
    if value is None:
        return "n/a"
    return f"{int(value):,}"
