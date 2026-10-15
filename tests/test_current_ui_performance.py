"""Cross-component acceptance tests for the current-only live UI."""

import time

import numpy as np
from PySide6 import QtCore, QtWidgets

from powermonitor.models import SAMPLE_DTYPE, ControllerState
from powermonitor.ui.plots import make_demo_records
from powermonitor.ui.window import MainWindow


class CountingController:
    def __init__(self):
        self.state = ControllerState(connection="connected", output="on", capture="running")
        self.recent = make_demo_records(1000)
        self.recent_calls = 0
        self.requests = []

    def poll_events(self):
        return []

    def recent_records(self):
        self.recent_calls += 1
        return self.recent.copy()

    def request(self, action, **kwargs):
        self.requests.append((action, kwargs))

    def shutdown(self, timeout=3):
        return True


def close_window(qtbot, window):
    window.controller.state = ControllerState(
        connection="disconnected", output="off", capture="idle"
    )
    window._state = window.controller.state
    deadline = time.monotonic() + 3
    while window._threads and time.monotonic() < deadline:
        QtWidgets.QApplication.processEvents()
        qtbot.wait(10)
    assert not window._threads
    window.close()
    QtCore.QCoreApplication.sendPostedEvents(None, QtCore.QEvent.Type.DeferredDelete)


def extend_records(records, count=250):
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


def test_paused_live_view_does_no_snapshot_or_curve_work(qtbot):
    controller = CountingController()
    window = MainWindow(controller=controller)
    qtbot.addWidget(window)
    window.show()
    window._poll_controller()
    assert controller.recent_calls == 1
    baseline = window.plot_widget.selected_range()

    window.follow_check.setChecked(False)
    controller.recent = extend_records(controller.recent, 1000)
    controller.state = ControllerState(
        connection="connected",
        output="on",
        capture="running",
        sample_count=len(controller.recent),
    )
    for _ in range(20):
        window._poll_controller()
        QtWidgets.QApplication.processEvents()
    assert controller.recent_calls == 1
    assert window.plot_widget.selected_range() == baseline
    close_window(qtbot, window)


def test_resuming_follow_fetches_latest_immediately(qtbot):
    controller = CountingController()
    window = MainWindow(controller=controller)
    qtbot.addWidget(window)
    window.show()
    window._poll_controller()
    window.follow_check.setChecked(False)
    controller.recent = extend_records(controller.recent, 1000)
    controller.state = ControllerState(
        connection="connected",
        output="on",
        capture="running",
        sample_count=len(controller.recent),
    )
    before = controller.recent_calls
    window.follow_check.setChecked(True)
    QtWidgets.QApplication.processEvents()
    assert controller.recent_calls == before + 1
    assert window.plot_widget.selected_range()[1] >= controller.recent["time_s"][-1]
    close_window(qtbot, window)


def test_current_only_live_tick_budget(qtbot):
    controller = CountingController()
    controller.recent = make_demo_records(50_000)
    controller.state = ControllerState(
        connection="connected", output="on", capture="running", sample_count=50_000
    )
    window = MainWindow(controller=controller)
    qtbot.addWidget(window)
    window.resize(1300, 850)
    window.show()
    # Measure the expensive refresh callback directly; idle 50 ms polls are
    # intentionally cheap and do not belong in the active-refresh percentile.
    samples = []
    for _ in range(30):
        # Change the visible tail so render-signature caching cannot turn this
        # into an idle-path benchmark.
        controller.recent["index"][-1] += 1
        controller.recent["time_s"][-1] += 1 / 5000
        started = time.perf_counter()
        window._poll_recent_records(force=True, sample_count=controller.state.sample_count)
        QtWidgets.QApplication.processEvents()
        samples.append(time.perf_counter() - started)
    active_p95 = float(np.percentile(samples, 95))
    assert active_p95 < 0.030, f"active live refresh p95 {active_p95:.6f}s exceeds 30ms"
    close_window(qtbot, window)
