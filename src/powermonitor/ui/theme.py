"""Light and dark visual tokens for the desktop UI."""

from __future__ import annotations

from dataclasses import dataclass

from PySide6 import QtGui, QtWidgets


@dataclass(frozen=True)
class ChartTheme:
    name: str
    surface: str
    panel: str
    raised: str
    text: str
    secondary_text: str
    muted_text: str
    grid: str
    border: str
    selection: str
    current: str
    voltage: str
    power: str
    warning: str
    error: str
    good: str


LIGHT = ChartTheme(
    name="light",
    surface="#fcfcfb",
    panel="#f4f3ee",
    raised="#ffffff",
    text="#0b0b0b",
    secondary_text="#44443f",
    muted_text="#6d6b63",
    grid="#e1e0d9",
    border="#d0cec4",
    selection="#6aa6ff",
    current="#2a78d6",
    voltage="#eb6834",
    power="#1baf7a",
    warning="#9a6a00",
    error="#b3261e",
    good="#167a4b",
)

DARK = ChartTheme(
    name="dark",
    surface="#1a1a19",
    panel="#222220",
    raised="#282826",
    text="#f0efec",
    secondary_text="#d3d1cb",
    muted_text="#a8a59d",
    grid="#2c2c2a",
    border="#44433f",
    selection="#79aef7",
    current="#3987e5",
    voltage="#d95926",
    power="#199e70",
    warning="#e0ad33",
    error="#ff8a7a",
    good="#63d394",
)

THEMES = {LIGHT.name: LIGHT, DARK.name: DARK}


def apply_application_theme(app: QtWidgets.QApplication, theme: ChartTheme) -> None:
    palette = QtGui.QPalette()
    palette.setColor(QtGui.QPalette.ColorRole.Window, QtGui.QColor(theme.surface))
    palette.setColor(QtGui.QPalette.ColorRole.WindowText, QtGui.QColor(theme.text))
    palette.setColor(QtGui.QPalette.ColorRole.Base, QtGui.QColor(theme.raised))
    palette.setColor(QtGui.QPalette.ColorRole.AlternateBase, QtGui.QColor(theme.panel))
    palette.setColor(QtGui.QPalette.ColorRole.ToolTipBase, QtGui.QColor(theme.raised))
    palette.setColor(QtGui.QPalette.ColorRole.ToolTipText, QtGui.QColor(theme.text))
    palette.setColor(QtGui.QPalette.ColorRole.Text, QtGui.QColor(theme.text))
    palette.setColor(QtGui.QPalette.ColorRole.Button, QtGui.QColor(theme.panel))
    palette.setColor(QtGui.QPalette.ColorRole.ButtonText, QtGui.QColor(theme.text))
    palette.setColor(QtGui.QPalette.ColorRole.BrightText, QtGui.QColor(theme.error))
    palette.setColor(QtGui.QPalette.ColorRole.Highlight, QtGui.QColor(theme.selection))
    palette.setColor(QtGui.QPalette.ColorRole.HighlightedText, QtGui.QColor(theme.surface))
    app.setPalette(palette)
    app.setStyleSheet(
        f"""
        QMainWindow, QWidget {{
            background: {theme.surface};
            color: {theme.text};
        }}
        QGroupBox {{
            border: 1px solid {theme.border};
            border-radius: 8px;
            margin-top: 12px;
            padding: 10px 8px 8px 8px;
            font-weight: 600;
        }}
        QGroupBox::title {{
            subcontrol-origin: margin;
            left: 10px;
            padding: 0 4px;
            color: {theme.secondary_text};
        }}
        QPushButton, QComboBox, QDoubleSpinBox, QSpinBox, QLineEdit {{
            background: {theme.raised};
            color: {theme.text};
            border: 1px solid {theme.border};
            border-radius: 6px;
            padding: 4px 6px;
        }}
        QPushButton:disabled, QComboBox:disabled, QDoubleSpinBox:disabled, QLineEdit:disabled {{
            color: {theme.muted_text};
            background: {theme.panel};
        }}
        QPushButton:hover:!disabled {{
            border-color: {theme.selection};
        }}
        QTabWidget::pane {{
            border: 1px solid {theme.border};
            border-radius: 8px;
        }}
        QHeaderView::section {{
            background: {theme.panel};
            color: {theme.secondary_text};
            border: 0;
            border-right: 1px solid {theme.border};
            border-bottom: 1px solid {theme.border};
            padding: 4px;
        }}
        QTableView, QTableWidget {{
            gridline-color: {theme.grid};
            selection-background-color: {theme.selection};
            alternate-background-color: {theme.panel};
        }}
        QLabel#warningLabel {{
            color: {theme.warning};
            font-weight: 600;
        }}
        QLabel#errorLabel {{
            color: {theme.error};
            font-weight: 600;
        }}
        QLabel#channelTitle {{
            font-weight: 700;
        }}
        QLabel#summaryHeading {{
            color: {theme.secondary_text};
            font-size: 11px;
        }}
        QPushButton#offButton {{
            color: {theme.error};
            font-weight: 700;
            border-color: {theme.error};
        }}
        """
    )
