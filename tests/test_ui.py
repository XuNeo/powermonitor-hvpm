# ruff: noqa: E402,I001
from __future__ import annotations

import os
import threading
import time
from dataclasses import replace
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("QT_API", "pyside6")

import numpy as np
import pytest
import shiboken6
from PySide6 import QtCore, QtWidgets

from powermonitor.models import (
    SAMPLE_DTYPE,
    SAMPLE_PERIOD_S,
    ControllerEvent,
    ControllerState,
    DeviceInfo,
    Quality,
    SampleBatch,
)
from powermonitor.statistics import WindowStatistics
from powermonitor.storage import CaptureWriter
from powermonitor.ui.table import SampleTableModel
from powermonitor.ui.theme import DARK
from powermonitor.ui.window import MainWindow


class FakeController:
    def __init__(self, state: ControllerState | None = None) -> None:
        self.state = state or ControllerState()
        self.requests: list[tuple[str, dict]] = []
        self.events: list[ControllerEvent] = []
        self.shutdown_called = False
        self.shutdown_result = True
        self.recent = np.empty(0, dtype=SAMPLE_DTYPE)
        self.recent_calls = 0

    def request(self, action: str, **kwargs) -> None:
        self.requests.append((action, kwargs))

    def poll_events(self) -> list[ControllerEvent]:
        events = list(self.events)
        self.events.clear()
        return events

    def recent_records(self) -> np.ndarray:
        self.recent_calls += 1
        return self.recent.copy()

    def shutdown(self, timeout: float = 3.0) -> bool:
        self.shutdown_called = True
        return self.shutdown_result

    def set_state(self, **changes) -> None:
        self.state = replace(self.state, **changes)
        self.events.append(ControllerEvent("state", self.state))


def sample_records(count: int = 1000) -> np.ndarray:
    records = np.empty(count, dtype=SAMPLE_DTYPE)
    indices = np.arange(count, dtype=np.uint64)
    times = indices.astype(np.float64) / 5000.0
    records["index"] = indices
    records["time_s"] = times
    records["host_elapsed_s"] = times
    records["current_a"] = 0.010 + times * 0.001
    records["voltage_v"] = 3.3
    records["flags"] = 0
    records["dropped"] = 0
    return records


def append_sample_records(records: np.ndarray, count: int) -> np.ndarray:
    extended = np.empty(len(records) + count, dtype=SAMPLE_DTYPE)
    extended[: len(records)] = records
    indices = np.arange(len(records), len(extended), dtype=np.uint64)
    times = indices.astype(np.float64) * SAMPLE_PERIOD_S
    extended["index"][len(records) :] = indices
    extended["time_s"][len(records) :] = times
    extended["host_elapsed_s"][len(records) :] = times
    extended["current_a"][len(records) :] = 0.010 + times * 0.001
    extended["voltage_v"][len(records) :] = 3.3
    extended["flags"][len(records) :] = 0
    extended["dropped"][len(records) :] = 0
    return extended


def make_capture(path: Path, records: np.ndarray) -> None:
    with CaptureWriter(path) as writer:
        writer.append(SampleBatch(records))


def start_growing_capture(path: Path, records: np.ndarray) -> CaptureWriter:
    writer = CaptureWriter(path)
    writer.append(SampleBatch(records))
    return writer


def stats_for_window(start: float, end: float) -> WindowStatistics:
    return WindowStatistics(
        start,
        end,
        1,
        1,
        end - start,
        1,
        0.1,
        0.1,
        0.1,
        3.3,
        0.33,
        0.1,
        0.33,
        0,
        False,
        0,
    )


def pump_until(qtbot, predicate, timeout=3000):
    deadline = time.monotonic() + timeout / 1000
    while time.monotonic() < deadline:
        QtWidgets.QApplication.processEvents()
        if predicate():
            return
        qtbot.wait(20)
    raise AssertionError("condition did not become true")


