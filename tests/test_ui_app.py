# ruff: noqa: E402,I001
from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("QT_API", "pyside6")

from powermonitor import app as app_module


class FakeWindow:
    def __init__(self, save_result=True):
        self.shutdown_called = False
        self.save_result = save_result

    def grab(self):
        owner = self

        class Pixmap:
            def save(self, destination, image_format=None) -> bool:
                if owner.save_result:
                    destination.write(b"fake-png")
                return owner.save_result

        return Pixmap()

    def _wait_for_background_tasks(self):
        return not any(thread.isRunning() for thread in getattr(self, "_threads", ()))

    @property
    def controller(self):
        class Controller:
            def __init__(self, owner):
                self.owner = owner

            def shutdown(self, timeout=3.0):
                self.owner.shutdown_called = True
                return True

        return Controller(self)


class FakeApp:
    def __init__(self):
        self.exit_code = None
        self.process_events_count = 0

    def quit(self):
        self.exit(0)

    def exit(self, return_code=0):
        self.exit_code = return_code

    def processEvents(self):  # noqa: N802 - Qt API name
        self.process_events_count += 1


def test_default_mode_is_safe_demo():
    args = app_module.build_parser().parse_args([])
    assert args.demo is True
    assert args.open is None
    assert args.screenshot is None


def test_screenshot_helper_saves_and_shuts_down(tmp_path):
    window = FakeWindow()
    app = FakeApp()
    output = tmp_path / "shot.png"
    app_module._grab_and_quit(window, output, app)
    assert output.read_bytes() == b"fake-png"
    assert window.shutdown_called
    assert app.exit_code == 0


def test_screenshot_failure_still_shuts_down_and_quits(tmp_path, capsys):
    window = FakeWindow(save_result=False)
    app = FakeApp()
    app_module._grab_and_quit(window, tmp_path / "shot.png", app)
    assert "Screenshot failed" in capsys.readouterr().err
    assert window.shutdown_called
    assert app.exit_code == 2


def test_screenshot_refuses_to_overwrite_existing_file(tmp_path, capsys):
    window = FakeWindow()
    app = FakeApp()
    output = tmp_path / "shot.png"
    output.write_bytes(b"original")
    app_module._grab_and_quit(window, output, app)
    assert output.read_bytes() == b"original"
    assert "File exists" in capsys.readouterr().err
    assert window.shutdown_called
    assert app.exit_code == 2


def test_screenshot_waits_for_background_thread(tmp_path):
    class Thread:
        def __init__(self):
            self.polls = 0

        def isRunning(self):  # noqa: N802 - Qt API name
            self.polls += 1
            return self.polls < 3

    window = FakeWindow()
    window._threads = [Thread()]
    app = FakeApp()
    output = tmp_path / "shot.png"
    app_module._finish_screenshot(window, output, app, deadline_s=1.0)
    assert output.read_bytes() == b"fake-png"
    assert app.process_events_count == 2
    assert app.exit_code == 0


def test_screenshot_background_timeout_waits_for_safe_thread_exit(tmp_path, capsys, monkeypatch):
    class Thread:
        def __init__(self):
            self.polls = 0

        def isRunning(self):  # noqa: N802 - Qt API name
            self.polls += 1
            return self.polls < 4

    window = FakeWindow()
    window._threads = [Thread()]
    app = FakeApp()
    monotonic_values = iter((0.0, 2.0, 3.0, 4.0))
    monkeypatch.setattr(app_module.time, "monotonic", lambda: next(monotonic_values))
    app_module._finish_screenshot(window, tmp_path / "shot.png", app, deadline_s=1.0)
    assert "before deadline" in capsys.readouterr().err
    assert not (tmp_path / "shot.png").exists()
    assert window.shutdown_called
    assert app.process_events_count == 3
    assert app.exit_code == 2


def test_headless_open_failure_returns_without_modal(tmp_path, monkeypatch):
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")

    class BrokenWindow:
        def __init__(self):
            self.controller = FakeWindow().controller

        def show(self):
            return None

        def open_capture(self, _path):
            raise ValueError("invalid capture")

        def close(self):
            return None

    monkeypatch.setattr(app_module, "MainWindow", BrokenWindow)
    modal_calls = []
    monkeypatch.setattr(
        app_module.QtWidgets.QMessageBox,
        "critical",
        lambda *args, **kwargs: modal_calls.append(args),
    )
    assert app_module.main(["--open", str(tmp_path / "missing.pmcap")]) == 2
    assert not modal_calls
