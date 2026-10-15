"""Measure large-file plotting independently from the acquisition worker.

Reports timing plus the oscilloscope-style display composition: the current
line (every raw point when zoomed in, per-column means when dense) and the
optional min/max vertical envelope. The benchmark never assumes a fixed
number of curves.
"""

from __future__ import annotations

import argparse
import json
import time
from collections.abc import Mapping
from pathlib import Path

import psutil
from PySide6 import QtTest, QtWidgets

from powermonitor.storage import CaptureReader
from powermonitor.ui.plots import PowerPlotWidget


def _curve_points(curve: object) -> int:
    getter = getattr(curve, "getOriginalDataset", None)
    if not callable(getter):
        return 0
    dataset = getter()
    if not isinstance(dataset, tuple) or len(dataset) != 2:
        return 0
    x_values = dataset[0]
    try:
        return int(len(x_values))  # type: ignore[arg-type]
    except TypeError:
        return 0


def _as_int(value: object) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        result = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return result


def _view_range(widget: object) -> tuple[float, float, float, float] | None:
    plots = getattr(widget, "plots", None)
    try:
        view = plots[0].getViewBox().viewRange()  # type: ignore[index]
        x_axis, y_axis = view[0], view[1]
        return float(x_axis[0]), float(x_axis[1]), float(y_axis[0]), float(y_axis[1])
    except (AttributeError, IndexError, TypeError, ValueError):
        return None


def summarize_display(widget: object) -> dict[str, object]:
    """Describe the rendered curves without knowing reduction internals.

    If the widget exposes ``display_summary()`` the benchmark trusts it for
    display mode, raw visible count and envelope pairs. Otherwise it derives
    what it can from the populated PlotDataItem objects: a non-empty envelope
    means dense mode, an empty envelope means raw mode, and raw visible count
    falls back to the current line point count (labelled explicitly). The
    current view range is included so two equally-sized reductions over
    different windows still compare as different renders.
    """
    curves = getattr(widget, "curves", None)
    per_curve: dict[str, int] = {}
    if isinstance(curves, Mapping):
        for name, curve in curves.items():
            per_curve[str(name)] = _curve_points(curve)
    envelope_key = next((name for name in per_curve if "envelope" in name.lower()), None)
    current_key = (
        "current"
        if "current" in per_curve
        else next(
            (name for name in per_curve if name != envelope_key),
            None,
        )
    )
    current_points = per_curve[current_key] if current_key is not None else 0
    envelope_points = per_curve[envelope_key] if envelope_key is not None else 0

    widget_info: Mapping[str, object] = {}
    provider = getattr(widget, "display_summary", None)
    if callable(provider):
        try:
            candidate = provider()
        except Exception:
            candidate = None
        if isinstance(candidate, Mapping):
            widget_info = candidate

    mode = widget_info.get("mode")
    if not isinstance(mode, str) or not mode:
        if envelope_key is None:
            mode = "unknown"
        else:
            mode = "aggregate" if envelope_points else "raw"

    raw_source = "widget"
    raw_visible = _as_int(widget_info.get("raw_visible_count"))
    if raw_visible is None:
        raw_visible = current_points
        raw_source = "current_curve_points"

    envelope_pairs = _as_int(widget_info.get("envelope_pairs"))
    if envelope_pairs is None:
        envelope_pairs = envelope_points // 2 if envelope_key is not None else 0

    return {
        "curve_count": len(per_curve),
        "view_range": list(view_range) if (view_range := _view_range(widget)) else None,
        "display_mode": mode,
        "raw_visible_count": raw_visible,
        "raw_visible_count_source": raw_source,
        "current_line_points": current_points,
        "envelope_points": envelope_points,
        "envelope_pairs": envelope_pairs,
        "curve_points": per_curve,
    }


