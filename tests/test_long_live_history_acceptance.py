"""Acceptance tests for complete history during and after a long capture."""

import time
from pathlib import Path

import numpy as np
import pytest
from PySide6 import QtWidgets

from powermonitor.models import SAMPLE_DTYPE, ControllerState, SampleBatch
from powermonitor.storage import CaptureWriter, GrowingCaptureReader
from powermonitor.ui.window import MainWindow


def batch(start: int, count: int) -> SampleBatch:
    values = np.zeros(count, dtype=SAMPLE_DTYPE)
    indices = np.arange(start, start + count, dtype=np.uint64)
    values["index"] = indices
    values["time_s"] = indices / 5000
    values["host_elapsed_s"] = values["time_s"]
    values["current_a"] = 0.04
    values["voltage_v"] = 3.3
    return SampleBatch(values)


class Controller:
    def __init__(self, path: Path):
        self.state = ControllerState(
            connection="connected",
            output="on",
            capture="running",
            recording_path=path,
            sample_count=1_000_000,
        )
        self.requests = []

    def poll_events(self):
        return []

    def recent_records(self):
        return batch(950_000, 50_000).records.copy()

    def request(self, action, **kwargs):
        self.requests.append((action, kwargs))

    def shutdown(self, timeout=3):
        return True


def close(qtbot, window):
    window.controller.state = ControllerState(connection="disconnected", output="off")
    window._state = window.controller.state
    deadline = time.monotonic() + 3
    while not window._wait_for_background_tasks() and time.monotonic() < deadline:
        QtWidgets.QApplication.processEvents()
        qtbot.wait(10)
    window.close()


def test_growing_reader_exposes_complete_file_not_stale_metadata(tmp_path):
    path = tmp_path / "long.pmcap"
    writer = CaptureWriter(path)
    writer.append(batch(0, 1_000_000))
    reader = GrowingCaptureReader(path)
    assert len(reader.records) == 1_000_000
    assert reader.time_bounds == pytest.approx((0.0, 200.0))
    assert reader.metadata["sample_count"] == 0
    writer.close()


def test_live_window_uses_full_200_second_history_not_recent_tail(tmp_path, qtbot):
    path = tmp_path / "long.pmcap"
    writer = CaptureWriter(path)
    writer.append(batch(0, 1_000_000))
    controller = Controller(path)
    window = MainWindow(controller=controller)
    qtbot.addWidget(window)
    window.show()
    window._poll_recent_records(force=True, sample_count=1_000_000)
    assert window.plot_widget._bounds == pytest.approx((0.0, 200.0))
    assert window._records is not None and len(window._records) == 1_000_000
    close(qtbot, window)
    writer.close()


def test_stop_transition_preserves_view_and_full_count(tmp_path, qtbot):
    path = tmp_path / "long.pmcap"
    writer = CaptureWriter(path)
    writer.append(batch(0, 1_000_000))
    controller = Controller(path)
    window = MainWindow(controller=controller)
    qtbot.addWidget(window)
    window.show()
    window._poll_recent_records(force=True, sample_count=1_000_000)
    view_box = window.plot_widget.plots[0].getViewBox()
    window.follow_check.setChecked(False)
    view_box.setRange(xRange=(20.0, 40.0), yRange=(0.03, 0.05), padding=0)
    before = view_box.viewRange()
    writer.close()
    controller.state = ControllerState(
        connection="connected",
        output="on",
        capture="idle",
        recording_path=path,
        sample_count=1_000_000,
    )
    window._capture_finished(path)
    after = view_box.viewRange()
    assert window._reader is not None
    assert len(window._reader.records) == 1_000_000
    assert after[0] == pytest.approx(before[0])
    assert after[1] == pytest.approx(before[1])
    close(qtbot, window)
