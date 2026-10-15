"""pyqtgraph-based waveform panel for live and replay data."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Callable, Literal, cast

import numpy as np
import pyqtgraph as pg
from PySide6 import QtCore, QtWidgets

from powermonitor.models import SAMPLE_DTYPE, SAMPLE_PERIOD_S, Quality

from .formatting import format_current_a, format_time_s
from .theme import LIGHT, ChartTheme

_RENDER_INTERVAL_MS = 33
_ZOOM_HISTORY_LIMIT = 64


@dataclass(frozen=True)
class DisplayData:
    """Waveform arrays ready for direct pyqtgraph rendering."""

    mode: Literal["raw", "aggregate"]
    line_x: np.ndarray
    line_y: np.ndarray
    envelope_x: np.ndarray
    envelope_y: np.ndarray


@dataclass(frozen=True)
class _ZoomState:
    x_range: tuple[float, float]
    y_range: tuple[float, float]


def build_display_data(
    records: np.ndarray,
    lo: float,
    hi: float,
    columns: int,
) -> DisplayData:
    """Build truthful raw or pixel-bucketed current display arrays."""

    empty = np.empty(0, dtype=np.float64)
    columns = max(1, int(columns))
    if not len(records) or not math.isfinite(lo) or not math.isfinite(hi) or hi <= lo:
        return DisplayData("raw", empty, empty, empty, empty)

    times = records["time_s"]
    start = int(np.searchsorted(times, lo, side="left"))
    end = int(np.searchsorted(times, hi, side="left"))
    visible = records[start:end]
    if not len(visible):
        return DisplayData("raw", empty, empty, empty, empty)

    times = visible["time_s"]
    values = visible["current_a"]

    if len(visible) <= 2 * columns:
        flags = visible["flags"]
        valid = np.isfinite(times) & np.isfinite(values)
        valid &= (flags & int(Quality.INVALID | Quality.DEVICE_ERROR)) == 0
        breaks = ~valid
        breaks |= (flags & int(Quality.GAP_BEFORE)) != 0
        if len(visible) > 1:
            breaks[1:] |= np.diff(times) > SAMPLE_PERIOD_S * (1 + 1e-6)
        line_x = times.astype(np.float64, copy=True)
        line_y = values.astype(np.float64, copy=True)
        line_y[~valid] = np.nan
        gap_positions = np.flatnonzero(breaks & valid)
        gap_positions = gap_positions[gap_positions > 0]
        if len(gap_positions):
            targets = np.arange(len(line_x)) + np.searchsorted(
                gap_positions, np.arange(len(line_x)), side="right"
            )
            raw_x, raw_y = line_x, line_y
            line_x = np.empty(len(raw_x) + len(gap_positions), dtype=np.float64)
            line_y = np.empty(len(raw_y) + len(gap_positions), dtype=np.float64)
            line_x[targets] = raw_x
            line_y[targets] = raw_y
            sentinels = targets[gap_positions] - 1
            line_x[sentinels] = raw_x[gap_positions]
            line_y[sentinels] = np.nan
        return DisplayData("raw", line_x, line_y, empty, empty)

    group_buckets: list[np.ndarray] = []
    group_segments: list[np.ndarray] = []
    group_counts: list[np.ndarray] = []
    group_sums: list[np.ndarray] = []
    group_minima: list[np.ndarray] = []
    group_maxima: list[np.ndarray] = []
    segment_offset = 0
    previous_time: float | None = None
    chunk_size = 100_000
    for chunk_start in range(0, len(visible), chunk_size):
        chunk = visible[chunk_start : chunk_start + chunk_size]
        chunk_times = chunk["time_s"]
        chunk_values = chunk["current_a"]
        chunk_flags = chunk["flags"]
        valid = np.isfinite(chunk_times) & np.isfinite(chunk_values)
        valid &= (chunk_flags & int(Quality.INVALID | Quality.DEVICE_ERROR)) == 0
        breaks = ~valid
        breaks |= (chunk_flags & int(Quality.GAP_BEFORE)) != 0
        if len(chunk) > 1:
            breaks[1:] |= np.diff(chunk_times) > SAMPLE_PERIOD_S * (1 + 1e-6)
        if previous_time is not None:
            breaks[0] |= chunk_times[0] - previous_time > SAMPLE_PERIOD_S * (1 + 1e-6)
        segments = segment_offset + np.cumsum(breaks, dtype=np.int64)
        segment_offset = int(segments[-1])
        previous_time = float(chunk_times[-1])
        if not np.any(valid):
            continue
        buckets = np.floor((chunk_times - lo) * columns / (hi - lo)).astype(np.int64)
        np.clip(buckets, 0, columns - 1, out=buckets)
        good_buckets = buckets[valid]
        good_segments = segments[valid]
        good_values = chunk_values[valid]
        group_start = np.r_[
            True,
            (np.diff(good_segments) != 0) | (np.diff(good_buckets) != 0),
        ]
        starts = np.flatnonzero(group_start)
        group_buckets.append(good_buckets[starts])
        group_segments.append(good_segments[starts])
        group_counts.append(np.diff(np.r_[starts, len(good_values)]))
        group_sums.append(np.add.reduceat(good_values, starts))
        group_minima.append(np.minimum.reduceat(good_values, starts))
        group_maxima.append(np.maximum.reduceat(good_values, starts))
    if not group_buckets:
        return DisplayData("aggregate", empty, empty, empty, empty)

    group_bucket = np.concatenate(group_buckets)
    group_segment = np.concatenate(group_segments)
    counts = np.concatenate(group_counts)
    sums = np.concatenate(group_sums)
    minima = np.concatenate(group_minima)
    maxima = np.concatenate(group_maxima)
    same_as_previous = np.r_[
        False,
        (np.diff(group_segment) == 0) & (np.diff(group_bucket) == 0),
    ]
    if np.any(same_as_previous):
        merge_starts = np.flatnonzero(~same_as_previous)
        counts = np.add.reduceat(counts, merge_starts)
        sums = np.add.reduceat(sums, merge_starts)
        minima = np.minimum.reduceat(minima, merge_starts)
        maxima = np.maximum.reduceat(maxima, merge_starts)
        group_bucket = group_bucket[merge_starts]
        group_segment = group_segment[merge_starts]
    means = sums / counts
    centers = lo + (group_bucket.astype(np.float64) + 0.5) * (hi - lo) / columns

    line_break = np.r_[False, (np.diff(group_segment) != 0) | (np.diff(group_bucket) > 1)]
    break_count = int(np.count_nonzero(line_break))
    line_x = np.empty(len(centers) + break_count, dtype=np.float64)
    line_y = np.empty(len(means) + break_count, dtype=np.float64)
    targets = np.arange(len(centers)) + np.cumsum(line_break)
    line_x[targets] = centers
    line_y[targets] = means
    if break_count:
        gap_targets = targets[line_break] - 1
        line_x[gap_targets] = centers[line_break]
        line_y[gap_targets] = np.nan

    envelope_x = np.repeat(centers, 2)
    envelope_y = np.column_stack((minima, maxima)).reshape(-1)
    return DisplayData("aggregate", line_x, line_y, envelope_x, envelope_y)


class CurrentViewBox(pg.ViewBox):
    """View box with time-first wheel zoom and rectangle selection."""

    restoreZoomRequested = QtCore.Signal(object)

    def __init__(self, *, enableMenu: bool = True) -> None:  # noqa: N803
        super().__init__(enableMenu=enableMenu)
        self._zoom_history: list[_ZoomState] = []
        self._rect_drag_state: _ZoomState | None = None

    def wheelEvent(self, ev, axis=None) -> None:  # noqa: N802, ARG002
        self._push_zoom_state(self._zoom_state())
        modifiers = ev.modifiers()
        zoom_axis = (
            self.YAxis if modifiers & QtCore.Qt.KeyboardModifier.ShiftModifier else self.XAxis
        )
        super().wheelEvent(ev, axis=zoom_axis)

    def mouseDragEvent(self, ev, axis=None) -> None:  # noqa: N802
        is_rect_drag = (
            axis is None
            and self.state["mouseMode"] == self.RectMode
            and ev.button()
            in (QtCore.Qt.MouseButton.LeftButton, QtCore.Qt.MouseButton.MiddleButton)
        )
        if not is_rect_drag:
            super().mouseDragEvent(ev, axis=axis)
            return
        if self._rect_drag_state is None:
            self._rect_drag_state = self._zoom_state()
        ev.accept()
        if not ev.isFinish():
            self.updateScaleBox(ev.buttonDownPos(), ev.pos())
            return
        self.rbScaleBox.hide()
        state = self._rect_drag_state
        self._rect_drag_state = None
        start = self.childGroup.mapFromParent(ev.buttonDownPos(ev.button()))
        end = self.childGroup.mapFromParent(ev.pos())
        x_range = sorted((float(start.x()), float(end.x())))
        if x_range[1] > x_range[0]:
            self.setXRange(x_range[0], x_range[1], padding=0)
            self.sigRangeChangedManually.emit([True, False])
        if state is not None and self._zoom_state() != state:
            self._push_zoom_state(state)

    def mouseClickEvent(self, ev) -> None:  # noqa: N802
        if ev.button() == QtCore.Qt.MouseButton.RightButton:
            ev.accept()
            if self._zoom_history:
                self.restoreZoomRequested.emit(self._zoom_history.pop())
            return
        super().mouseClickEvent(ev)

    def clear_zoom_history(self) -> None:
        self._zoom_history.clear()

    def map_zoom_history(self, mapper: Callable[[_ZoomState], _ZoomState]) -> None:
        self._zoom_history = [mapper(state) for state in self._zoom_history]

    def _zoom_state(self) -> _ZoomState:
        x_range, y_range = self.viewRange()
        return _ZoomState(
            (float(x_range[0]), float(x_range[1])),
            (float(y_range[0]), float(y_range[1])),
        )

    def _push_zoom_state(self, state: _ZoomState) -> None:
        if self._zoom_history and self._zoom_history[-1] == state:
            return
        self._zoom_history.append(state)
        if len(self._zoom_history) > _ZOOM_HISTORY_LIMIT:
            del self._zoom_history[0]


class PowerPlotWidget(QtWidgets.QWidget):
    """Single-current waveform plot with a shared visible selection window."""

    selectionChanged = QtCore.Signal(float, float)
    followChanged = QtCore.Signal(bool)

    def __init__(self, parent: QtWidgets.QWidget | None = None, theme: ChartTheme = LIGHT) -> None:
        super().__init__(parent)
        self.theme = theme
        self._records: np.ndarray | None = None
        self._bounds = (0.0, 1.0)
        self._follow = True
        self._follow_window_width_s = 10.0
        self._live = False
        self._y_range_initialized = False
        self._selection = (0.0, SAMPLE_PERIOD_S)
        self._syncing_region = False
        self._updating_view = False
        self._last_render_signature: tuple[object, ...] | None = None
        self._last_display_mode = "raw"
        self._last_raw_visible_count = 0
        self._last_envelope_pairs = 0
        self._cursor_index = 0
        self._render_timer = QtCore.QTimer(self)
        self._render_timer.setSingleShot(True)
        self._render_timer.setInterval(_RENDER_INTERVAL_MS)
        self._render_timer.timeout.connect(self._update_visible_data)
        self.setFocusPolicy(QtCore.Qt.FocusPolicy.StrongFocus)
        self.setAccessibleName("Current waveform; arrow keys move the sample cursor")

        self.cursor_label = QtWidgets.QLabel("Cursor: n/a")
        self.cursor_label.setTextInteractionFlags(
            QtCore.Qt.TextInteractionFlag.TextSelectableByMouse
        )

        self.plots: list[pg.PlotWidget] = []
        self.curves: dict[str, pg.PlotDataItem] = {}
        self.cursor_lines: list[pg.InfiniteLine] = []
        self.regions: list[pg.LinearRegionItem] = []

        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self._channel_label("Current", "A", theme.current))
        current = self._make_plot("Current", "A")
        layout.addWidget(current, 1)
        layout.addWidget(self.cursor_label)

        envelope = current.plot(name="Current envelope", connect="pairs")
        envelope.setDownsampling(ds=1, auto=False)
        envelope.setClipToView(False)
        envelope.setZValue(-1)
        curve = current.plot(name="Current", pen=pg.mkPen(theme.current, width=2))
        curve.setDownsampling(ds=1, auto=False)
        curve.setClipToView(False)
        self.curves = {"current": curve, "current_envelope": envelope}

        region = pg.LinearRegionItem(values=self._selection, movable=False)
        region.setZValue(10)
        region.setBrush(pg.mkBrush(90, 150, 240, 35))
        region.setHoverBrush(pg.mkBrush(90, 150, 240, 35))
        region.setAcceptedMouseButtons(QtCore.Qt.MouseButton.NoButton)
        region.setAcceptHoverEvents(False)
        for edge in region.lines:
            edge.setPen(pg.mkPen(theme.selection, width=1))
            edge.setAcceptedMouseButtons(QtCore.Qt.MouseButton.NoButton)
            edge.setAcceptHoverEvents(False)
        region.sigRegionChanged.connect(self._region_changed)
        region.sigRegionChangeFinished.connect(self._region_finished)
        current.addItem(region)
        self.regions.append(region)

        line = pg.InfiniteLine(angle=90, movable=False, pen=pg.mkPen(theme.secondary_text, width=1))
        line.setVisible(False)
        line.setZValue(20)
        current.addItem(line)
        self.cursor_lines.append(line)

        cast(pg.GraphicsScene, current.scene()).sigMouseMoved.connect(
            lambda position: self._mouse_moved(position, current)
        )
        view_box = cast(CurrentViewBox, current.getViewBox())
        view_box.sigXRangeChanged.connect(self._x_range_changed)
        view_box.sigRangeChangedManually.connect(self._range_changed_manually)
        view_box.restoreZoomRequested.connect(self._restore_zoom)
        view_box.sigResized.connect(self._schedule_visible_update)

        self.region = region
        self.set_theme(theme)

    def set_theme(self, theme: ChartTheme) -> None:
        self.theme = theme
        self.curves["current"].setPen(pg.mkPen(theme.current, width=2))
        envelope_color = pg.mkColor(theme.current)
        envelope_color.setAlpha(80)
        self.curves["current_envelope"].setPen(pg.mkPen(envelope_color, width=1))
        plot = self.plots[0]
        plot.setBackground(theme.surface)
        plot.getAxis("bottom").setPen(pg.mkPen(theme.grid))
        plot.getAxis("left").setPen(pg.mkPen(theme.grid))
        plot.getAxis("bottom").setTextPen(pg.mkPen(theme.secondary_text))
        plot.getAxis("left").setTextPen(pg.mkPen(theme.secondary_text))
        plot.showGrid(x=True, y=True, alpha=0.35)
        for edge in self.region.lines:
            edge.setPen(pg.mkPen(theme.selection, width=1))
        self.cursor_lines[0].setPen(pg.mkPen(theme.secondary_text, width=1))

    def set_follow_enabled(self, enabled: bool) -> None:
        if enabled and not self._follow:
            raw_x = self.plots[0].getViewBox().viewRange()[0]
            width = float(raw_x[1] - raw_x[0])
            if math.isfinite(width) and width > 0:
                self._follow_window_width_s = max(SAMPLE_PERIOD_S, width)
        self._follow = enabled
        if enabled:
            self._follow_latest()

    def set_records(
        self,
        records: np.ndarray | None,
        *,
        full_bounds: tuple[float, float] | None = None,
        live: bool = False,
        preserve_view: bool = False,
        preserve_history: bool = False,
    ) -> None:
        view_box = cast(CurrentViewBox, self.plots[0].getViewBox())
        previous_live = self._live
        preserved_x, preserved_y = [tuple(values) for values in view_box.viewRange()]
        preserved_selection = self._selection

        if not preserve_history:
            view_box.clear_zoom_history()
        if not preserve_view:
            self._y_range_initialized = False
            self._last_render_signature = None

        self._records = records
        self._live = live
        if records is None or not len(records):
            self._bounds = full_bounds or (0.0, 1.0)
            self.region.setBounds(self._bounds)
            self._clear_curves()
            self.cursor_label.setText("Cursor: n/a")
            return

        if full_bounds is None:
            self._bounds = (
                float(records["time_s"][0]),
                float(records["time_s"][-1] + SAMPLE_PERIOD_S),
            )
        else:
            self._bounds = full_bounds
        start, end = self._bounds
        if end <= start:
            end = start + SAMPLE_PERIOD_S
            self._bounds = (start, end)
        self.region.setBounds(self._bounds)

        if preserve_history and previous_live and not live:
            if self._follow:
                view_box.map_zoom_history(self._right_anchor_zoom_state)
            else:
                view_box.map_zoom_history(self._clamp_zoom_state)

        if preserve_view:
            if self._follow and live:
                width = self._valid_x_width(preserved_x)
                self._follow_window_width_s = width
                x_range = self._right_anchored_range(width)
                self._set_view_range(x_range, preserved_y)
                self.set_selection(*x_range, emit=True)
                self._update_visible_data(view_range=x_range)
            elif self._follow and previous_live and not live:
                width = self._valid_x_width(preserved_x)
                self._follow_window_width_s = width
                x_range = self._right_anchored_range(width)
                self._set_view_range(x_range, preserved_y)
                self.set_selection(*x_range, emit=True)
                self._update_visible_data(view_range=x_range)
            else:
                x_range = self._clamp_x_range(preserved_x)
                selection = self._clamp_x_range(preserved_selection)
                self._set_view_range(x_range, preserved_y)
                self.set_selection(*selection, emit=True)
                self._update_visible_data(view_range=x_range)
            return

        if live and self._follow:
            self._follow_latest()
        else:
            x_range = (start, end)
            self._set_view_range(x_range, preserved_y)
            self.set_selection(*x_range, emit=True)
            self._update_visible_data(view_range=x_range)

    def set_selection(self, start: float, end: float, *, emit: bool = False) -> None:
        start, end = self._normalized_range(start, end)
        if (start, end) == self._selection:
            return
        self._selection = (start, end)
        self._syncing_region = True
        try:
            self.region.setRegion((start, end))
        finally:
            self._syncing_region = False
        if emit:
            self.selectionChanged.emit(start, end)

    def selected_range(self) -> tuple[float, float]:
        return self._selection

    def raw_records(self) -> np.ndarray | None:
        return self._records

    def display_summary(self) -> dict[str, object]:
        """Return display-only metadata for benchmarks and diagnostics."""
        return {
            "mode": self._last_display_mode,
            "raw_visible_count": self._last_raw_visible_count,
            "envelope_pairs": self._last_envelope_pairs,
        }

    def _channel_label(self, title: str, unit: str, color: str) -> QtWidgets.QLabel:
        label = QtWidgets.QLabel(f"{title} ({unit})")
        label.setObjectName("channelTitle")
        # Titles remain text-colored; the adjacent line identifies the channel.
        label.setProperty("channel_color", color)
        return label

    def _make_plot(self, label: str, unit: str) -> pg.PlotWidget:
        view_box = CurrentViewBox(enableMenu=False)
        view_box.setMouseMode(view_box.RectMode)
        plot = pg.PlotWidget(viewBox=view_box)
        plot.setMenuEnabled(False)
        plot.setMouseEnabled(x=True, y=True)
        plot.setLabel("bottom", "Reconstructed time", units="s")
        plot.setLabel("left", label, units=unit)
        plot.getAxis("left").setWidth(78)
        plot.getAxis("bottom").enableAutoSIPrefix(False)
        plot.getAxis("left").enableAutoSIPrefix(False)
        self.plots.append(plot)
        return plot

    def _clear_curves(self) -> None:
        self.curves["current"].setData([], [])
        self.curves["current_envelope"].setData([], [])
        self._last_render_signature = None

    def _follow_latest(self) -> None:
        visible_range = self._right_anchored_range(self._follow_window_width_s)
        self._set_view_range(visible_range)
        self.set_selection(*visible_range, emit=True)
        self._update_visible_data(view_range=visible_range)

    def _set_view_range(
        self,
        x_range: tuple[float, float],
        y_range: tuple[float, float] | None = None,
    ) -> None:
        view_box = self.plots[0].getViewBox()
        self._updating_view = True
        try:
            if y_range is None:
                view_box.setXRange(*x_range, padding=0.0)
            else:
                view_box.setRange(xRange=x_range, yRange=y_range, padding=0)
        finally:
            self._updating_view = False

    def _valid_x_width(self, x_range: tuple[float, float]) -> float:
        width = float(x_range[1] - x_range[0])
        if not math.isfinite(width) or width <= 0:
            width = self._follow_window_width_s
        return max(SAMPLE_PERIOD_S, width)

    def _right_anchored_range(self, width: float) -> tuple[float, float]:
        start, end = self._bounds
        available = max(SAMPLE_PERIOD_S, end - start)
        width = min(available, max(SAMPLE_PERIOD_S, width))
        return max(start, end - width), end

    def _clamp_x_range(self, x_range: tuple[float, float]) -> tuple[float, float]:
        start, end = self._bounds
        available = max(SAMPLE_PERIOD_S, end - start)
        lo, hi = float(x_range[0]), float(x_range[1])
        if not all(math.isfinite(value) for value in (lo, hi)) or hi <= lo:
            return start, end
        width = hi - lo
        if width >= available:
            return start, end
        lo = min(max(start, lo), end - width)
        return lo, lo + width

    def _right_anchor_zoom_state(self, state: _ZoomState) -> _ZoomState:
        x_range = self._right_anchored_range(self._valid_x_width(state.x_range))
        return _ZoomState(x_range, state.y_range)

    def _clamp_zoom_state(self, state: _ZoomState) -> _ZoomState:
        return _ZoomState(self._clamp_x_range(state.x_range), state.y_range)

    def _x_range_changed(self, _view_box: pg.ViewBox, _range_values: tuple[float, float]) -> None:
        if self._updating_view or self._records is None or not len(self._records):
            return
        self._schedule_visible_update()

    def _range_changed_manually(self, mask: object) -> None:
        try:
            x_changed = bool(mask[0])  # type: ignore[index]
        except (IndexError, TypeError):
            x_changed = False
        if x_changed:
            raw_x = self.plots[0].getViewBox().viewRange()[0]
            width = float(raw_x[1] - raw_x[0])
            if math.isfinite(width) and width > 0:
                self._follow_window_width_s = max(SAMPLE_PERIOD_S, width)
            if self._follow and self._live:
                self._follow_latest()
            else:
                self.set_selection(*self._current_view_range(), emit=True)

    def _restore_zoom(self, state: _ZoomState) -> None:
        if self._follow and self._live:
            self._follow_window_width_s = self._valid_x_width(state.x_range)
            x_range = self._right_anchored_range(self._follow_window_width_s)
        else:
            x_range = self._clamp_x_range(state.x_range)
        self._set_view_range(x_range, state.y_range)
        self.set_selection(*self._current_view_range(), emit=True)
        self._update_visible_data(view_range=self._current_view_range())

    def _schedule_visible_update(self, *_args: object) -> None:
        if not self._render_timer.isActive():
            self._render_timer.start()

    def _current_view_range(self) -> tuple[float, float]:
        view = self.plots[0].getViewBox().viewRange()[0]
        lo, hi = float(view[0]), float(view[1])
        if not math.isfinite(lo) or not math.isfinite(hi) or hi <= lo:
            return self._bounds
        return max(self._bounds[0], lo), min(self._bounds[1], hi)

    def _update_visible_data(
        self,
        view_range: tuple[float, float] | None = None,
    ) -> None:
        records = self._records
        if records is None or not len(records):
            self._clear_curves()
            return
        if view_range is None:
            view_range = self._current_view_range()
        lo, hi = view_range
        if hi <= lo:
            lo, hi = self._bounds
        start_index = int(np.searchsorted(records["time_s"], lo, side="left"))
        end_index = int(np.searchsorted(records["time_s"], hi, side="left"))
        start_index = max(0, start_index)
        end_index = min(len(records), end_index)
        if end_index <= start_index:
            signature: tuple[object, ...] = ("empty", lo, hi, self.width())
            if signature != self._last_render_signature:
                self._clear_curves()
                self._last_render_signature = signature
            return
        visible = records[start_index:end_index]
        view_box = self.plots[0].getViewBox()
        columns = max(1, int(view_box.width()))
        signature = (
            start_index,
            end_index,
            int(visible["index"][0]),
            int(visible["index"][-1]),
            float(lo),
            float(hi),
            columns,
        )
        if signature == self._last_render_signature:
            return
        display = build_display_data(visible, lo, hi, columns)
        self._last_display_mode = display.mode
        self._last_raw_visible_count = len(visible)
        self._last_envelope_pairs = len(display.envelope_x) // 2
        self.curves["current"].setData(
            display.line_x,
            display.line_y,
            connect="finite",
            antialias=False,
        )
        self.curves["current_envelope"].setData(
            display.envelope_x,
            display.envelope_y,
            connect="pairs",
            antialias=False,
        )
        if not self._y_range_initialized:
            y_source = display.envelope_y if len(display.envelope_y) else display.line_y
            finite = y_source[np.isfinite(y_source)]
            if len(finite):
                y_lo, y_hi = float(np.min(finite)), float(np.max(finite))
                padding = max((y_hi - y_lo) * 0.05, 1e-6)
                self._updating_view = True
                try:
                    view_box.setYRange(y_lo - padding, y_hi + padding, padding=0)
                finally:
                    self._updating_view = False
                self._y_range_initialized = True
        self._last_render_signature = signature

    def _region_changed(self, changed: pg.LinearRegionItem) -> None:
        if self._syncing_region:
            return
        region = cast(tuple[float, float], changed.getRegion())
        self._selection = self._normalized_range(float(region[0]), float(region[1]))

    def _region_finished(self, changed: pg.LinearRegionItem) -> None:
        if self._syncing_region:
            return
        self._region_changed(changed)
        self.selectionChanged.emit(*self._selection)

    def _normalized_range(self, start: float, end: float) -> tuple[float, float]:
        if not math.isfinite(start) or not math.isfinite(end):
            return self._selection
        lower, upper = self._bounds
        start = min(max(lower, start), upper)
        end = min(max(lower, end), upper)
        if end <= start:
            end = min(upper, start + SAMPLE_PERIOD_S)
            if end <= start:
                start = max(lower, end - SAMPLE_PERIOD_S)
        return float(start), float(end)

    def _mouse_moved(self, position: QtCore.QPointF, plot: pg.PlotWidget) -> None:
        if plot.sceneBoundingRect().contains(position):
            point = plot.getViewBox().mapSceneToView(position)
            self._show_cursor(float(point.x()))

    def keyPressEvent(self, event) -> None:  # noqa: N802
        if event.key() in (QtCore.Qt.Key.Key_Left, QtCore.Qt.Key.Key_Right):
            if self._records is not None and len(self._records):
                step = 1 if event.key() == QtCore.Qt.Key.Key_Right else -1
                index = min(len(self._records) - 1, max(0, self._cursor_index + step))
                self._show_cursor(float(self._records["time_s"][index]))
            event.accept()
            return
        super().keyPressEvent(event)

    def _show_cursor(self, time_s: float) -> None:
        records = self._records
        if records is None or not len(records):
            self.cursor_label.setText("Cursor: n/a")
            return
        times = records["time_s"]
        index = int(np.searchsorted(times, time_s))
        if index >= len(records):
            index = len(records) - 1
        if index > 0 and abs(float(times[index - 1]) - time_s) <= abs(float(times[index]) - time_s):
            index -= 1
        self._cursor_index = index
        record = records[index]
        cursor_time = float(record["time_s"])
        self.cursor_lines[0].setPos(cursor_time)
        self.cursor_lines[0].setVisible(True)
        current = float(record["current_a"])
        self.cursor_label.setText(
            "Cursor: "
            f"{format_time_s(cursor_time)}; "
            f"Current {format_current_a(current)}; "
            f"flags {int(record['flags'])}; dropped {int(record['dropped'])}"
        )


def make_demo_records(count: int = 10_000) -> np.ndarray:
    """Create deterministic offline data for screenshots and UI tests."""

    records = np.empty(count, dtype=SAMPLE_DTYPE)
    indices = np.arange(count, dtype=np.uint64)
    seconds = indices.astype(np.float64) * SAMPLE_PERIOD_S
    current = 0.035 + 0.012 * np.sin(2 * np.pi * 5 * seconds)
    current += np.where((indices // 700) % 5 == 0, 0.018, 0.0)
    voltage = 3.3 + 0.03 * np.sin(2 * np.pi * 0.7 * seconds)
    records["index"] = indices
    records["time_s"] = seconds
    records["host_elapsed_s"] = seconds
    records["current_a"] = current
    records["voltage_v"] = voltage
    records["flags"] = 0
    records["dropped"] = 0
    if count > 2000:
        records["flags"][2000] = int(Quality.GAP_BEFORE)
    return records