def close_test_window(qtbot, widget: MainWindow, controller: FakeController) -> None:
    pump_until(qtbot, widget._wait_for_background_tasks)
    controller.state = replace(
        controller.state, connection="disconnected", output="off", capture="idle"
    )
    widget._state = controller.state
    assert widget.close()


@pytest.fixture
def window(qtbot, monkeypatch):
    monkeypatch.setattr(QtWidgets.QMessageBox, "warning", lambda *args, **kwargs: None)
    controller = FakeController()
    widget = MainWindow(controller=controller)
    qtbot.addWidget(widget)
    widget.show()
    return widget, controller


def test_device_panel_scan_connect_and_safe_power_flow(window, qtbot, monkeypatch):
    widget, controller = window
    qtbot.mouseClick(widget.scan_button, QtCore.Qt.MouseButton.LeftButton)
    assert controller.requests[-1] == ("scan", {"backend": "simulator"})

    device = DeviceInfo(serial="SIM-HVPM-0001", firmware="simulator", backend="simulator")
    controller.events.append(ControllerEvent("devices", [device]))
    widget._poll_controller()
    assert widget.device_combo.count() == 1

    qtbot.mouseClick(widget.connect_button, QtCore.Qt.MouseButton.LeftButton)
    assert controller.requests[-1] == (
        "connect",
        {"backend": "simulator", "serial": "SIM-HVPM-0001"},
    )

    controller.set_state(connection="connected", output="unknown", device=device, backend="monsoon")
    widget._poll_controller()
    assert widget.off_button.isEnabled()
    assert not widget.apply_voltage_button.isEnabled()
    assert not widget.enable_button.isEnabled()

    controller.set_state(output="off")
    widget._poll_controller()
    widget.voltage_spin.setValue(4.20)
    assert not widget.enable_button.isEnabled()
    qtbot.mouseClick(widget.apply_voltage_button, QtCore.Qt.MouseButton.LeftButton)
    assert controller.requests[-1] == ("set_voltage", {"voltage_v": 4.2})
    controller.set_state(target_voltage_v=4.2)
    widget._poll_controller()
    assert widget.enable_button.isEnabled()

    monkeypatch.setattr(
        QtWidgets.QMessageBox,
        "question",
        lambda *args, **kwargs: QtWidgets.QMessageBox.StandardButton.Yes,
    )
    qtbot.mouseClick(widget.enable_button, QtCore.Qt.MouseButton.LeftButton)
    assert controller.requests[-1] == ("enable_output", {"confirmed": True})

    qtbot.mouseClick(widget.off_button, QtCore.Qt.MouseButton.LeftButton)
    assert controller.requests[-1] == ("disable_output", {})


def test_capture_run_stop_independent_from_off(window, qtbot):
    widget, controller = window
    controller.set_state(connection="connected", output="on", capture="idle")
    widget._poll_controller()
    assert widget.run_button.isEnabled()
    assert not widget.apply_voltage_button.isEnabled()
    qtbot.mouseClick(widget.run_button, QtCore.Qt.MouseButton.LeftButton)
    action, kwargs = controller.requests[-1]
    assert action == "start"
    assert Path(kwargs["path"]).suffix == ".pmcap"
    assert widget._live_path == kwargs["path"]

    controller.set_state(capture="running")
    widget._poll_controller()
    assert widget.stop_button.isEnabled()
    assert widget.off_button.isEnabled()
    assert not widget.run_button.isEnabled()
    assert not widget.voltage_spin.isEnabled()
    qtbot.mouseClick(widget.stop_button, QtCore.Qt.MouseButton.LeftButton)
    assert controller.requests[-1] == ("stop", {})
    controller.set_state(connection="disconnected", output="unknown", capture="idle")
    widget._poll_controller()


