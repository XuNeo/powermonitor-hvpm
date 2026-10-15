"""Application entry point for the Monsoon HVPM desktop UI."""

from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path
from typing import Protocol

from PySide6 import QtCore, QtWidgets

from powermonitor.ui.window import MainWindow


class _Application(Protocol):
    def quit(self) -> None: ...

    def exit(self, retcode: int | None = 0) -> None: ...

    def processEvents(self) -> None: ...  # noqa: N802 - Qt API name


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Monsoon HVPM desktop capture and replay UI")
    parser.add_argument(
        "--demo",
        action="store_true",
        default=True,
        help="keep safe simulator as the default backend; never auto-connects or enables output",
    )
    parser.add_argument("--open", type=Path, default=None, help="open an existing .pmcap directory")
    parser.add_argument(
        "--screenshot",
        type=Path,
        default=None,
        help="save a startup screenshot and exit",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    QtCore.QCoreApplication.setOrganizationName("powermonitor")
    QtCore.QCoreApplication.setApplicationName("powermonitor")
    app = QtWidgets.QApplication.instance()
    owns_app = app is None
    if app is None:
        app = QtWidgets.QApplication(sys.argv[:1])
    window = MainWindow()
    window.show()
    if args.open is not None:
        try:
            window.open_capture(args.open)
        except Exception as exc:
            message = f"Open replay failed: {exc}"
            print(message, file=sys.stderr)
            if not _is_headless_platform():
                QtWidgets.QMessageBox.critical(window, "Open replay failed", str(exc))
            window.controller.shutdown(timeout=3.0)
            window.close()
            return 2
    elif args.screenshot is not None:
        window.load_demo_records()
    if args.screenshot is not None:
        app.processEvents()
        QtCore.QTimer.singleShot(250, lambda: _finish_screenshot(window, args.screenshot, app))
        return app.exec()
    if owns_app:
        return app.exec()
    return 0


def _is_headless_platform() -> bool:
    return os.environ.get("QT_QPA_PLATFORM", "").lower() in {"offscreen", "minimal"}


def _finish_screenshot(
    window: MainWindow,
    destination: Path,
    app: _Application,
    *,
    deadline_s: float = 30.0,
) -> None:
    deadline = time.monotonic() + deadline_s
    timed_out = False
    while not window._wait_for_background_tasks():
        if not timed_out and time.monotonic() >= deadline:
            timed_out = True
            print(
                "Screenshot failed: background statistics did not finish before deadline",
                file=sys.stderr,
            )
        app.processEvents()
        time.sleep(0.01)
    if timed_out:
        _shutdown_and_exit(window, app, 2)
    else:
        _grab_and_quit(window, destination, app)


def _grab_and_quit(window: MainWindow, destination: Path, app: _Application) -> None:
    return_code = 0
    try:
        destination = Path(destination)
        destination.parent.mkdir(parents=True, exist_ok=True)
        encoded = QtCore.QByteArray()
        buffer = QtCore.QBuffer(encoded)
        if not buffer.open(QtCore.QIODevice.OpenModeFlag.WriteOnly):
            raise RuntimeError("Unable to allocate screenshot buffer")
        try:
            if not window.grab().save(buffer, "PNG"):
                raise RuntimeError("Unable to encode screenshot as PNG")
        finally:
            buffer.close()
        with destination.open("xb") as stream:
            stream.write(encoded.data())
    except Exception as exc:
        return_code = 2
        print(f"Screenshot failed: {exc}", file=sys.stderr)
    _shutdown_and_exit(window, app, return_code)


def _shutdown_and_exit(window: MainWindow, app: _Application, return_code: int) -> None:
    try:
        if not window.controller.shutdown(timeout=3.0):
            return_code = 2
            print("Shutdown failed: controller worker did not stop", file=sys.stderr)
    except Exception as exc:
        return_code = 2
        print(f"Shutdown failed: {exc}", file=sys.stderr)
    app.exit(return_code)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
