"""Main PySide6 desktop window for safe HVPM capture and replay."""

from __future__ import annotations

import itertools
import math
import time
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from typing import Any

import numpy as np
from PySide6 import QtCore, QtWidgets

from powermonitor.models import SAMPLE_PERIOD_S, ControllerEvent, ControllerState, DeviceInfo
from powermonitor.statistics import WindowStatistics, compute_statistics
from powermonitor.storage import CaptureReader, GrowingCaptureReader, export_csv
from powermonitor.ui.plots import PowerPlotWidget, make_demo_records
from powermonitor.ui.table import RawDataWidget, SelectionSummaryWidget, StatisticsWidget
from powermonitor.ui.theme import LIGHT, THEMES, ChartTheme, apply_application_theme

_LIVE_PLOT_INTERVAL_S = 0.5


class _StatisticsSnapshot:
    """Immutable memmap snapshot retained by one statistics request."""

    def __init__(self, records: np.ndarray) -> None:
        self.records = records

    def iter_chunks(
        self,
        start: float,
        end: float,
        chunk_size: int = 100_000,
    ) -> Any:
        times = self.records["time_s"]
        lo = int(np.searchsorted(times, start - SAMPLE_PERIOD_S, side="right"))
        hi = int(np.searchsorted(times, end, side="left"))
        for offset in range(lo, hi, chunk_size):
            chunk = self.records[offset : min(hi, offset + chunk_size)]
            chunk = chunk[chunk["time_s"] + SAMPLE_PERIOD_S - start > 1e-12]
            if len(chunk):
                yield chunk


def _compute_statistics_result(
    source: Any,
    start_s: float,
    end_s: float,
) -> tuple[WindowStatistics | None, str]:
    try:
        if isinstance(source, (CaptureReader, GrowingCaptureReader, _StatisticsSnapshot)):
            stats = compute_statistics(
                source.iter_chunks(start_s, end_s, chunk_size=200_000),
                start_s,
                end_s,
            )
        else:
            stats = compute_statistics(source, start_s, end_s)
    except Exception as exc:  # pragma: no cover - exercised through UI error path
        return None, str(exc)
    return stats, ""


class _ExportWorker(QtCore.QObject):
    finished = QtCore.Signal(int, str, str)

    def __init__(
        self,
        generation: int,
        reader: CaptureReader,
        destination: Path,
        start_s: float | None,
        end_s: float | None,
    ) -> None:
        super().__init__()
        self.generation = generation
        self.reader = reader
        self.destination = Path(destination)
        self.start_s = start_s
        self.end_s = end_s

    @QtCore.Slot()
    def run(self) -> None:
        try:
            export_csv(self.reader, self.destination, self.start_s, self.end_s)
        except Exception as exc:
            self.finished.emit(self.generation, str(self.destination), str(exc))
        else:
            self.finished.emit(self.generation, str(self.destination), "")


