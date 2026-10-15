"""Cross-component acceptance for live zoom that keeps following latest data."""

import time

import numpy as np
import pytest
from PySide6 import QtWidgets

from powermonitor.models import SAMPLE_DTYPE, ControllerState
from powermonitor.ui.plots import make_demo_records
from powermonitor.ui.window import MainWindow


class Controller:
    def __init__(self):
        self.recent = make_demo_records(60_000)
        self.state = ControllerState(
            connection="connected",
            output="on",
            capture="running",
            sample_count=len(self.recent),
        )
        self.recent_calls = 0

    def poll_events(self):
        return []

    def recent_records(self):
        self.recent_calls += 1
        return self.recent.copy()

    def shutdown(self, timeout=3):
        return True


def extend(records, count=5_000):
    result = np.empty(len(records) + count, dtype=SAMPLE_DTYPE)
    result[: len(records)] = records
    indices = np.arange(len(records), len(result), dtype=np.uint64)
    result["index"][len(records) :] = indices
    result["time_s"][len(records) :] = indices / 5000
    result["host_elapsed_s"][len(records) :] = indices / 5000
    result["current_a"][len(records) :] = 0.04
    result["voltage_v"][len(records) :] = 3.3
    result["flags"][len(records) :] = 0
    result["dropped"][len(records) :] = 0
    return result


def close(qtbot, window):
    window.controller.state = ControllerState(connection="disconnected", output="off")
    window._state = window.controller.state
    deadline = time.monotonic() + 3
    while not window._wait_for_background_tasks() and time.monotonic() < deadline:
        QtWidgets.QApplication.processEvents()
        qtbot.wait(10)
    window.close()


def test_live_x_zoom_keeps_follow_and_window_width(qtbot):
    controller = Controller()
    window = MainWindow(controller=controller)
    qtbot.addWidget(window)
    window.show()
    window._poll_recent_records(force=True, sample_count=len(controller.recent))
    view_box = window.plot_widget.plots[0].getViewBox()
    latest = float(controller.recent["time_s"][-1] + 1 / 5000)
    view_box.setXRange(latest - 2.0, latest, padding=0)
    view_box.sigRangeChangedManually.emit([True, False])
    assert window.follow_check.isChecked()
    before = tuple(view_box.viewRange()[0])
    width = before[1] - before[0]

    controller.recent = extend(controller.recent)
    controller.state = ControllerState(
        connection="connected",
        output="on",
        capture="running",
        sample_count=len(controller.recent),
    )
    window._poll_recent_records(force=True, sample_count=len(controller.recent))
    after = tuple(view_box.viewRange()[0])
    assert window.follow_check.isChecked()
    assert after[1] > before[1]
    assert after[1] - after[0] == pytest.approx(width)
    assert window.plot_widget.selected_range() == pytest.approx(after)
    close(qtbot, window)


def test_only_checkbox_disables_live_follow(qtbot):
    controller = Controller()
    window = MainWindow(controller=controller)
    qtbot.addWidget(window)
    window.show()
    window._poll_recent_records(force=True, sample_count=len(controller.recent))
    before_calls = controller.recent_calls
    window.follow_check.setChecked(False)
    controller.recent = extend(controller.recent)
    controller.state = ControllerState(
        connection="connected",
        output="on",
        capture="running",
        sample_count=len(controller.recent),
    )
    for _ in range(5):
        window._poll_controller()
    assert controller.recent_calls == before_calls
    close(qtbot, window)