def _curve_extent(curve: object) -> tuple[int, float | None, float | None]:
    getter = getattr(curve, "getOriginalDataset", None)
    if not callable(getter):
        return (0, None, None)
    dataset = getter()
    if not isinstance(dataset, tuple) or len(dataset) != 2:
        return (0, None, None)
    x_values = dataset[0]
    try:
        count = int(len(x_values))  # type: ignore[arg-type]
    except TypeError:
        return (0, None, None)
    if not count:
        return (0, None, None)
    try:
        return count, float(x_values[0]), float(x_values[-1])  # type: ignore[index]
    except (TypeError, ValueError, IndexError):
        return (count, None, None)


def render_key(widget: object) -> object:
    """What actually changed once the curves were (re)rendered.

    Point counts plus each curve's first/last X distinguish two equally-sized
    reductions over different time windows. The live viewport range is
    deliberately excluded: it changes on setXRange before the deferred render.
    """
    curves = getattr(widget, "curves", None)
    if not isinstance(curves, Mapping):
        return None
    summary = summarize_display(widget)
    extents = {str(name): _curve_extent(curve) for name, curve in curves.items()}
    return (
        summary["display_mode"],
        summary["current_line_points"],
        summary["envelope_points"],
        tuple(sorted(extents.items())),
    )


def settle_display(
    app: QtWidgets.QApplication,
    widget: object,
    baseline_key: object,
    *,
    timeout_s: float = 2.0,
) -> dict[str, object]:
    """Pump until the rendered curves differ from baseline, return a summary.

    Tries a synchronous event pump first so immediately-rendered views add no
    wait; deferred renders (33 ms coalescing timer, macOS 2 Hz live throttle)
    are pumped until the curve data actually changes. The returned timing
    therefore covers the deferred render latency, not just the range call.
    """
    app.processEvents()
    if render_key(widget) != baseline_key:
        return summarize_display(widget)
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        QtTest.QTest.qWait(5)
        app.processEvents()
        if render_key(widget) != baseline_key:
            # The render timer is single-shot; one more pump confirms stability.
            QtTest.QTest.qWait(5)
            app.processEvents()
            return summarize_display(widget)
    return summarize_display(widget)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("capture", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    app = QtWidgets.QApplication([])
    widget = PowerPlotWidget()
    widget.resize(1100, 900)
    widget.show()
    app.processEvents()
    process = psutil.Process()
    baseline = render_key(widget)
    started = time.monotonic()
    reader = CaptureReader(args.capture)
    open_s = time.monotonic() - started
    started = time.monotonic()
    widget.set_records(reader.records, full_bounds=reader.time_bounds)
    overview_display = settle_display(app, widget, baseline)
    render_s = time.monotonic() - started
    overview_key = render_key(widget)
    started = time.monotonic()
    widget.plots[0].getViewBox().setXRange(100.0, 101.0, padding=0)
    zoom_display = settle_display(app, widget, overview_key)
    zoom_s = time.monotonic() - started
    zoom_key = render_key(widget)
    started = time.monotonic()
    # 0.2 s holds about 1000 raw 5 kHz samples, narrow enough to enter raw
    # mode on a desktop-width plot; the reported mode is observed, not assumed.
    widget.plots[0].getViewBox().setXRange(100.40, 100.60, padding=0)
    raw_display = settle_display(app, widget, zoom_key)
    raw_zoom_s = time.monotonic() - started
    result = {
        "samples": len(reader.records),
        "reader_validate_s": open_s,
        "full_overview_s": render_s,
        "zoom_one_second_s": zoom_s,
        "zoom_raw_window_s": raw_zoom_s,
        "display_full_overview": overview_display,
        "display_zoom_one_second": zoom_display,
        "display_zoom_raw_window": raw_display,
        "rss_mb": process.memory_info().rss / 1024**2,
        "note": (
            "RSS includes read-only file-backed pages; preprocessing allocations are chunk "
            "bounded. Render timings include the Qt event pump until curves settle. "
            "raw_visible_count_source=current_curve_points means the widget exposed no "
            "display_summary() helper, so aggregate mode reports mean-line points rather than raw "
            "samples; display_mode=unknown means no envelope curve was present."
        ),
    }
    widget.close()
    args.output.write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
