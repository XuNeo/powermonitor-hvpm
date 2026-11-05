"""Hardware-free acceptance tests for the terminal sampler migrated from fc0b368."""

from __future__ import annotations

import io
import math
import os
import signal
import subprocess
import sys
from collections import deque

import numpy as np
import pytest

from powermonitor import cli, live
from powermonitor.backends.base import AcquisitionCancelled
from powermonitor.models import SAMPLE_DTYPE, SAMPLE_PERIOD_S, DeviceInfo, Quality, SampleBatch


def batch(currents, flags=None):
    records = np.zeros(len(currents), dtype=SAMPLE_DTYPE)
    records["index"] = np.arange(len(currents))
    records["time_s"] = np.arange(len(currents)) * SAMPLE_PERIOD_S
    records["current_a"] = currents
    if flags is not None:
        records["flags"] = flags
    return SampleBatch(records)


class FakeBackend:
    def __init__(self, batches=(), *, failure=None):
        self.batches = batches
        self.failure = failure
        self.calls = []

    def connect(self, serial=None):
        self.calls.append(("connect", serial))
        if self.failure == "connect":
            raise OSError("connect failed")
        return DeviceInfo(serial="TEST")

    def sample_batches(self, cancel):
        self.cancel = cancel
        self.calls.append("start")
        try:
            if self.failure == "sample":
                raise OSError("sampling failed")
            yield from self.batches
        finally:
            self.calls.append("close_stream")
            if self.failure == "close":
                raise OSError("stream close failed")

    def stop_sampling(self):
        self.calls.append("stop")
        if self.failure == "stop":
            raise OSError("STOP failed")

    def disconnect(self):
        self.calls.append("disconnect")
        if self.failure == "disconnect":
            raise OSError("disconnect failed")

    def set_voltage(self, value):
        pytest.fail("Live sampling must not set output voltage")

    def disable_output(self):
        pytest.fail("Live sampling must not switch output")


@pytest.fixture
def clock(monkeypatch):
    now = iter(range(1000))
    monkeypatch.setattr(live.time, "monotonic", lambda: next(now))


def test_latest_mean_peak_filter_invalid_and_device_error(clock, capsys):
    backend = FakeBackend(
        [
            batch(
                [0.001, 0.002, 0.003, math.nan, 9, 10],
                [0, 0, Quality.LOSS_UNKNOWN, 0, Quality.INVALID, Quality.DEVICE_ERROR],
            )
        ]
    )
    assert live.monitor(backend, "TEST", 0.2, None, True) == 0
    output = capsys.readouterr().out
    assert "I     3.000 mA" in output
    assert "avg     2.000" in output
    assert "peak     3.000 mA" in output
    assert "sample quality warning" in output
    assert backend.calls == [("connect", "TEST"), "start", "close_stream", "stop", "disconnect"]
    assert backend.cancel.is_set()


def test_refresh_statistics_are_windowed_and_warning_is_sticky(clock, capsys):
    backend = FakeBackend(
        [
            batch([0.005], [Quality.GAP_BEFORE]),
            batch([0.001, 0.003]),
        ]
    )
    assert live.monitor(backend, None, 0.2, None, True) == 0
    lines = [line for line in capsys.readouterr().out.splitlines() if "  I " in line]
    assert len(lines) == 2
    assert "avg     2.000" in lines[1]
    assert "peak     3.000" in lines[1]
    assert all("sample quality warning" in line for line in lines)


def test_no_valid_window_does_not_repeat_stale_current(clock, capsys):
    backend = FakeBackend([batch([0.001]), batch([math.inf]), SampleBatch.empty()])
    assert live.monitor(backend, None, 0.2, None, False) == 0
    lines = capsys.readouterr().out.splitlines()
    assert sum("no valid current samples" in line for line in lines) == 2
    assert sum("  I " in line for line in lines) == 1
    assert all("sample quality warning" in line for line in lines[-2:])
    assert "\033" not in "\n".join(lines)