def test_numeric_selection_updates_stats_without_plot_data_downsampling(window, qtbot):
    widget, _controller = window
    widget.load_demo_records(count=5000)
    widget.selection_start_spin.setValue(0.1)
    widget.selection_end_spin.setValue(0.3)
    qtbot.mouseClick(widget.apply_selection_button, QtCore.Qt.MouseButton.LeftButton)
    assert widget.plot_widget.selected_range() == pytest.approx((0.1, 0.3))
    pump_until(
        qtbot,
        lambda: (
            widget.selection_summary.mean_current_label.text().endswith("mA")
            and widget.stats_widget.table.rowCount() > 0
        ),
    )
    assert widget.raw_widget.model.total_rows == 5000
    assert widget.selection_summary.mean_current_label.text().endswith("mA")
    assert widget.selection_summary.duration_label.text() != "n/a"
    assert widget.selection_summary.samples_label.text() != "n/a"


def test_run_clears_stale_replay_and_refreshes_live_window_stats(window, qtbot):
    widget, controller = window
    widget.load_demo_records(count=200)
    old_generation = widget._stats_generation
    controller.set_state(connection="connected", output="off", capture="idle")
    widget._poll_controller()
    qtbot.mouseClick(widget.run_button, QtCore.Qt.MouseButton.LeftButton)
    assert widget._reader is None
    assert widget._records is None
    assert widget.raw_widget.model.total_rows == 0
    assert widget._stats_generation > old_generation

    controller.recent = sample_records(1000)
    controller.set_state(capture="running", sample_count=1000)
    widget._last_live_statistics = 0.0
    widget._poll_controller()
    pump_until(qtbot, lambda: widget.selection_summary.mean_current_label.text() != "n/a")
    assert widget.raw_widget.model.total_rows == 0
    widget.tabs.setCurrentWidget(widget.raw_widget)
    pump_until(qtbot, lambda: widget.raw_widget.model.total_rows == 1000)


def test_connection_error_keeps_disconnect_and_off_recovery_reachable(window):
    widget, controller = window
    controller.set_state(connection="error", output="unknown", error="USB disconnected")
    widget._poll_controller()
    assert widget.disconnect_button.isEnabled()
    assert widget.off_button.isEnabled()
    assert not widget.connect_button.isEnabled()


def test_theme_switch_changes_active_theme(window, qtbot):
    widget, _controller = window
    widget.theme_combo.setCurrentIndex(widget.theme_combo.findData("dark"))
    qtbot.wait(20)
    assert widget._theme == DARK
    assert widget.plot_widget.theme == DARK


def test_stop_transition_preserves_full_data_view_selection_and_history(tmp_path, qtbot):
    records = sample_records(60_000)
    path = tmp_path / "stop.pmcap"
    writer = start_growing_capture(path, records)
    controller = FakeController(
        ControllerState(
            connection="connected",
            output="on",
            capture="running",
            recording_path=path,
            sample_count=len(records),
        )
    )
    widget = MainWindow(controller=controller)
    qtbot.addWidget(widget)
    widget._poll_live_records(force=True, sample_count=len(records))
    widget.follow_check.setChecked(False)
    view_box = widget.plot_widget.plots[0].getViewBox()
    view_box.setRange(xRange=(2.0, 4.0), yRange=(0.011, 0.017), padding=0)
    widget.plot_widget.set_selection(2.5, 3.5, emit=False)
    history_state = view_box._zoom_state()
    view_box._push_zoom_state(history_state)
    view_box.setRange(xRange=(3.0, 3.5), yRange=(0.012, 0.016), padding=0)
    before = view_box.viewRange()
    before_selection = widget.plot_widget.selected_range()

    writer.close()
    controller.state = replace(controller.state, capture="idle")
    widget._capture_finished(path)

    after = view_box.viewRange()
    assert widget._live_reader is None
    assert widget._live_path is None
    assert widget._reader is not None and len(widget._reader.records) == len(records)
    assert widget._records is widget._reader.records
    assert after[0] == pytest.approx(before[0])
    assert after[1] == pytest.approx(before[1])
    assert widget.plot_widget.selected_range() == pytest.approx(before_selection)
    assert view_box._zoom_history
    restored = view_box._zoom_history.pop()
    assert restored.x_range == pytest.approx(history_state.x_range)
    assert restored.y_range == pytest.approx(history_state.y_range)
    close_test_window(qtbot, widget, controller)


