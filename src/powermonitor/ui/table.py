"""Paged data and statistics views."""

from __future__ import annotations

from collections.abc import Sequence

import numpy as np
from PySide6 import QtCore, QtWidgets

from powermonitor.statistics import WindowStatistics

from .formatting import (
    format_charge_c,
    format_charge_mah,
    format_count,
    format_current_a,
    format_energy_j,
    format_energy_mwh,
    format_power_w,
    format_ratio,
    format_time_s,
    format_voltage_v,
)


class SampleTableModel(QtCore.QAbstractTableModel):
    """Virtual table model that exposes one bounded page of samples."""

    COLUMNS = (
        "index",
        "time_s",
        "current_a",
        "voltage_v",
        "power_w",
        "flags",
        "dropped",
    )

    def __init__(self, parent: QtCore.QObject | None = None) -> None:
        super().__init__(parent)
        self._records: np.ndarray | None = None
        self._page_start = 0
        self._page_size = 1000

    @property
    def total_rows(self) -> int:
        return 0 if self._records is None else len(self._records)

    @property
    def page_start(self) -> int:
        return self._page_start

    @property
    def page_size(self) -> int:
        return self._page_size

    def set_records(self, records: np.ndarray | None) -> None:
        self.beginResetModel()
        self._records = records
        self._page_start = min(self._page_start, self._last_page_start())
        self.endResetModel()

    def set_page_size(self, page_size: int) -> None:
        page_size = max(1, int(page_size))
        if page_size == self._page_size:
            return
        self.beginResetModel()
        self._page_size = page_size
        self._page_start = min(self._page_start, self._last_page_start())
        self.endResetModel()

    def set_page_start(self, page_start: int) -> None:
        page_start = max(0, min(int(page_start), self._last_page_start()))
        if page_start == self._page_start:
            return
        self.beginResetModel()
        self._page_start = page_start
        self.endResetModel()

    def rowCount(
        self,
        parent: QtCore.QModelIndex | QtCore.QPersistentModelIndex = QtCore.QModelIndex(),
    ) -> int:
        if parent.isValid() or self._records is None:
            return 0
        return min(self._page_size, max(0, len(self._records) - self._page_start))

    def columnCount(
        self,
        parent: QtCore.QModelIndex | QtCore.QPersistentModelIndex = QtCore.QModelIndex(),
    ) -> int:
        return 0 if parent.isValid() else len(self.COLUMNS)

    def data(
        self,
        index: QtCore.QModelIndex | QtCore.QPersistentModelIndex,
        role: int = QtCore.Qt.ItemDataRole.DisplayRole,
    ) -> str | int | float | None:
        if not index.isValid() or self._records is None:
            return None
        if role not in (QtCore.Qt.ItemDataRole.DisplayRole, QtCore.Qt.ItemDataRole.ToolTipRole):
            return None
        record = self._records[self._page_start + index.row()]
        column = self.COLUMNS[index.column()]
        if column == "index":
            return str(int(record["index"]))
        if column == "time_s":
            return f"{float(record['time_s']):.6f}"
        if column == "current_a":
            return f"{float(record['current_a']):.9g}"
        if column == "voltage_v":
            return f"{float(record['voltage_v']):.9g}"
        if column == "power_w":
            return f"{float(record['current_a'] * record['voltage_v']):.9g}"
        if column == "flags":
            return str(int(record["flags"]))
        if column == "dropped":
            return str(int(record["dropped"]))
        return None

    def headerData(
        self,
        section: int,
        orientation: QtCore.Qt.Orientation,
        role: int = QtCore.Qt.ItemDataRole.DisplayRole,
    ) -> str | None:
        if role != QtCore.Qt.ItemDataRole.DisplayRole:
            return None
        if orientation == QtCore.Qt.Orientation.Horizontal:
            return self.COLUMNS[section]
        return str(self._page_start + section)

    def _last_page_start(self) -> int:
        total = self.total_rows
        if total <= self._page_size:
            return 0
        return ((total - 1) // self._page_size) * self._page_size


class RawDataWidget(QtWidgets.QWidget):
    """Paged raw-sample table; never copies the whole recording."""

    def __init__(self, parent: QtWidgets.QWidget | None = None) -> None:
        super().__init__(parent)
        self.model = SampleTableModel(self)
        self.view = QtWidgets.QTableView()
        self.view.setModel(self.model)
        self.view.setAlternatingRowColors(True)
        self.view.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectionBehavior.SelectRows)
        self.view.setSelectionMode(QtWidgets.QAbstractItemView.SelectionMode.SingleSelection)
        self.view.horizontalHeader().setStretchLastSection(True)
        self.view.verticalHeader().setVisible(False)

        self.previous_button = QtWidgets.QPushButton("Previous page")
        self.next_button = QtWidgets.QPushButton("Next page")
        self.page_size = QtWidgets.QSpinBox()
        self.page_size.setRange(100, 200_000)
        self.page_size.setSingleStep(100)
        self.page_size.setValue(self.model.page_size)
        self.page_label = QtWidgets.QLabel("No samples")

        controls = QtWidgets.QHBoxLayout()
        controls.addWidget(self.previous_button)
        controls.addWidget(self.next_button)
        controls.addSpacing(12)
        controls.addWidget(QtWidgets.QLabel("Rows per page"))
        controls.addWidget(self.page_size)
        controls.addStretch(1)
        controls.addWidget(self.page_label)

        layout = QtWidgets.QVBoxLayout(self)
        layout.addLayout(controls)
        layout.addWidget(self.view)

        self.previous_button.clicked.connect(self.previous_page)
        self.next_button.clicked.connect(self.next_page)
        self.page_size.valueChanged.connect(self._page_size_changed)
        self._refresh_controls()

    def set_records(self, records: np.ndarray | None) -> None:
        self.model.set_records(records)
        self._refresh_controls()

    @QtCore.Slot()
    def previous_page(self) -> None:
        self.model.set_page_start(self.model.page_start - self.model.page_size)
        self._refresh_controls()

    @QtCore.Slot()
    def next_page(self) -> None:
        self.model.set_page_start(self.model.page_start + self.model.page_size)
        self._refresh_controls()

    @QtCore.Slot(int)
    def _page_size_changed(self, value: int) -> None:
        self.model.set_page_size(value)
        self._refresh_controls()

    def _refresh_controls(self) -> None:
        total = self.model.total_rows
        start = self.model.page_start
        end = min(total, start + self.model.page_size)
        self.previous_button.setEnabled(start > 0)
        self.next_button.setEnabled(end < total)
        if total:
            self.page_label.setText(f"Rows {start + 1:,}-{end:,} of {total:,}")
        else:
            self.page_label.setText("No samples")