@pytest.mark.parametrize("failure", ["connect", "sample", "close", "stop", "disconnect"])
def test_failure_cleanup_is_ordered_and_stop_failure_never_disposes(failure, clock, capsys):
    backend = FakeBackend([batch([0.001]), batch([0.002])], failure=failure)
    handlers = {sig: signal.getsignal(sig) for sig in (signal.SIGINT, signal.SIGTERM)}
    assert live.monitor(backend, None, 0.2, 0.5, True) == 1
    assert capsys.readouterr().err
    assert {sig: signal.getsignal(sig) for sig in handlers} == handlers
    if failure == "connect":
        assert backend.calls == [("connect", None), "disconnect"]
    elif failure == "stop":
        assert backend.calls[-2:] == ["close_stream", "stop"]
        assert "disconnect" not in backend.calls
    else:
        assert backend.calls[-3:] == ["close_stream", "stop", "disconnect"]


@pytest.mark.parametrize("sig", [signal.SIGINT, signal.SIGTERM])
def test_signal_cancels_stream_and_restores_handlers(sig, clock):
    class Signalled(FakeBackend):
        def sample_batches(self, cancel):
            signal.getsignal(sig)(sig, None)
            yield from super().sample_batches(cancel)

    before = signal.getsignal(sig)
    backend = Signalled([batch([0.001]), batch([0.002])])
    assert live.monitor(backend, None, 0.2, None, True) == 0
    assert backend.cancel.is_set()
    assert backend.calls[-3:] == ["close_stream", "stop", "disconnect"]
    assert signal.getsignal(sig) == before


def test_cancel_during_calibration_is_success_and_stops_before_disposal():
    class Calibrating(FakeBackend):
        def sample_batches(self, cancel):
            self.cancel = cancel
            assert cancel.wait(1), "Duration must cancel calibration, not wait for the first batch"
            raise AcquisitionCancelled("Calibration cancelled")
            yield  # Make this a generator, like the real backend.

    backend = Calibrating()
    assert live.monitor(backend, None, 0.2, 0.02, True) == 0
    assert backend.calls[-2:] == ["stop", "disconnect"]


def test_unsolicited_acquisition_cancel_is_failure(capsys):
    class Broken(FakeBackend):
        def sample_batches(self, cancel):
            raise AcquisitionCancelled("Unexpected cancellation")
            yield

    backend = Broken()
    assert live.monitor(backend, None, 0.2, None, True) == 1
    assert "Unexpected cancellation" in capsys.readouterr().err
    assert backend.calls[-2:] == ["stop", "disconnect"]


class Terminal(io.StringIO):
    def isatty(self):
        return True


@pytest.mark.parametrize("plain", [False, True])
def test_terminal_screen_and_numeric_only_output(plain, clock, monkeypatch):
    output = Terminal()
    monkeypatch.setattr(live.sys, "stdout", output)
    monkeypatch.setattr(live.shutil, "get_terminal_size", lambda: os.terminal_size((80, 24)))
    backend = FakeBackend([batch([0.001], [Quality.GAP_BEFORE])])
    assert live.monitor(backend, None, 0.2, None, plain) == 0
    text = output.getvalue()
    assert "sample quality warning" in text
    assert ("Current (mA), auto-scaled" in text) == (not plain)
    assert ("\033[?1049h" in text) == (not plain)
    assert ("\033[?1049l" in text) == (not plain)


def test_terminal_resize_does_not_hide_quality_warning(clock, monkeypatch):
    output = Terminal()
    monkeypatch.setattr(live.sys, "stdout", output)
    sizes = iter([os.terminal_size((80, 24)), os.terminal_size((35, 10))])
    monkeypatch.setattr(live.shutil, "get_terminal_size", lambda: next(sizes))
    backend = FakeBackend([batch([0.001], [Quality.GAP_BEFORE]), batch([0.002])])
    assert live.monitor(backend, None, 0.2, None, False) == 0
    text = output.getvalue()
    assert text.count("\033[?1049h") == text.count("\033[?1049l") == 1
    assert text.count("sample quality warning") == 2