def test_manual_open_and_new_run_clear_zoom_history(tmp_path, window, qtbot):
    widget, controller = window
    path = tmp_path / "manual.pmcap"
    make_capture(path, sample_records(1000))
    view_box = widget.plot_widget.plots[0].getViewBox()
    view_box._push_zoom_state(view_box._zoom_state())
    widget.open_capture(path)
    assert not view_box._zoom_history

    view_box._push_zoom_state(view_box._zoom_state())
    controller.set_state(connection="connected", output="on", capture="idle")
    widget._poll_controller()
    qtbot.mouseClick(widget.run_button, QtCore.Qt.MouseButton.LeftButton)
    assert not view_box._zoom_history


def test_old_statistics_result_does_not_override_new_selection(window):
    widget, _controller = window
    old_stats = stats_for_window(0, 1)
    new_stats = stats_for_window(2, 3)
    widget._stats_generation = 2
    widget._statistics_finished(1, old_stats, "")
    assert "0.000000" not in widget.stats_widget.status_label.text()
    widget._statistics_finished(2, new_stats, "")
    assert "2.000000" in widget.stats_widget.status_label.text()


def test_replay_reader_warnings_remain_visible_for_clean_selection(tmp_path, qtbot):
    records = sample_records(2000)
    records["flags"][1500] = int(Quality.LOSS_UNKNOWN)
    path = tmp_path / "loss.pmcap"
    make_capture(path, records)
    controller = FakeController()
    widget = MainWindow(controller=controller)
    qtbot.addWidget(widget)
    widget.open_capture(path)
    widget.plot_widget.set_selection(0.0, 0.1, emit=True)
    pump_until(qtbot, lambda: widget.stats_widget.table.rowCount() > 0)
    table_text = _table_text(widget.stats_widget.table)
    assert "loss" in widget.stats_widget.warning_label.text().lower()
    assert "Loss unknown in selection" in table_text
    assert "no" in table_text


def test_replay_selection_export_streams_original_samples(tmp_path, qtbot):
    records = sample_records(1000)
    path = tmp_path / "source.pmcap"
    make_capture(path, records)
    controller = FakeController()
    widget = MainWindow(controller=controller)
    qtbot.addWidget(widget)
    widget.open_capture(path)
    widget.plot_widget.set_selection(0.0001, 0.0004, emit=True)
    destination = tmp_path / "selection.csv"
    widget.export_selection_csv(destination)
    pump_until(
        qtbot,
        lambda: destination.exists() and "CSV exported" in widget.status_label.text(),
    )
    lines = destination.read_text().splitlines()
    assert lines[0].startswith("index,time_s")
    assert [line.split(",")[0] for line in lines[1:]] == ["0", "1"]


def test_raw_table_model_is_paged_not_full_qt_rows(qtbot):
    model = SampleTableModel()
    records = sample_records(2500)
    model.set_records(records)
    assert model.total_rows == 2500
    assert model.rowCount() == 1000
    model.set_page_start(1000)
    assert model.headerData(0, QtCore.Qt.Orientation.Vertical) == "1000"
    index = model.index(0, 0)
    assert model.data(index) == "1000"


def test_raw_table_model_preserves_page_when_records_update(qtbot):
    model = SampleTableModel()
    model.set_records(sample_records(2500))
    model.set_page_start(1000)
    model.set_records(sample_records(2600))
    assert model.page_start == 1000
    model.set_records(sample_records(500))
    assert model.page_start == 0