class MainWindow(QtWidgets.QMainWindow):
    """Safe capture UI; all hardware operations go through AcquisitionController."""

    def __init__(self, controller: Any | None = None) -> None:
        super().__init__()
        if controller is None:
            from powermonitor.acquisition import AcquisitionController

            controller = AcquisitionController()
        self.controller = controller
        self._state = getattr(controller, "state", ControllerState())
        self._reader: CaptureReader | None = None
        self._live_reader: GrowingCaptureReader | None = None
        self._live_path = (
            Path(self._state.recording_path) if self._state.recording_path is not None else None
        )
        self._records: np.ndarray | None = None
        self._loaded_path: Path | None = None
        self._stats_generation = 0
        self._export_generation = 0
        self._threads: list[QtCore.QThread] = []
        self._workers: list[QtCore.QObject] = []
        self._thread_workers: dict[QtCore.QThread, QtCore.QObject] = {}
        self._statistics_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="pm-stats")
        self._statistics_future: Future[tuple[WindowStatistics | None, str]] | None = None
        self._statistics_future_generation: int | None = None
        self._pending_statistics: tuple[Any, float, float, int] | None = None
        self._statistics_executor_shutdown = False
        self._last_live_statistics = 0.0
        self._ever_connected = self._state.connection != "disconnected"
        self._last_table_update = 0.0
        self._last_live_sample_count: int | None = None
        self._last_live_record_index: int | None = None
        self._last_plot_update = float("-inf")
        self._last_raw_table_record_index: int | None = None
        self._raw_table_refresh_pending = False
        self._capture_counter = itertools.count(1)
        self._theme = LIGHT

        self.setWindowTitle("Monsoon HVPM Power Monitor")
        self.resize(1320, 880)
        self._build_ui()
        self._apply_theme(self._theme)
        self._refresh_state(self._state)

        self._statistics_poll_timer = QtCore.QTimer(self)
        self._statistics_poll_timer.setInterval(20)
        self._statistics_poll_timer.timeout.connect(self._poll_statistics_future)

        self.poll_timer = QtCore.QTimer(self)
        self.poll_timer.setInterval(50)
        self.poll_timer.timeout.connect(self._poll_controller)
        self.poll_timer.start()

    def load_demo_records(self, count: int = 20_000) -> None:
        records = make_demo_records(count)
        self._invalidate_data_source()
        self._records = records
        self.plot_widget.set_records(records)
        self.raw_widget.set_records(records)
        self._set_reader_warnings(())
        start, end = 0.0, min(1.0, float(records["time_s"][-1] + SAMPLE_PERIOD_S))
        self.plot_widget.set_selection(start, end, emit=False)
        self._plot_selection_changed(start, end)
        self.loaded_label.setText("Offline demo data (not a live capture)")
        self._refresh_export_controls()

    def open_capture(self, path: Path) -> None:
        reader = CaptureReader(Path(path))
        self._invalidate_data_source()
        self._reader = reader
        self._loaded_path = Path(path)
        self._records = reader.records
        self.plot_widget.set_records(reader.records, full_bounds=reader.time_bounds)
        self.raw_widget.set_records(reader.records)
        self._set_reader_warnings(reader.warnings)
        start, end = reader.time_bounds
        if end > start:
            self.plot_widget.set_selection(start, end, emit=False)
            self._plot_selection_changed(start, end)
        else:
            self.stats_widget.set_statistics(None)
        self.loaded_label.setText(f"Replay: {Path(path)}")
        self._refresh_export_controls()

    def export_full_csv(self, destination: Path) -> None:
        if self._reader is None:
            self._set_error("Open or finish a recording before exporting CSV")
            return
        self._start_export(destination, None, None)

    def export_selection_csv(self, destination: Path) -> None:
        if self._reader is None:
            self._set_error("Open or finish a recording before exporting CSV")
            return
        start, end = self.plot_widget.selected_range()
        self._start_export(destination, start, end)

    def closeEvent(self, event: Any) -> None:  # noqa: N802 - Qt API name
        if not self._wait_for_background_tasks():
            self.poll_timer.start()
            self._set_error(
                "Statistics or CSV export is still finishing; this window will remain open. "
                "Close again after it completes."
            )
            event.ignore()
            return

        self.poll_timer.stop()
        self._shutdown_statistics_executor()
        pre_state = self._state
        ok = True
        try:
            ok = bool(self.controller.shutdown(timeout=3.0))
        except Exception as exc:
            ok = False
            self._set_error(f"Shutdown failed: {exc}")
        state = getattr(self.controller, "state", self._state)
        if not ok:
            state = replace(state, connection="error", output="unknown")
        self._refresh_state(state)
        unsafe = (not ok) or state.connection == "error"
        if pre_state.connection != "disconnected" and pre_state.output != "off":
            unsafe = True
        if unsafe:
            self.output_label.setText("Output: unknown")
            QtWidgets.QMessageBox.warning(
                self,
                "Shutdown status unknown",
                "Controller shutdown did not prove the HVPM output is off. "
                "Treat the output state as unknown and check the device before touching the DUT.",
            )
        event.accept()

    def _build_ui(self) -> None:
        central = QtWidgets.QWidget()
        self.setCentralWidget(central)
        outer = QtWidgets.QHBoxLayout(central)
        outer.setContentsMargins(10, 10, 10, 10)

        self.plot_widget = PowerPlotWidget(theme=self._theme)
        self.plot_widget.selectionChanged.connect(self._plot_selection_changed)
        self.stats_widget = StatisticsWidget()
        self.selection_summary = SelectionSummaryWidget()
        self.raw_widget = RawDataWidget()

        controls = QtWidgets.QWidget()
        controls.setMaximumWidth(390)
        controls_layout = QtWidgets.QVBoxLayout(controls)
        controls_layout.setContentsMargins(0, 0, 0, 0)
        controls_layout.addWidget(self._build_device_group())
        controls_layout.addWidget(self._build_power_group())
        controls_layout.addWidget(self._build_capture_group())
        controls_layout.addWidget(self._build_selection_group())
        controls_layout.addStretch(1)

        self.status_label = QtWidgets.QLabel("Ready")
        self.status_label.setWordWrap(True)
        self.error_label = QtWidgets.QLabel("")
        self.error_label.setObjectName("errorLabel")
        self.error_label.setWordWrap(True)
        controls_layout.addWidget(self.status_label)
        controls_layout.addWidget(self.error_label)

        waveform_page = QtWidgets.QWidget()
        waveform_layout = QtWidgets.QVBoxLayout(waveform_page)
        waveform_layout.setContentsMargins(6, 6, 6, 6)
        waveform_layout.addWidget(self.plot_widget, 1)
        waveform_layout.addWidget(self.selection_summary)

        self.tabs = QtWidgets.QTabWidget()
        self.tabs.addTab(waveform_page, "Waveforms")
        self.tabs.addTab(self.stats_widget, "Statistics")
        self.tabs.addTab(self.raw_widget, "Raw samples")
        self.tabs.currentChanged.connect(self._tab_changed)

        outer.addWidget(controls)
        outer.addWidget(self.tabs, 1)

    def _build_device_group(self) -> QtWidgets.QGroupBox:
        group = QtWidgets.QGroupBox("Device")
        layout = QtWidgets.QGridLayout(group)
        self.backend_combo = QtWidgets.QComboBox()
        self.backend_combo.addItem("Simulator", "simulator")
        self.backend_combo.addItem("Monsoon HVPM", "monsoon")
        self.scan_button = QtWidgets.QPushButton("Scan")
        self.device_combo = QtWidgets.QComboBox()
        self.device_combo.setEditable(False)
        self.connect_button = QtWidgets.QPushButton("Connect")
        self.disconnect_button = QtWidgets.QPushButton("Disconnect")
        self.connection_label = QtWidgets.QLabel("Connection: disconnected")
        self.output_label = QtWidgets.QLabel("Output: unknown")
        self.capture_label = QtWidgets.QLabel("Capture: idle")
        self.device_label = QtWidgets.QLabel("Device: none")
        self.device_label.setWordWrap(True)

        layout.addWidget(QtWidgets.QLabel("Backend"), 0, 0)
        layout.addWidget(self.backend_combo, 0, 1, 1, 2)
        layout.addWidget(self.scan_button, 0, 3)
        layout.addWidget(QtWidgets.QLabel("Serial"), 1, 0)
        layout.addWidget(self.device_combo, 1, 1, 1, 3)
        layout.addWidget(self.connect_button, 2, 1)
        layout.addWidget(self.disconnect_button, 2, 2)
        layout.addWidget(self.connection_label, 3, 0, 1, 4)
        layout.addWidget(self.output_label, 4, 0, 1, 4)
        layout.addWidget(self.capture_label, 5, 0, 1, 4)
        layout.addWidget(self.device_label, 6, 0, 1, 4)

        self.scan_button.clicked.connect(self._scan_clicked)
        self.connect_button.clicked.connect(self._connect_clicked)
        self.disconnect_button.clicked.connect(lambda: self.controller.request("disconnect"))
        return group

    def _build_power_group(self) -> QtWidgets.QGroupBox:
        group = QtWidgets.QGroupBox("Power output")
        layout = QtWidgets.QGridLayout(group)
        self.voltage_spin = QtWidgets.QDoubleSpinBox()
        self.voltage_spin.setRange(0.80, 13.50)
        self.voltage_spin.setDecimals(2)
        self.voltage_spin.setSingleStep(0.01)
        self.voltage_spin.setValue(3.30)
        self.voltage_spin.valueChanged.connect(self._voltage_edited)
        self.apply_voltage_button = QtWidgets.QPushButton("Apply target")
        self.enable_button = QtWidgets.QPushButton("Enable output")
        self.off_button = QtWidgets.QPushButton("OFF")
        self.off_button.setObjectName("offButton")
        self.off_button.setMinimumHeight(36)
        self.target_label = QtWidgets.QLabel(
            "Target voltage is staged only until Enable is confirmed"
        )
        self.target_label.setWordWrap(True)

        layout.addWidget(QtWidgets.QLabel("Target voltage"), 0, 0)
        layout.addWidget(self.voltage_spin, 0, 1)
        layout.addWidget(QtWidgets.QLabel("V"), 0, 2)
        layout.addWidget(self.apply_voltage_button, 1, 0, 1, 3)
        layout.addWidget(self.enable_button, 2, 0, 1, 2)
        layout.addWidget(self.off_button, 2, 2)
        layout.addWidget(self.target_label, 3, 0, 1, 3)

        self.apply_voltage_button.clicked.connect(self._apply_voltage_clicked)
        self.enable_button.clicked.connect(self._enable_clicked)
        self.off_button.clicked.connect(lambda: self.controller.request("disable_output"))
        return group

    def _build_capture_group(self) -> QtWidgets.QGroupBox:
        group = QtWidgets.QGroupBox("Capture and replay")
        layout = QtWidgets.QGridLayout(group)
        self.record_path_edit = QtWidgets.QLineEdit(str(self._next_recording_path()))
        self.record_path_edit.setReadOnly(True)
        self.new_path_button = QtWidgets.QPushButton("New path")
        self.run_button = QtWidgets.QPushButton("RUN")
        self.stop_button = QtWidgets.QPushButton("STOP")
        self.open_button = QtWidgets.QPushButton("Open replay")
        self.export_full_button = QtWidgets.QPushButton("Export full CSV")
        self.export_selection_button = QtWidgets.QPushButton("Export selection CSV")
        self.follow_check = QtWidgets.QCheckBox("Follow latest")
        self.follow_check.setChecked(True)
        self.theme_combo = QtWidgets.QComboBox()
        self.theme_combo.addItem("Light", "light")
        self.theme_combo.addItem("Dark", "dark")
        self.loaded_label = QtWidgets.QLabel("No replay loaded")
        self.loaded_label.setWordWrap(True)

        layout.addWidget(QtWidgets.QLabel("Next recording"), 0, 0)
        layout.addWidget(self.record_path_edit, 0, 1, 1, 2)
        layout.addWidget(self.new_path_button, 0, 3)
        layout.addWidget(self.run_button, 1, 0, 1, 2)
        layout.addWidget(self.stop_button, 1, 2, 1, 2)
        layout.addWidget(self.open_button, 2, 0, 1, 2)
        layout.addWidget(self.export_full_button, 2, 2, 1, 2)
        layout.addWidget(self.export_selection_button, 3, 0, 1, 2)
        layout.addWidget(self.follow_check, 3, 2, 1, 2)
        layout.addWidget(QtWidgets.QLabel("Theme"), 4, 0)
        layout.addWidget(self.theme_combo, 4, 1, 1, 3)
        layout.addWidget(self.loaded_label, 5, 0, 1, 4)

        self.new_path_button.clicked.connect(self._new_recording_path_clicked)
        self.run_button.clicked.connect(self._run_clicked)
        self.stop_button.clicked.connect(lambda: self.controller.request("stop"))
        self.open_button.clicked.connect(self._open_clicked)
        self.export_full_button.clicked.connect(self._export_full_clicked)
        self.export_selection_button.clicked.connect(self._export_selection_clicked)
        self.follow_check.toggled.connect(self._follow_toggled)
        self.plot_widget.followChanged.connect(self._plot_follow_changed)
        self.theme_combo.currentIndexChanged.connect(self._theme_changed)
        return group

    def _build_selection_group(self) -> QtWidgets.QGroupBox:
        group = QtWidgets.QGroupBox("Selection window")
        layout = QtWidgets.QGridLayout(group)
        self.selection_start_spin = QtWidgets.QDoubleSpinBox()
        self.selection_end_spin = QtWidgets.QDoubleSpinBox()
        for spin in (self.selection_start_spin, self.selection_end_spin):
            spin.setRange(0.0, 1_000_000_000.0)
            spin.setDecimals(6)
            spin.setSingleStep(0.001)
            spin.setKeyboardTracking(False)
        self.apply_selection_button = QtWidgets.QPushButton("Apply numeric window")
        self.window_label = QtWidgets.QLabel("Window: n/a")
        self.window_label.setWordWrap(True)
        layout.addWidget(QtWidgets.QLabel("Start"), 0, 0)
        layout.addWidget(self.selection_start_spin, 0, 1)
        layout.addWidget(QtWidgets.QLabel("s"), 0, 2)
        layout.addWidget(QtWidgets.QLabel("End"), 1, 0)
        layout.addWidget(self.selection_end_spin, 1, 1)
        layout.addWidget(QtWidgets.QLabel("s"), 1, 2)
        layout.addWidget(self.apply_selection_button, 2, 0, 1, 3)
        layout.addWidget(self.window_label, 3, 0, 1, 3)
        self.apply_selection_button.clicked.connect(self._apply_numeric_selection)
        return group

    @QtCore.Slot()
    def _scan_clicked(self) -> None:
        self._set_error("")
        self.controller.request("scan", backend=self._selected_backend())

    @QtCore.Slot()
    def _connect_clicked(self) -> None:
        self._set_error("")
        serial = self.device_combo.currentData()
        if serial is None:
            text = self.device_combo.currentText().strip()
            serial = text or None
        self.controller.request("connect", backend=self._selected_backend(), serial=serial)

    @QtCore.Slot()
    def _apply_voltage_clicked(self) -> None:
        self._set_error("")
        self.controller.request("set_voltage", voltage_v=self.voltage_spin.value())

    @QtCore.Slot(float)
    def _voltage_edited(self, voltage: float) -> None:
        staged = math.isclose(voltage, self._state.target_voltage_v, abs_tol=0.004)
        self.enable_button.setEnabled(
            staged
            and self._state.connection == "connected"
            and self._state.capture in {"idle", "error"}
            and self._state.output == "off"
        )
        if staged:
            self.target_label.setText(f"Staged target: {self._state.target_voltage_v:.2f} V")
        else:
            self.target_label.setText(
                f"Edited {voltage:.2f} V; apply it before enabling "
                f"(staged {self._state.target_voltage_v:.2f} V)"
            )

    @QtCore.Slot()
    def _enable_clicked(self) -> None:
        staged_voltage = self._state.target_voltage_v
        edited_voltage = self.voltage_spin.value()
        if not math.isclose(edited_voltage, staged_voltage, abs_tol=0.004):
            self._set_error(
                f"Apply target {edited_voltage:.2f} V before enabling; "
                f"the controller is still staged at {staged_voltage:.2f} V"
            )
            return
        answer = QtWidgets.QMessageBox.question(
            self,
            "Confirm HVPM output",
            f"Enable HVPM main output at staged target {staged_voltage:.2f} V?\n"
            "RUN, Connect and Open Replay never enable output automatically.",
            QtWidgets.QMessageBox.StandardButton.Yes | QtWidgets.QMessageBox.StandardButton.No,
            QtWidgets.QMessageBox.StandardButton.No,
        )
        if answer == QtWidgets.QMessageBox.StandardButton.Yes:
            self.controller.request("enable_output", confirmed=True)

    @QtCore.Slot()
    def _run_clicked(self) -> None:
        path = Path(self.record_path_edit.text())
        if path.exists():
            path = self._next_recording_path()
            self.record_path_edit.setText(str(path))
        self._invalidate_data_source(clear_display=True)
        self._live_path = path
        self.controller.request("start", path=path)
        self.loaded_label.setText(f"Live recording (raw samples retained): {path}")
        self._refresh_export_controls()

    @QtCore.Slot()
    def _new_recording_path_clicked(self) -> None:
        self.record_path_edit.setText(str(self._next_recording_path()))

    @QtCore.Slot()
    def _open_clicked(self) -> None:
        directory = QtWidgets.QFileDialog.getExistingDirectory(
            self,
            "Open .pmcap recording directory",
            str(Path(self.record_path_edit.text()).parent),
        )
        if not directory:
            return
        try:
            self.open_capture(Path(directory))
        except Exception as exc:
            self._set_error(f"Open replay failed: {exc}")

    @QtCore.Slot()
    def _export_full_clicked(self) -> None:
        destination = self._select_csv_destination("Export full CSV")
        if destination is not None:
            self.export_full_csv(destination)

    @QtCore.Slot()
    def _export_selection_clicked(self) -> None:
        destination = self._select_csv_destination("Export selection CSV")
        if destination is not None:
            self.export_selection_csv(destination)

    @QtCore.Slot()
    def _apply_numeric_selection(self) -> None:
        start = self.selection_start_spin.value()
        end = self.selection_end_spin.value()
        if end <= start:
            self._set_error("Selection end must be after start")
            return
        if self._capture_active():
            self._set_follow_enabled(False)
        self.plot_widget.set_selection(start, end, emit=True)

    @QtCore.Slot(bool)
    def _follow_toggled(self, enabled: bool) -> None:
        self.plot_widget.set_follow_enabled(enabled)
        if not enabled:
            if self._capture_active():
                self._stats_generation += 1
                self._pending_statistics = None
            return
        if self._capture_active():
            self._poll_live_records(force=True, sample_count=self._state.sample_count)
            self._last_plot_update = time.monotonic()

    @QtCore.Slot(bool)
    def _plot_follow_changed(self, enabled: bool) -> None:
        with QtCore.QSignalBlocker(self.follow_check):
            self.follow_check.setChecked(enabled)

    @QtCore.Slot(int)
    def _tab_changed(self, _index: int) -> None:
        if self._raw_tab_visible():
            self._refresh_raw_table(force=True)

    @QtCore.Slot(float, float)
    def _plot_selection_changed(self, start: float, end: float) -> None:
        self._set_numeric_selection(start, end)
        self.window_label.setText(f"Window: {start:.6f} s to {end:.6f} s")
        if self._capture_active() and self._follow_active():
            return
        self._start_statistics(start, end)

    @QtCore.Slot(int)
    def _theme_changed(self, _index: int = 0) -> None:
        theme = THEMES[str(self.theme_combo.currentData())]
        self._apply_theme(theme)

    def _apply_theme(self, theme: ChartTheme) -> None:
        self._theme = theme
        app = QtWidgets.QApplication.instance()
        if isinstance(app, QtWidgets.QApplication):
            apply_application_theme(app, theme)
        if hasattr(self, "plot_widget"):
            self.plot_widget.set_theme(theme)

    def _selected_backend(self) -> str:
        return str(self.backend_combo.currentData() or "simulator")

    def _capture_active(self, state: ControllerState | None = None) -> bool:
        active_state = getattr(self.controller, "state", self._state) if state is None else state
        if active_state is None:
            active_state = self._state
        return active_state.capture not in {"idle", "error"}

    def _follow_active(self) -> bool:
        return self.follow_check.isChecked()

    def _set_follow_enabled(self, enabled: bool) -> None:
        if self.follow_check.isChecked() == enabled:
            self.plot_widget.set_follow_enabled(enabled)
            return
        self.follow_check.setChecked(enabled)

    def _raw_tab_visible(self) -> bool:
        return self.tabs.currentWidget() is self.raw_widget

    def _poll_controller(self) -> None:
        try:
            events = self.controller.poll_events()
        except Exception as exc:
            self._set_error(f"Controller polling failed: {exc}")
            events = []
        for event in events:
            self._handle_event(event)
        state = getattr(self.controller, "state", self._state)
        if state != self._state:
            self._refresh_state(state)
        if self._capture_active(state):
            now = time.monotonic()
            if now - self._last_plot_update >= _LIVE_PLOT_INTERVAL_S:
                self._poll_live_records(sample_count=state.sample_count)
                self._last_plot_update = now
            if self._raw_table_refresh_pending:
                self._refresh_raw_table()

    def _handle_event(self, event: ControllerEvent) -> None:
        if event.kind == "state" and isinstance(event.payload, ControllerState):
            self._refresh_state(event.payload)
        elif event.kind == "devices":
            self._set_devices(event.payload or [])
        elif event.kind == "error":
            self._set_error(str(event.payload))
        elif event.kind == "capture_finished":
            self._capture_finished(Path(event.payload))

    def _refresh_state(self, state: ControllerState) -> None:
        self._state = state
        if state.capture not in {"idle", "error"} and state.recording_path is not None:
            self._live_path = Path(state.recording_path)
        connected = state.connection == "connected"
        connection_active = state.connection in {"connected", "connecting", "error"}
        self._ever_connected |= connected
        capture_active = state.capture not in {"idle", "error"}
        power_config_allowed = connected and not capture_active and state.output == "off"
        target_is_current = math.isclose(
            self.voltage_spin.value(), state.target_voltage_v, abs_tol=0.004
        )
        self.connection_label.setText(f"Connection: {state.connection}")
        self.output_label.setText(f"Output: {state.output}")
        self.capture_label.setText(f"Capture: {state.capture}")
        if state.device is None:
            self.device_label.setText("Device: none")
        else:
            self.device_label.setText(
                f"Device: {state.device.name} serial {state.device.serial} "
                f"firmware {state.device.firmware}"
            )
        self.status_label.setText(
            f"Backend {state.backend}; target {state.target_voltage_v:.2f} V; "
            f"samples {state.sample_count:,}"
        )
        self._voltage_edited(self.voltage_spin.value())
        self.error_label.setText(state.error)
        self.error_label.setVisible(bool(state.error))

        self.backend_combo.setEnabled(not connection_active and not capture_active)
        self.scan_button.setEnabled(not connection_active and not capture_active)
        self.device_combo.setEnabled(not connection_active and not capture_active)
        self.connect_button.setEnabled(not connection_active and not capture_active)
        self.disconnect_button.setEnabled(connection_active and not capture_active)
        self.voltage_spin.setEnabled(not capture_active)
        self.apply_voltage_button.setEnabled(power_config_allowed)
        self.enable_button.setEnabled(power_config_allowed and target_is_current)
        self.off_button.setEnabled(connection_active or capture_active or state.device is not None)
        self.run_button.setEnabled(connected and state.capture == "idle")
        self.stop_button.setEnabled(capture_active)
        self.open_button.setEnabled(not capture_active)
        self.new_path_button.setEnabled(not capture_active)
        self.record_path_edit.setEnabled(not capture_active)
        self._refresh_export_controls()

    def _poll_live_records(
        self,
        *,
        force: bool = False,
        sample_count: int | None = None,
    ) -> None:
        reader = self._refresh_live_reader()
        if reader is not None:
            records = reader.records
            record_index = self._tail_record_index(records)
            if not force and record_index == self._last_live_record_index:
                self._last_live_sample_count = sample_count
                return
            self._last_live_sample_count = sample_count
            self._last_live_record_index = record_index
            self._records = records
            self.plot_widget.set_records(
                records,
                full_bounds=reader.time_bounds,
                live=True,
                preserve_view=True,
                preserve_history=True,
            )
            self._set_reader_warnings(reader.warnings)
            self._refresh_raw_table(records=records)
            self._refresh_live_statistics(reader)
            return

        if not self._follow_active():
            return
        if not force and sample_count is not None and sample_count == self._last_live_sample_count:
            return
        try:
            records = self.controller.recent_records()
        except Exception as exc:
            self._set_error(f"Live sample polling failed: {exc}")
            return
        record_index = self._tail_record_index(records)
        if not force and record_index is not None and record_index == self._last_live_record_index:
            self._last_live_sample_count = sample_count
            return
        self._last_live_sample_count = sample_count
        self._last_live_record_index = record_index
        self._records = records
        self.plot_widget.set_records(
            records,
            live=True,
            preserve_view=True,
            preserve_history=True,
        )
        self._refresh_raw_table(records=records)
        self._refresh_live_statistics(records)

    def _poll_recent_records(
        self,
        *,
        force: bool = False,
        sample_count: int | None = None,
    ) -> None:
        """Compatibility wrapper for callers that explicitly request a live refresh."""
        self._poll_live_records(force=force, sample_count=sample_count)

    def _refresh_live_reader(self) -> GrowingCaptureReader | None:
        path = self._live_path
        if path is None:
            return None
        if self._live_reader is None:
            if not path.is_dir():
                return None
            if not (path / "metadata.json").is_file() or not (path / "samples.bin").is_file():
                return None
            try:
                self._live_reader = GrowingCaptureReader(path)
            except (FileNotFoundError, NotADirectoryError):
                return None
            except Exception as exc:
                self._set_error(f"Live recording open failed: {exc}")
                return None
        try:
            self._live_reader.refresh()
        except (FileNotFoundError, NotADirectoryError):
            return None
        except Exception as exc:
            self._set_error(f"Live recording refresh failed: {exc}")
        return self._live_reader

    def _refresh_live_statistics(self, source: Any) -> None:
        now = time.monotonic()
        records = source.records if isinstance(source, GrowingCaptureReader) else source
        if len(records) and now - self._last_live_statistics >= 0.5:
            start, end = self.plot_widget.selected_range()
            statistics_source = (
                _StatisticsSnapshot(records)
                if isinstance(source, GrowingCaptureReader)
                else records
            )
            self._start_statistics(start, end, source=statistics_source)
            self._last_live_statistics = now

    def _tail_record_index(self, records: np.ndarray | None) -> int | None:
        if records is None or not len(records):
            return None
        return int(records["index"][-1])

    def _refresh_raw_table(
        self,
        *,
        records: np.ndarray | None = None,
        force: bool = False,
    ) -> None:
        if not self._raw_tab_visible():
            return
        records = self._records if records is None else records
        record_index = self._tail_record_index(records)
        now = time.monotonic()
        if not force:
            if record_index == self._last_raw_table_record_index:
                self._raw_table_refresh_pending = False
                return
            raw_update_recent = now - self._last_table_update < 1.0
            if self._last_raw_table_record_index is not None and raw_update_recent:
                self._raw_table_refresh_pending = True
                return
        self.raw_widget.set_records(records)
        self._last_raw_table_record_index = record_index
        self._last_table_update = now
        self._raw_table_refresh_pending = False

    def _set_devices(self, devices: list[DeviceInfo]) -> None:
        self.device_combo.clear()
        for device in devices:
            text = f"{device.serial} ({device.name}, {device.backend})"
            self.device_combo.addItem(text, device.serial)
        if not devices:
            self.device_combo.addItem("", None)
        self.status_label.setText(f"Scan found {len(devices)} device(s)")

    def _capture_finished(self, path: Path) -> None:
        self.record_path_edit.setText(str(self._next_recording_path()))
        if not path.exists():
            self._live_reader = None
            self._live_path = None
            self.loaded_label.setText("Capture was cancelled before a recording was created")
            return
        try:
            reader = CaptureReader(path)
        except Exception as exc:
            self._set_error(f"Recording finished but replay open failed: {exc}")
            return

        self._stats_generation += 1
        self._pending_statistics = None
        self._reader = reader
        self._live_reader = None
        self._live_path = None
        self._loaded_path = path
        self._records = reader.records
        self._last_live_record_index = self._tail_record_index(reader.records)
        self.plot_widget.set_records(
            reader.records,
            full_bounds=reader.time_bounds,
            live=False,
            preserve_view=True,
            preserve_history=True,
        )
        self._refresh_raw_table(records=reader.records, force=True)
        self._set_reader_warnings(reader.warnings)
        self.loaded_label.setText(f"Replay: {path}")
        self._refresh_export_controls()
        start, end = self.plot_widget.selected_range()
        if end > start:
            self._set_numeric_selection(start, end)
            self.window_label.setText(f"Window: {start:.6f} s to {end:.6f} s")
            self._start_statistics(start, end)

    def _start_statistics(self, start: float, end: float, *, source: Any = None) -> None:
        if end <= start or not math.isfinite(start) or not math.isfinite(end):
            return
        if source is None:
            source = self._reader
        if source is None and self._live_reader is not None:
            source = _StatisticsSnapshot(self._live_reader.records)
        if source is None:
            source = self._records
        if source is None:
            return
        if isinstance(source, np.ndarray) and not len(source):
            return
        self._stats_generation += 1
        generation = self._stats_generation
        self.stats_widget.show_busy(start, end)
        self.selection_summary.show_busy(start, end)
        request = (source, start, end, generation)
        if self._statistics_worker_running():
            self._pending_statistics = request
            return
        self._launch_statistics_worker(*request)

    def _statistics_worker_running(self) -> bool:
        future = self._statistics_future
        return future is not None and not future.done()

    def _launch_statistics_worker(
        self,
        source: Any,
        start: float,
        end: float,
        generation: int,
    ) -> None:
        if self._statistics_executor_shutdown:
            return
        self._statistics_future_generation = generation
        self._statistics_future = self._statistics_executor.submit(
            _compute_statistics_result,
            source,
            start,
            end,
        )
        if not self._statistics_poll_timer.isActive():
            self._statistics_poll_timer.start()

    @QtCore.Slot()
    def _poll_statistics_future(self) -> None:
        future = self._statistics_future
        if future is None:
            if self._pending_statistics is None:
                self._statistics_poll_timer.stop()
            return
        if not future.done():
            return
        generation = self._statistics_future_generation
        self._statistics_future = None
        self._statistics_future_generation = None
        try:
            stats, error = future.result()
        except Exception as exc:  # pragma: no cover - defensive executor boundary
            stats, error = None, str(exc)
        if generation is not None:
            self._statistics_finished(generation, stats, error)
        pending = self._pending_statistics
        self._pending_statistics = None
        if (
            pending is not None
            and not self._statistics_executor_shutdown
            and pending[3] == self._stats_generation
        ):
            self._launch_statistics_worker(*pending)
        else:
            self._statistics_poll_timer.stop()

    @QtCore.Slot(int, object, str)
    def _statistics_finished(
        self, generation: int, stats: WindowStatistics | None, error: str
    ) -> None:
        if generation != self._stats_generation:
            return
        if error:
            self.stats_widget.show_error(error)
            self.selection_summary.show_error(error)
            return
        self.stats_widget.set_statistics(stats)
        self.selection_summary.set_statistics(stats)

    def _start_export(self, destination: Path, start_s: float | None, end_s: float | None) -> None:
        if self._reader is None:
            return
        self._export_generation += 1
        generation = self._export_generation
        self.status_label.setText(f"Exporting CSV to {destination} ...")
        worker = _ExportWorker(generation, self._reader, Path(destination), start_s, end_s)
        thread = QtCore.QThread(self)
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.finished.connect(self._export_finished)
        worker.finished.connect(thread.quit)
        worker.finished.connect(worker.deleteLater)
        thread.finished.connect(self._background_thread_finished)
        self._threads.append(thread)
        self._workers.append(worker)
        self._thread_workers[thread] = worker
        thread.start()

    @QtCore.Slot(int, str, str)
    def _export_finished(self, generation: int, destination: str, error: str) -> None:
        if generation != self._export_generation:
            return
        if error:
            self._set_error(f"CSV export failed: {error}")
        else:
            self.status_label.setText(f"CSV exported: {destination}")
            self._set_error("")

    @QtCore.Slot()
    def _background_thread_finished(self) -> None:
        sender = self.sender()
        if not isinstance(sender, QtCore.QThread):
            return
        thread = sender
        worker = self._thread_workers.pop(thread, None)
        try:
            self._threads.remove(thread)
        except ValueError:
            pass
        if worker is not None:
            try:
                self._workers.remove(worker)
            except ValueError:
                pass
        thread.deleteLater()

    def _wait_for_background_tasks(self) -> bool:
        if self._statistics_worker_running():
            return False
        self._poll_statistics_future()
        if self._statistics_worker_running():
            return False
        for thread in list(self._threads):
            try:
                if thread.isRunning():
                    return False
            except RuntimeError:
                self._discard_deleted_thread(thread)
        return True

    def _shutdown_statistics_executor(self) -> None:
        if self._statistics_executor_shutdown:
            return
        self._statistics_executor_shutdown = True
        self._pending_statistics = None
        self._statistics_poll_timer.stop()
        self._statistics_executor.shutdown(wait=True)

    def _discard_deleted_thread(self, thread: QtCore.QThread) -> None:
        worker = self._thread_workers.pop(thread, None)
        try:
            self._threads.remove(thread)
        except ValueError:
            pass
        if worker is not None:
            try:
                self._workers.remove(worker)
            except ValueError:
                pass

    def _invalidate_data_source(self, *, clear_display: bool = False) -> None:
        self._stats_generation += 1
        self._pending_statistics = None
        self._reader = None
        self._live_reader = None
        self._live_path = None
        self._loaded_path = None
        self._last_live_sample_count = None
        self._last_live_record_index = None
        self._last_raw_table_record_index = None
        self._raw_table_refresh_pending = False
        self._last_table_update = 0.0
        self._last_live_statistics = 0.0
        self._last_plot_update = float("-inf")
        self._set_reader_warnings(())
        if clear_display:
            self._records = None
            self.plot_widget.set_records(None)
            self.raw_widget.set_records(None)
            self.stats_widget.set_statistics(None)
            self.selection_summary.set_statistics(None)
            self.window_label.setText("Window: n/a")

    def _set_reader_warnings(self, warnings: Any) -> None:
        self.stats_widget.set_reader_warnings(warnings)
        self.selection_summary.set_reader_warnings(warnings)

    def _refresh_export_controls(self) -> None:
        has_reader = self._reader is not None
        self.export_full_button.setEnabled(has_reader)
        self.export_selection_button.setEnabled(has_reader)

    def _set_numeric_selection(self, start: float, end: float) -> None:
        self._updating_selection_controls = True
        try:
            self.selection_start_spin.setValue(max(0.0, start))
            self.selection_end_spin.setValue(max(0.0, end))
        finally:
            self._updating_selection_controls = False

    def _set_error(self, message: str) -> None:
        self.error_label.setText(message)
        self.error_label.setVisible(bool(message))

    def _select_csv_destination(self, title: str) -> Path | None:
        default_parent = self._recording_parent()
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        default_name = default_parent / f"powermonitor-{stamp}.csv"
        filename, _selected_filter = QtWidgets.QFileDialog.getSaveFileName(
            self,
            title,
            str(default_name),
            "CSV files (*.csv)",
        )
        return Path(filename) if filename else None

    def _recording_parent(self) -> Path:
        base = QtCore.QStandardPaths.writableLocation(
            QtCore.QStandardPaths.StandardLocation.CacheLocation
        )
        if base:
            parent = Path(base) / "recordings"
        else:
            parent = Path.home() / ".cache" / "powermonitor" / "recordings"
        parent.mkdir(parents=True, exist_ok=True)
        return parent

    def _next_recording_path(self) -> Path:
        parent = self._recording_parent()
        while True:
            stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
            path = parent / f"capture-{stamp}-{next(self._capture_counter):04d}.pmcap"
            if not path.exists():
                return path
