"""Regression tests for manual Y zoom across live data-source updates."""

import time
from dataclasses import replace

import numpy as np
import pytest
from PySide6 import QtWidgets

from powermonitor.models import SAMPLE_DTYPE, ControllerState, SampleBatch
from powermonitor.storage import CaptureWriter
from powermonitor.ui.window import MainWindow


def records(count, offset=0, current=0.04):
    result = np.zeros(count, dtype=SAMPLE_DTYPE)
    index = np.arange(offset, offset + count)
    result["index"] = index
    result["time_s"] = index / 5000
    result["host_elapsed_s"] = result["time_s"]
    result["current_a"] = current
    result["voltage_v"] = 3.3
    return result


class Controller:
    def __init__(self, path=None):
        self.state = ControllerState(
            connection="connected",
            output="on",
            capture="running",
            recording_path=path,
        )
        self.recent = records(1000)

    def poll_events(self):
        return []

    def recent_records(self):
        return self.recent.copy()

    def shutdown(self, timeout=3):
        return True


def close(qtbot, window):
    deadline = time.monotonic() + 3
    while not window._wait_for_background_tasks() and time.monotonic() < deadline:
        QtWidgets.QApplication.processEvents()
        qtbot.wait(10)
    window.controller.state = replace(
        window.controller.state, connection="disconnected", output="off", capture="idle"
    )
    window._state = window.controller.state
    window.close()


def test_growing_reader_refresh_preserves_manual_y_range_and_stop(tmp_path, qtbot):
    path = tmp_path / "live.pmcap"
    writer = CaptureWriter(path)
    writer.append(SampleBatch(records(10_000)))
    controller = Controller(path)
    controller.state = replace(controller.state, sample_count=10_000)
    window = MainWindow(controller=controller)
    qtbot.addWidget(window)
    window.show()
    window._poll_live_records(force=True, sample_count=10_000)
    view_box = window.plot_widget.plots[0].getViewBox()
    view_box.setYRange(0.037, 0.043, padding=0)
    view_box.sigRangeChangedManually.emit([False, True])
    expected = tuple(view_box.viewRange()[1])

    for start in (10_000, 12_500):
        writer.append(SampleBatch(records(2500, start)))
        controller.state = replace(controller.state, sample_count=start + 2500)
        window._poll_live_records(force=True, sample_count=start + 2500)
        assert tuple(view_box.viewRange()[1]) == pytest.approx(expected)
        assert window.follow_check.isChecked()

    writer.close()
    controller.state = replace(controller.state, capture="idle", recording_path=path)
    window._capture_finished(path)
    assert tuple(view_box.viewRange()[1]) == pytest.approx(expected)
    close(qtbot, window)


def test_recent_fallback_refresh_preserves_manual_y_range(qtbot):
    controller = Controller()
    controller.state = replace(controller.state, sample_count=1000)
    window = MainWindow(controller=controller)
    qtbot.addWidget(window)
    window.show()
    window._poll_live_records(force=True, sample_count=1000)
    view_box = window.plot_widget.plots[0].getViewBox()
    view_box.setYRange(0.035, 0.045, padding=0)
    view_box.sigRangeChangedManually.emit([False, True])
    expected = tuple(view_box.viewRange()[1])

    controller.recent = records(1500)
    controller.state = replace(controller.state, sample_count=1500)
    window._poll_live_records(force=True, sample_count=1500)
    assert tuple(view_box.viewRange()[1]) == pytest.approx(expected)
    close(qtbot, window)