def test_live_follow_off_skips_recent_fallback_but_updates_status(window):
    widget, controller = window
    controller.recent = sample_records(1000)
    controller.set_state(capture="running", sample_count=1000)
    widget._poll_controller()
    assert controller.recent_calls == 1

    widget.follow_check.setChecked(False)
    controller.set_state(sample_count=1100)
    widget._last_plot_update = float("-inf")
    widget._poll_controller()
    widget._poll_controller()
    assert controller.recent_calls == 1
    assert "samples 1,100" in widget.status_label.text()
    assert widget.raw_widget.model.total_rows == 0


def test_growing_capture_updates_full_bounds_with_follow_off(tmp_path, qtbot):
    initial = sample_records(55_000)
    path = tmp_path / "growing.pmcap"
    writer = start_growing_capture(path, initial)
    controller = FakeController(
        ControllerState(
            connection="connected",
            output="on",
            capture="running",
            recording_path=path,
            sample_count=len(initial),
        )
    )
    widget = MainWindow(controller=controller)
    qtbot.addWidget(widget)
    widget.show()
    widget._poll_live_records(force=True, sample_count=len(initial))
    assert widget._live_reader is not None
    assert isinstance(widget._records, np.memmap)
    assert widget.plot_widget._bounds == pytest.approx((0.0, 11.0))

    widget.follow_check.setChecked(False)
    view_box = widget.plot_widget.plots[0].getViewBox()
    view_box.setRange(xRange=(1.0, 2.0), yRange=(0.01, 0.03), padding=0)
    widget.plot_widget.set_selection(1.2, 1.8, emit=False)
    before = view_box.viewRange()

    extended = append_sample_records(initial, 10_000)
    writer.append(SampleBatch(extended[len(initial) :]))
    controller.set_state(sample_count=len(extended))
    widget._poll_live_records(force=True, sample_count=len(extended))

    after = view_box.viewRange()
    assert widget.plot_widget._bounds == pytest.approx((0.0, 13.0))
    assert after[0] == pytest.approx(before[0])
    assert after[1] == pytest.approx(before[1])
    assert widget.plot_widget.selected_range() == pytest.approx((1.2, 1.8))
    assert controller.recent_calls == 0
    writer.close()
    close_test_window(qtbot, widget, controller)


def test_growing_capture_stats_use_stable_early_history_snapshot(tmp_path, qtbot):
    records = sample_records(60_000)
    path = tmp_path / "stats.pmcap"
    writer = start_growing_capture(path, records)
    controller = FakeController(
        ControllerState(
            connection="connected",
            output="on",
            capture="running",
            recording_path=path,
            sample_count=len(records),
        )
    )
    widget = MainWindow(controller=controller)
    qtbot.addWidget(widget)
    widget._poll_live_records(force=True, sample_count=len(records))
    widget.follow_check.setChecked(False)
    widget.plot_widget.set_selection(0.2, 0.4, emit=True)
    pump_until(
        qtbot,
        lambda: (
            "0.200000" in widget.stats_widget.status_label.text()
            and widget.stats_widget.table.rowCount() > 0
        ),
    )
    assert "1,000" in _table_text(widget.stats_widget.table)
    writer.close()
    close_test_window(qtbot, widget, controller)


def test_growing_capture_raw_table_uses_full_memmap(tmp_path, qtbot):
    records = sample_records(60_000)
    path = tmp_path / "raw.pmcap"
    writer = start_growing_capture(path, records)
    controller = FakeController(
        ControllerState(
            connection="connected",
            output="on",
            capture="running",
            recording_path=path,
            sample_count=len(records),
        )
    )
    widget = MainWindow(controller=controller)
    qtbot.addWidget(widget)
    widget._poll_live_records(force=True, sample_count=len(records))
    widget.tabs.setCurrentWidget(widget.raw_widget)
    assert widget.raw_widget.model.total_rows == 60_000
    assert isinstance(widget.raw_widget.model._records, np.memmap)
    writer.close()
    close_test_window(qtbot, widget, controller)