class SelectionSummaryWidget(QtWidgets.QGroupBox):
    """Compact selection results kept visible beside the waveforms."""

    def __init__(self, parent: QtWidgets.QWidget | None = None) -> None:
        super().__init__("Selection summary", parent)
        self.setAccessibleName("Current waveform selection summary")
        self._reader_warnings: tuple[str, ...] = ()

        self.status_label = QtWidgets.QLabel("No selection statistics yet")
        self.mean_current_label = QtWidgets.QLabel("n/a")
        self.mean_power_label = QtWidgets.QLabel("n/a")
        self.duration_label = QtWidgets.QLabel("n/a")
        self.samples_label = QtWidgets.QLabel("n/a")
        self.warning_label = QtWidgets.QLabel("")
        self.warning_label.setObjectName("warningLabel")
        self.warning_label.setWordWrap(True)

        metrics = QtWidgets.QHBoxLayout()
        for title, value in (
            ("Mean current", self.mean_current_label),
            ("Mean power", self.mean_power_label),
            ("Valid duration", self.duration_label),
            ("Valid samples", self.samples_label),
        ):
            column = QtWidgets.QVBoxLayout()
            heading = QtWidgets.QLabel(title)
            heading.setObjectName("summaryHeading")
            value.setTextInteractionFlags(QtCore.Qt.TextInteractionFlag.TextSelectableByMouse)
            column.addWidget(heading)
            column.addWidget(value)
            metrics.addLayout(column, 1)

        layout = QtWidgets.QVBoxLayout(self)
        layout.addWidget(self.status_label)
        layout.addLayout(metrics)
        layout.addWidget(self.warning_label)
        self._update_warnings(None)

    def set_reader_warnings(self, warnings: Sequence[str]) -> None:
        self._reader_warnings = tuple(warnings)
        self._update_warnings(None)

    def show_busy(self, start: float, end: float) -> None:
        self.status_label.setText(f"Updating {start:.6f} s to {end:.6f} s ...")

    def show_error(self, message: str) -> None:
        self.status_label.setText("Selection statistics failed")
        self._clear_metrics()
        warnings = (*self._reader_warnings, message)
        self.warning_label.setText("\n".join(warnings))
        self.warning_label.setVisible(True)

    def set_statistics(self, stats: WindowStatistics | None) -> None:
        if stats is None:
            self.status_label.setText("No selection statistics yet")
            self._clear_metrics()
            self._update_warnings(None)
            return
        self.status_label.setText(
            f"Window {stats.start_s:.6f} s to {stats.end_s:.6f} s "
            f"({format_ratio(stats.coverage)} coverage)"
        )
        self.mean_current_label.setText(format_current_a(stats.mean_current_a))
        self.mean_power_label.setText(format_power_w(stats.mean_power_w))
        self.duration_label.setText(format_time_s(stats.valid_duration_s))
        self.samples_label.setText(
            f"{format_count(stats.valid_sample_count)} / {format_count(stats.sample_count)}"
        )
        self._update_warnings(stats)

    def _clear_metrics(self) -> None:
        for label in (
            self.mean_current_label,
            self.mean_power_label,
            self.duration_label,
            self.samples_label,
        ):
            label.setText("n/a")

    def _update_warnings(self, stats: WindowStatistics | None) -> None:
        warnings = list(self._reader_warnings)
        if (
            stats is not None
            and stats.loss_unknown
            and not any("loss" in item.lower() for item in warnings)
        ):
            warnings.append("Loss unknown in selection; time and integrals are incomplete")
        if stats is not None and stats.gap_count:
            warnings.append("Selected samples contain gaps")
        self.warning_label.setText("\n".join(warnings))
        self.warning_label.setVisible(bool(warnings))