def test_closed_stdout_and_stderr_do_not_skip_cleanup(clock, monkeypatch):
    class Closed(Terminal):
        def write(self, value):
            raise BrokenPipeError("terminal closed")

    monkeypatch.setattr(live.sys, "stdout", Closed())
    monkeypatch.setattr(live.sys, "stderr", Closed())
    backend = FakeBackend()
    assert live.monitor(backend, None, 0.2, None, False) == 1
    assert backend.calls[-1] == "disconnect"


def test_terminal_closed_during_sampling_still_stops(clock, monkeypatch):
    class ClosesLater(Terminal):
        def write(self, value):
            if "  I " in value:
                raise BrokenPipeError("terminal closed")
            return super().write(value)

    monkeypatch.setattr(live.sys, "stdout", ClosesLater())
    backend = FakeBackend([batch([0.001])])
    assert live.monitor(backend, None, 0.2, None, False) == 1
    assert backend.calls[-3:] == ["close_stream", "stop", "disconnect"]


def test_waveform_axes_right_alignment_constant_and_gaps():
    constant = live.waveform(deque([(0, 0.0), (1, 0.0)]), 40, 12)
    assert constant[0] == "Current (mA), auto-scaled"
    assert constant[-1] == "Time: 0.0s -> 1.0s (latest)"
    assert any(line.endswith("**") for line in constant)
    gapped = live.waveform(deque([(0, 1.0), (1, math.nan), (2, -1.0)]), 40, 12)
    assert sum(line.count("*") for line in gapped) == 2
    assert all("|" not in line.split("|", 1)[-1] for line in gapped[1:-2])
    assert live.waveform(deque(), 80, 24) == ["Waveform: waiting for valid samples"]
    assert live.waveform(deque([(0, math.nan)]), 80, 24) == ["Waveform: waiting for valid samples"]
    assert live.trend(deque([0.0, 1.0])) == "[.@] 0.000..1.000 mA"
    assert live.trend(deque([0.0, 0.0])) == "[--] 0.000..0.000 mA"


def test_waveform_keeps_only_visible_recent_points():
    lines = live.waveform(deque((float(i), float(i)) for i in range(150)), 80, 24)
    assert lines[-1] == "Time: 84.0s -> 149.0s (latest)"
    assert len(lines) == 13


@pytest.mark.parametrize("option", ["--interval", "--seconds"])
@pytest.mark.parametrize("value", ["0", "-1", "nan", "inf"])
def test_invalid_arguments_fail_before_backend_access(option, value, monkeypatch):
    monkeypatch.setattr(live, "make_backend", lambda _: pytest.fail("must not access hardware"))
    with pytest.raises(SystemExit) as exc:
        live.main([f"{option}={value}"])
    assert exc.value.code == 2


def test_missing_hwlock_fails_without_hardware_access(monkeypatch, capsys):
    monkeypatch.setattr(live.shutil, "which", lambda _: None)
    monkeypatch.setattr(live, "make_backend", lambda _: pytest.fail("must not access hardware"))
    assert live.main([]) == 1
    assert "requires hwlock" in capsys.readouterr().err


def test_lock_wrapper_forwards_module_arguments_and_restores_signals(monkeypatch, capsys):
    monkeypatch.setattr(live.shutil, "which", lambda _: "/usr/bin/hwlock")
    calls = []
    before = {sig: signal.getsignal(sig) for sig in (signal.SIGINT, signal.SIGTERM)}

    def execv(path, args):
        calls.append((path, args))
        assert all(signal.getsignal(sig) == signal.SIG_IGN for sig in before)
        raise OSError("exec failed")

    monkeypatch.setattr(live.os, "execv", execv)
    monkeypatch.setattr(live, "make_backend", lambda _: pytest.fail("must not connect unlocked"))
    assert live.main(["--serial", "123", "--plain"]) == 1
    assert "exec failed" in capsys.readouterr().err
    args = calls[0][1]
    assert args[2:4] == ["--resource", "monsoon-hvpm"]
    assert args[-8:] == [
        sys.executable,
        "-I",
        "-m",
        "powermonitor.live",
        "--serial",
        "123",
        "--plain",
        "--locked",
    ]
    assert {sig: signal.getsignal(sig) for sig in before} == before