def test_live_follow_on_immediately_fetches_latest(window):
    widget, controller = window
    controller.set_state(capture="running", sample_count=1000)
    widget.follow_check.setChecked(False)
    controller.recent_calls = 0
    controller.recent = sample_records(1000)
    widget.follow_check.setChecked(True)
    assert controller.recent_calls == 1
    assert widget._records is not None and len(widget._records) == 1000


def test_live_follow_resume_keeps_current_width(window):
    widget, controller = window
    controller.recent = sample_records(1000)
    controller.set_state(capture="running", sample_count=1000)
    widget._poll_controller()
    view_box = widget.plot_widget.plots[0].getViewBox()
    view_box.setXRange(0.10, 0.15, padding=0)
    widget.follow_check.setChecked(False)

    controller.recent = append_sample_records(controller.recent, 500)
    controller.set_state(sample_count=len(controller.recent))
    widget.follow_check.setChecked(True)

    latest = float(controller.recent["time_s"][-1] + SAMPLE_PERIOD_S)
    x_range = tuple(view_box.viewRange()[0])
    assert widget.follow_check.isChecked()
    assert x_range[1] == pytest.approx(latest)
    assert x_range[1] - x_range[0] == pytest.approx(0.05)
    assert widget.plot_widget.selected_range() == pytest.approx(x_range)


def test_plot_follow_signal_synchronizes_checkbox_without_canceling_stats(window):
    widget, controller = window
    controller.set_state(capture="running", sample_count=1000)
    pending_statistics = (object(), 0.0, 1.0, widget._stats_generation)
    widget._pending_statistics = pending_statistics
    old_generation = widget._stats_generation
    assert widget.follow_check.isChecked()

    widget.plot_widget.followChanged.emit(False)

    assert not widget.follow_check.isChecked()
    assert widget._stats_generation == old_generation
    assert widget._pending_statistics is pending_statistics


def test_manual_plot_x_signal_keeps_follow_checked_and_live_polling(window):
    widget, controller = window
    controller.recent = sample_records(1200)
    controller.set_state(capture="running", sample_count=len(controller.recent))
    widget._poll_controller()
    view_box = widget.plot_widget.plots[0].getViewBox()
    latest = float(controller.recent["time_s"][-1] + SAMPLE_PERIOD_S)
    view_box.setXRange(latest - 0.05, latest, padding=0)
    old_generation = widget._stats_generation
    calls_before_manual_x = controller.recent_calls

    view_box.sigRangeChangedManually.emit([True, False])

    manual_range = tuple(view_box.viewRange()[0])
    assert widget.follow_check.isChecked()
    assert controller.recent_calls == calls_before_manual_x
    assert widget._stats_generation == old_generation
    assert widget.plot_widget.selected_range() == pytest.approx(manual_range)
    assert widget.selection_start_spin.value() == pytest.approx(manual_range[0])
    assert widget.selection_end_spin.value() == pytest.approx(manual_range[1])
    assert f"{manual_range[0]:.6f} s to {manual_range[1]:.6f} s" in widget.window_label.text()

    controller.recent = append_sample_records(controller.recent, 600)
    controller.set_state(sample_count=len(controller.recent))
    widget._last_plot_update = float("-inf")
    widget._poll_controller()

    followed_range = tuple(view_box.viewRange()[0])
    followed_latest = float(controller.recent["time_s"][-1] + SAMPLE_PERIOD_S)
    assert widget.follow_check.isChecked()
    assert controller.recent_calls == calls_before_manual_x + 1
    assert followed_range[1] == pytest.approx(followed_latest)
    assert followed_range[1] - followed_range[0] == pytest.approx(manual_range[1] - manual_range[0])
    assert widget.plot_widget.selected_range() == pytest.approx(followed_range)


def test_numeric_selection_pauses_live_follow(window):
    widget, controller = window
    controller.recent = sample_records(1000)
    controller.set_state(capture="running", sample_count=1000)
    widget._poll_controller()
    widget.selection_start_spin.setValue(0.02)
    widget.selection_end_spin.setValue(0.04)
    old_generation = widget._stats_generation
    widget._last_live_statistics = time.monotonic()
    widget._apply_numeric_selection()
    assert not widget.follow_check.isChecked()
    assert widget.plot_widget.selected_range() == pytest.approx((0.02, 0.04))
    assert widget._stats_generation > old_generation


