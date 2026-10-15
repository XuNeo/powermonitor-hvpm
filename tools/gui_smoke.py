"""Drive the real Qt window with simulated hardware and capture evidence."""

import argparse
import json
import math
import time
from pathlib import Path

from PySide6 import QtCore, QtTest, QtWidgets

from powermonitor.storage import CaptureReader
from powermonitor.ui.window import MainWindow


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("output", type=Path)
    parser.add_argument("--seconds", type=int, default=4)
    args = parser.parse_args()
    if args.seconds < 4:
        parser.error("At least four seconds are required for the selection workflow")
    args.output.mkdir(parents=True, exist_ok=False)
    app = QtWidgets.QApplication([])
    window = MainWindow()
    window.show()
    window.record_path_edit.setText(str(args.output / "capture.pmcap"))
    errors = []
    results = {}

    def wait_for(predicate, timeout=10):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            app.processEvents()
            if predicate():
                return
            QtTest.QTest.qWait(10)
        raise TimeoutError(f"GUI condition timed out: {window.controller.state}")

    def click(button):
        QtTest.QTest.mouseClick(button, QtCore.Qt.MouseButton.LeftButton)
        app.processEvents()

    def answer_confirmation():
        for widget in app.topLevelWidgets():
            if isinstance(widget, QtWidgets.QMessageBox) and widget.isVisible():
                button = widget.button(QtWidgets.QMessageBox.StandardButton.Yes)
                if button is not None:
                    click(button)
                    return
        raise RuntimeError("Enable confirmation dialog was not shown")

    try:
        wait_for(lambda: window.isVisible())
        click(window.scan_button)
        wait_for(lambda: window.device_combo.count() > 0)
        click(window.connect_button)
        wait_for(lambda: window.controller.state.connection == "connected")
        assert window.controller.state.output == "off"
        window.voltage_spin.setValue(3.3)
        click(window.apply_voltage_button)
        wait_for(lambda: window.controller.state.target_voltage_v == 3.3)
        wait_for(lambda: window.enable_button.isEnabled())
        QtCore.QTimer.singleShot(50, answer_confirmation)
        click(window.enable_button)
        wait_for(lambda: window.controller.state.output == "on")
        click(window.run_button)
        wait_for(
            lambda: window.controller.state.sample_count >= args.seconds * 5000,
            timeout=args.seconds + 15,
        )
        view_box = window.plot_widget.plots[0].getViewBox()
        live_range = tuple(view_box.viewRange()[0])
        view_box.setXRange(live_range[0], (live_range[0] + live_range[1]) / 2, padding=0)
        view_box.sigRangeChangedManually.emit([True, False])
        assert window.follow_check.isChecked()
        zoomed_range = tuple(view_box.viewRange()[0])
        zoomed_selection = window.plot_widget.selected_range()
        zoomed_width = zoomed_selection[1] - zoomed_selection[0]
        sample_count = window.controller.state.sample_count
        wait_for(lambda: window.controller.state.sample_count >= sample_count + 1000)
        wait_for(lambda: window.plot_widget.selected_range()[1] > zoomed_range[1])
        followed_range = tuple(view_box.viewRange()[0])
        followed_selection = window.plot_widget.selected_range()
        assert math.isclose(
            followed_selection[1] - followed_selection[0], zoomed_width, rel_tol=1e-3, abs_tol=1e-5
        )
        assert math.isclose(followed_selection[1], followed_range[1], abs_tol=1e-3)

        window.follow_check.setChecked(False)
        frozen_range = tuple(view_box.viewRange()[0])
        frozen_end = window.plot_widget.selected_range()[1]
        sample_count = window.controller.state.sample_count
        wait_for(lambda: window.controller.state.sample_count >= sample_count + 1000)
        assert tuple(view_box.viewRange()[0]) == frozen_range
        assert window.plot_widget.selected_range()[1] == frozen_end
        visible_end = window.controller.state.sample_count / 5000
        selection_start, selection_end = max(0.5, visible_end - 3), max(1.5, visible_end - 2)
        window.selection_start_spin.setValue(selection_start)
        window.selection_end_spin.setValue(selection_end)
        click(window.apply_selection_button)
        wait_for(lambda: f"{selection_end:.6f}" in window.stats_widget.status_label.text())
        window.plot_widget._show_cursor(selection_start + 0.6)
        manual_y = (0.035, 0.045)
        view_box.setYRange(*manual_y, padding=0)
        view_box.sigRangeChangedManually.emit([False, True])
        sample_count = window.controller.state.sample_count
        wait_for(lambda: window.controller.state.sample_count >= sample_count + 1000)
        actual_y = tuple(view_box.viewRange()[1])
        assert all(math.isclose(a, b, abs_tol=1e-6) for a, b in zip(actual_y, manual_y))
        window.grab().save(str(args.output / "live-light.png"))
        window.theme_combo.setCurrentIndex(window.theme_combo.findData("dark"))
        app.processEvents()
        window.grab().save(str(args.output / "live-dark.png"))
        stop_started = time.monotonic()
        click(window.stop_button)
        wait_for(lambda: window.controller.state.capture == "idle")
        results["stop_s"] = time.monotonic() - stop_started
        assert window.controller.state.output == "on"
        wait_for(lambda: window._reader is not None)
        click(window.off_button)
        wait_for(lambda: window.controller.state.output == "off")
        window.selection_start_spin.setValue(0.5)
        window.selection_end_spin.setValue(1.5)
        click(window.apply_selection_button)
        csv_path = args.output / "selection.csv"
        window.export_selection_csv(csv_path)
        wait_for(lambda: csv_path.exists() and "CSV exported" in window.status_label.text())
        assert len(csv_path.read_text().splitlines()) == 5001
        window.tabs.setCurrentWidget(window.stats_widget)
        app.processEvents()
        window.grab().save(str(args.output / "statistics.png"))
        window.tabs.setCurrentWidget(window.raw_widget)
        app.processEvents()
        window.grab().save(str(args.output / "raw-table.png"))
        reader = CaptureReader(args.output / "capture.pmcap")
        results.update(
            requested_seconds=args.seconds,
            samples=len(reader.records),
            complete=reader.metadata["complete"],
            csv_samples=5000,
            output_off_ack=window.controller.state.output == "off",
            platform=app.platformName(),
        )
        if not window.close():
            wait_for(window._wait_for_background_tasks)
            assert window.close()
        app.processEvents()
    except Exception as exc:
        errors.append(str(exc))
        window.controller.request("disable_output")
        window.controller.shutdown(timeout=3)
        raise
    finally:
        (args.output / "result.json").write_text(
            json.dumps({**results, "errors": errors}, indent=2)
        )
    print(json.dumps(results, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