def test_missing_libusb_fails_before_connect(monkeypatch):
    monkeypatch.setattr("usb.backend.libusb1.get_backend", lambda: None)
    with pytest.raises(live.BackendError, match="libusb"):
        live.make_backend("monsoon")


def test_simulator_does_not_load_monsoon_or_switch_output():
    backend = live.make_backend("simulator")
    assert not backend.output_enabled
    assert live.monitor(backend, None, 0.01, 0.06, True) == 0
    assert not backend.connected
    assert not backend.output_enabled


def test_cli_dispatches_live_without_desktop_arguments(monkeypatch):
    calls = []
    monkeypatch.setattr(live, "main", lambda args: calls.append(args) or 0)
    assert cli.main(["live", "--plain"]) == 0
    assert calls == [["--plain"]]


def test_cli_preserves_desktop_dispatch(monkeypatch):
    from powermonitor import app

    calls = []
    monkeypatch.setattr(app, "main", lambda args: calls.append(args) or 0)
    assert cli.main(["--open", "example.pmcap"]) == 0
    assert calls == [["--open", "example.pmcap"]]


@pytest.mark.parametrize("entry", [["-m", "powermonitor"], ["-m", "powermonitor.live"]])
def test_installed_cli_runs_headlessly_without_recordings(entry, tmp_path):
    args = ["live"] if entry[-1] == "powermonitor" else []
    # Importing the live path must not load Qt or any capture writer.
    source = (
        "import sys; from powermonitor.cli import main; "
        "assert main(['live', '--backend', 'simulator', '--seconds', '0.01']) == 0; "
        "assert not any(m.startswith('PySide6') for m in sys.modules); "
        "assert 'powermonitor.storage' not in sys.modules"
    )
    result = subprocess.run(
        [sys.executable, "-I", "-c", source],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr
    result = subprocess.run(
        [
            sys.executable,
            "-I",
            *entry,
            *args,
            "--backend",
            "simulator",
            "--seconds",
            "0.06",
            "--interval",
            "0.01",
            "--plain",
        ],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr
    assert "I     0.000 mA" in result.stdout
    assert "\033" not in result.stdout
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("sig", [signal.SIGINT, signal.SIGTERM])
def test_subprocess_signal_restores_terminal_screen(sig, tmp_path):
    import fcntl
    import pty
    import select
    import struct
    import termios
    import time

    master, slave = pty.openpty()
    fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 24, 80, 0, 0))
    process = subprocess.Popen(
        [
            sys.executable,
            "-I",
            "-m",
            "powermonitor",
            "live",
            "--backend",
            "simulator",
            "--interval",
            "0.01",
        ],
        cwd=tmp_path,
        stdin=slave,
        stdout=slave,
        stderr=slave,
    )
    os.close(slave)
    output = b""
    try:
        deadline = time.monotonic() + 5
        while b"\033[?1049h" not in output and time.monotonic() < deadline:
            assert process.poll() is None, output.decode(errors="replace")
            ready, _, _ = select.select([master], [], [], 0.1)
            if ready:
                output += os.read(master, 4096)
        assert b"\033[?1049h" in output
        process.send_signal(sig)
        assert process.wait(timeout=5) == 0
        while select.select([master], [], [], 0)[0]:
            try:
                data = os.read(master, 4096)
            except OSError:
                break
            if not data:
                break
            output += data
        assert b"\033[?1049l" in output
        assert b"Traceback" not in output
        assert list(tmp_path.iterdir()) == []
    finally:
        if process.poll() is None:
            process.kill()
        process.wait(timeout=5)
        os.close(master)


def test_console_command_is_installed(tmp_path):
    from pathlib import Path

    command = Path(sys.executable).with_name("powermonitor")
    result = subprocess.run(
        [str(command), "live", "--backend", "simulator", "--seconds", "0.01", "--plain"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr
    assert "output unchanged" in result.stdout
    assert list(tmp_path.iterdir()) == []