def test_same_live_sample_count_does_not_refetch(window):
    widget, controller = window
    controller.recent = sample_records(1000)
    controller.set_state(capture="running", sample_count=1000)
    widget._poll_controller()
    assert controller.recent_calls == 1
    widget._poll_controller()
    assert controller.recent_calls == 1


def test_raw_tab_refreshes_on_demand_and_preserves_page(window, qtbot, monkeypatch):
    widget, controller = window
    now = [0.0]
    monkeypatch.setattr("powermonitor.ui.window.time.monotonic", lambda: now[0])
    widget._last_live_statistics = 10.0
    widget._last_plot_update = float("-inf")
    controller.recent = sample_records(2500)
    controller.set_state(capture="running", sample_count=2500)
    widget._poll_controller()
    assert widget.raw_widget.model.total_rows == 0

    widget.tabs.setCurrentWidget(widget.raw_widget)
    assert widget.raw_widget.model.total_rows == 2500
    widget.raw_widget.model.set_page_start(1000)
    controller.recent = sample_records(2600)
    controller.set_state(sample_count=2600)
    widget._last_plot_update = float("-inf")
    now[0] = 0.2
    widget._poll_controller()
    assert widget.raw_widget.model.total_rows == 2500
    assert widget.raw_widget.model.page_start == 1000
    now[0] = 1.2
    widget._poll_controller()
    assert widget.raw_widget.model.total_rows == 2600
    assert widget.raw_widget.model.page_start == 1000


def test_slow_statistics_worker_keeps_one_thread_and_latest_pending(qtbot, monkeypatch):
    controller = FakeController(ControllerState(connection="disconnected", output="unknown"))
    widget = MainWindow(controller=controller)
    qtbot.addWidget(widget)
    widget._records = sample_records(1000)
    started = threading.Event()
    release = threading.Event()
    calls: list[tuple[float, float]] = []

    def slow_statistics(_source, start, end):
        calls.append((start, end))
        started.set()
        if len(calls) == 1:
            release.wait(2.0)
        return stats_for_window(start, end)

    monkeypatch.setattr("powermonitor.ui.window.compute_statistics", slow_statistics)
    widget._start_statistics(0.0, 0.1)
    pump_until(qtbot, started.is_set)
    for index in range(1, 101):
        widget._start_statistics(index / 10.0, index / 10.0 + 0.1)
    assert widget._statistics_future is not None
    assert not widget._statistics_future.done()
    assert widget._pending_statistics is not None
    assert widget._pending_statistics[1:3] == pytest.approx((10.0, 10.1))

    release.set()
    pump_until(qtbot, lambda: len(calls) == 2)
    pump_until(qtbot, lambda: widget._statistics_future is None)
    assert calls == pytest.approx([(0.0, 0.1), (10.0, 10.1)])
    assert "10.000000" in widget.stats_widget.status_label.text()


def test_close_during_real_statistics_worker_stays_open(qtbot, monkeypatch):
    controller = FakeController(ControllerState(connection="disconnected", output="unknown"))
    widget = MainWindow(controller=controller)
    qtbot.addWidget(widget)
    widget.show()
    modal_calls = []
    monkeypatch.setattr(
        QtWidgets.QMessageBox,
        "warning",
        lambda *args, **kwargs: modal_calls.append(args),
    )
    started = threading.Event()
    release = threading.Event()

    def slow_statistics(*_args, **_kwargs):
        started.set()
        release.wait(2.0)
        return stats_for_window(0.0, 1.0)

    monkeypatch.setattr("powermonitor.ui.window.compute_statistics", slow_statistics)
    widget._records = sample_records(100)
    widget._start_statistics(0.0, 1.0)
    pump_until(qtbot, started.is_set)
    assert not widget.close()
    assert widget.isVisible()
    assert not controller.shutdown_called
    assert not modal_calls
    assert "still finishing" in widget.error_label.text()

    release.set()
    pump_until(qtbot, lambda: widget._statistics_future is None)
    assert widget.close()
    assert controller.shutdown_called
    assert widget._statistics_executor_shutdown