class StatisticsWidget(QtWidgets.QWidget):
    """Detailed selection statistics with persistent recording warnings."""

    def __init__(self, parent: QtWidgets.QWidget | None = None) -> None:
        super().__init__(parent)
        self.status_label = QtWidgets.QLabel("No selection statistics yet")
        self.status_label.setWordWrap(True)
        self.warning_label = QtWidgets.QLabel("")
        self.warning_label.setObjectName("warningLabel")
        self.warning_label.setWordWrap(True)
        self.table = QtWidgets.QTableWidget(0, 2)
        self.table.setHorizontalHeaderLabels(["Metric", "Value"])
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QtWidgets.QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setAlternatingRowColors(True)
        layout = QtWidgets.QVBoxLayout(self)
        layout.addWidget(self.status_label)
        layout.addWidget(self.warning_label)
        layout.addWidget(self.table, 1)
        self._reader_warnings: tuple[str, ...] = ()

    def set_reader_warnings(self, warnings: Sequence[str]) -> None:
        self._reader_warnings = tuple(warnings)
        self._update_warning_label(None)

    def show_busy(self, start: float, end: float) -> None:
        self.status_label.setText(f"Computing statistics for {start:.6f} s to {end:.6f} s...")

    def show_error(self, message: str) -> None:
        self.status_label.setText("Statistics failed")
        self.table.setRowCount(0)
        warnings = list(self._reader_warnings)
        warnings.append(message)
        self.warning_label.setText("\n".join(warnings))
        self.warning_label.setVisible(True)

    def set_statistics(self, stats: WindowStatistics | None) -> None:
        if stats is None:
            self.status_label.setText("No selection statistics yet")
            self.table.setRowCount(0)
            self._update_warning_label(None)
            return
        self.status_label.setText(
            f"Selection {stats.start_s:.6f} s to {stats.end_s:.6f} s, "
            f"duration {stats.duration_s:.6f} s"
        )
        rows = [
            ("Samples in window", format_count(stats.sample_count)),
            ("Valid samples", format_count(stats.valid_sample_count)),
            ("Valid duration", format_time_s(stats.valid_duration_s)),
            ("Coverage", format_ratio(stats.coverage)),
            ("Mean current", format_current_a(stats.mean_current_a)),
            ("Min current", format_current_a(stats.min_current_a)),
            ("Max current", format_current_a(stats.max_current_a)),
            ("Mean voltage", format_voltage_v(stats.mean_voltage_v)),
            ("Mean power", format_power_w(stats.mean_power_w)),
            ("Charge", format_charge_c(stats.charge_c)),
            ("Charge", format_charge_mah(stats.charge_mah)),
            ("Energy", format_energy_j(stats.energy_j)),
            ("Energy", format_energy_mwh(stats.energy_mwh)),
            ("Invalid samples", format_count(stats.invalid_count)),
            ("Gap markers", format_count(stats.gap_count)),
            ("Loss unknown in selection", "yes" if stats.loss_unknown else "no"),
        ]
        self.table.setRowCount(len(rows))
        for row, (name, value) in enumerate(rows):
            name_item = QtWidgets.QTableWidgetItem(name)
            value_item = QtWidgets.QTableWidgetItem(value)
            if value == "n/a" or (isinstance(value, str) and value.endswith("no")):
                value_item.setForeground(self.palette().brush(self.foregroundRole()))
            self.table.setItem(row, 0, name_item)
            self.table.setItem(row, 1, value_item)
        self.table.resizeColumnsToContents()
        self._update_warning_label(stats)

    def _update_warning_label(self, stats: WindowStatistics | None) -> None:
        warnings = list(self._reader_warnings)
        if (
            stats is not None
            and stats.loss_unknown
            and not any("loss" in item.lower() for item in warnings)
        ):
            warnings.append(
                "Loss unknown in selected samples; reconstructed time and integrals incomplete"
            )
        if stats is not None and stats.gap_count:
            warnings.append("Gap markers in selected samples; missing intervals are not filled")
        self.warning_label.setText("\n".join(warnings))
        self.warning_label.setVisible(bool(warnings))