def test_successful_close_shuts_down_statistics_executor(qtbot, monkeypatch):
    controller = FakeController(ControllerState(connection="disconnected", output="unknown"))
    widget = MainWindow(controller=controller)
    qtbot.addWidget(widget)
    monkeypatch.setattr(QtWidgets.QMessageBox, "warning", lambda *args, **kwargs: None)
    assert widget.close()
    assert widget._statistics_executor_shutdown
    old_generation = widget._stats_generation
    widget._records = sample_records(10)
    widget._start_statistics(0.0, 0.001)
    assert widget._stats_generation == old_generation + 1
    assert widget._statistics_future is None


def test_close_while_statistics_thread_runs_stays_open_without_modal(qtbot, monkeypatch):
    controller = FakeController(ControllerState(connection="disconnected", output="unknown"))
    widget = MainWindow(controller=controller)
    qtbot.addWidget(widget)
    widget.show()
    modal_calls = []
    monkeypatch.setattr(
        QtWidgets.QMessageBox,
        "warning",
        lambda *args, **kwargs: modal_calls.append(args),
    )

    thread = QtCore.QThread(widget)
    monkeypatch.setattr(thread, "isRunning", lambda: True)

    widget._threads.append(thread)
    assert not widget.close()
    assert widget.isVisible()
    assert not controller.shutdown_called
    assert not modal_calls
    assert "still finishing" in widget.error_label.text()
    widget._threads.clear()
    widget.close()


def test_close_discards_deleted_qthread_wrapper(qtbot, monkeypatch):
    controller = FakeController(ControllerState(connection="disconnected", output="unknown"))
    widget = MainWindow(controller=controller)
    qtbot.addWidget(widget)
    monkeypatch.setattr(QtWidgets.QMessageBox, "warning", lambda *args, **kwargs: None)

    thread = QtCore.QThread(widget)
    widget._threads.append(thread)
    widget._workers.append(QtCore.QObject())
    widget._thread_workers[thread] = widget._workers[-1]
    thread.deleteLater()
    QtCore.QCoreApplication.sendPostedEvents(thread, QtCore.QEvent.Type.DeferredDelete)
    assert not shiboken6.isValid(thread)

    assert widget.close()
    assert controller.shutdown_called
    assert thread not in widget._threads
    assert thread not in widget._thread_workers
    assert not widget._workers


def test_close_never_connected_does_not_warn(qtbot, monkeypatch):
    controller = FakeController(ControllerState(connection="disconnected", output="unknown"))
    widget = MainWindow(controller=controller)
    qtbot.addWidget(widget)
    warnings = []
    monkeypatch.setattr(
        QtWidgets.QMessageBox,
        "warning",
        lambda *args, **kwargs: warnings.append(args),
    )
    widget.close()
    assert controller.shutdown_called
    assert not warnings


def test_close_unknown_after_connection_warns(qtbot, monkeypatch):
    controller = FakeController(ControllerState(connection="connected", output="unknown"))
    widget = MainWindow(controller=controller)
    qtbot.addWidget(widget)
    warnings = []
    monkeypatch.setattr(
        QtWidgets.QMessageBox,
        "warning",
        lambda *args, **kwargs: warnings.append(args),
    )
    widget.close()
    assert controller.shutdown_called
    assert warnings


def _table_text(table: QtWidgets.QTableWidget) -> str:
    parts = []
    for row in range(table.rowCount()):
        for column in range(table.columnCount()):
            item = table.item(row, column)
            if item is not None:
                parts.append(item.text())
    return "\n".join(parts)
