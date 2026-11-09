"""Command-line acceptance: explicit control, headless recordings, offline reuse."""

import json
import subprocess
import sys

import numpy as np
import pytest

from powermonitor import commands
from powermonitor.backends.base import AcquisitionCancelled, BackendError
from powermonitor.backends.simulator import SimulatorBackend
from powermonitor.models import SAMPLE_DTYPE, SAMPLE_PERIOD_S, DeviceInfo, SampleBatch
from powermonitor.storage import CaptureReader, CaptureWriter


class DeviceBackend(SimulatorBackend):
    def __init__(self, devices=("TEST",), *, fail=None):
        super().__init__()
        self.devices = devices
        self.calls = []
        self.fail = fail

    def scan(self):
        self.calls.append("scan")
        return [DeviceInfo(serial=serial) for serial in self.devices]

    def connect(self, serial=None):
        self.calls.append(("connect", serial))
        if self.fail == "connect":
            raise BackendError("connect failed")
        self.connected = True
        return DeviceInfo(serial=serial)

    def disconnect(self):
        self.calls.append("disconnect")
        if self.fail == "disconnect":
            raise BackendError("cleanup failed")
        # Fake hardware preserves power on disconnect, unlike a simulator.
        self.connected = False

    def set_voltage(self, voltage):
        self.calls.append(("voltage", voltage))
        self.output_enabled = True

    def disable_output(self):
        self.calls.append("off")
        self.output_enabled = False

    def stop_sampling(self):
        self.calls.append("stop")
        if self.fail == "stop":
            raise BackendError("STOP failed")


@pytest.fixture
def backend(monkeypatch):
    device = DeviceBackend()
    monkeypatch.setattr(commands, "make_backend", lambda *args, **kwargs: device)
    return device


def test_scan_never_connects_samples_or_controls_power(backend, capsys):
    assert commands.main(["scan", "--backend", "simulator", "--json"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["devices"][0]["serial"] == "TEST"
    assert backend.calls == ["scan"]


@pytest.mark.parametrize(
    "devices, expected",
    [
        ((), "No devices found.\n"),
        (("36612",), "Found 1 device:\n  36612  Monsoon HVPM (monsoon)\n"),
        (
            ("36612", "36613"),
            "Found 2 devices:\n"
            "  36612  Monsoon HVPM (monsoon)\n"
            "  36613  Monsoon HVPM (monsoon)\n",
        ),
    ],
)
def test_scan_default_output_is_readable_device_list(devices, expected, backend, capsys):
    backend.devices = devices
    assert commands.main(["scan", "--backend", "simulator"]) == 0
    output = capsys.readouterr()
    assert output.out == expected
    assert output.err == ""
    assert backend.calls == ["scan"]


@pytest.mark.parametrize("devices", [(), ("36612",), ("36612", "36613")])
def test_scan_json_contract_is_unchanged(devices, backend, capsys):
    backend.devices = devices
    assert commands.main(["scan", "--backend", "simulator", "--json"]) == 0
    assert json.loads(capsys.readouterr().out) == {
        "devices": [
            {
                "serial": serial,
                "name": "Monsoon HVPM",
                "firmware": "unknown",
                "backend": "monsoon",
            }
            for serial in devices
        ]
    }
    assert backend.calls == ["scan"]


@pytest.mark.parametrize("devices", [(), ("FIRST", "SECOND")])
def test_ambiguous_device_never_connects_or_powers(devices, backend, capsys):
    backend.devices = devices
    assert (
        commands.main(["output", "on", "--voltage", "3.30", "--yes", "--backend", "simulator"]) == 1
    )
    assert "Exactly one device" in capsys.readouterr().err
    assert all(not isinstance(call, tuple) for call in backend.calls)
    assert not backend.output_enabled


def test_serial_selection_is_explicit(backend):
    backend.devices = ("FIRST", "SECOND")
    assert (
        commands.main(
            [
                "output",
                "on",
                "--serial",
                "SECOND",
                "--voltage",
                "3.30",
                "--yes",
                "--backend",
                "simulator",
            ]
        )
        == 0
    )
    assert backend.calls == ["scan", ("connect", "SECOND"), ("voltage", 3.3), "disconnect"]
    assert backend.output_enabled


def test_absent_serial_fails_before_power(backend):
    assert (
        commands.main(["output", "off", "--serial", "MISSING", "--yes", "--backend", "simulator"])
        == 1
    )
    assert "off" not in backend.calls


@pytest.mark.parametrize("action", ["on", "off"])
def test_noninteractive_power_requires_yes(action, backend, monkeypatch, capsys):
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
    args = ["output", action, "--backend", "simulator"]
    if action == "on":
        args += ["--voltage", "3.3"]
    assert commands.main(args) == 1
    assert "requires --yes" in capsys.readouterr().err
    assert "off" not in backend.calls
    assert not any(isinstance(call, tuple) and call[0] == "voltage" for call in backend.calls)


@pytest.mark.parametrize("answer,code", [("ON 3.30\n", 0), ("yes\n", 1)])
def test_interactive_voltage_confirmation(answer, code, backend, monkeypatch, capsys):
    import io

    stream = io.StringIO(answer)
    stream.isatty = lambda: True
    monkeypatch.setattr(sys, "stdin", stream)
    assert (
        commands.main(["output", "on", "--voltage", "3.3", "--backend", "simulator", "--json"])
        == code
    )
    result = capsys.readouterr()
    assert "type ON 3.30" in result.err
    if code == 0:
        assert json.loads(result.out)["configured_voltage_v"] == 3.3
    assert backend.output_enabled == (code == 0)


@pytest.mark.parametrize("value", ["nan", "inf", "0", "13.51", "3.301"])
def test_invalid_voltage_rejected_before_usb(value, backend):
    with pytest.raises(SystemExit) as exc:
        commands.main(["output", "on", "--voltage", value, "--yes"])
    assert exc.value.code == 2
    assert backend.calls == []


def test_voltage_required_no_default(backend):
    with pytest.raises(SystemExit) as exc:
        commands.main(["output", "on", "--yes"])
    assert exc.value.code == 2
    assert backend.calls == []


def test_power_off_explicit_and_no_sampling(backend, capsys):
    backend.output_enabled = True
    assert commands.main(["output", "off", "--yes", "--backend", "simulator", "--json"]) == 0
    assert backend.calls == ["scan", ("connect", "TEST"), "off", "disconnect"]
    assert not backend.output_enabled
    assert json.loads(capsys.readouterr().out)["configured_voltage_v"] == 0


def test_partial_connect_failure_is_cleaned(backend):
    backend.fail = "connect"
    assert commands.main(["status", "--backend", "simulator"]) == 1
    assert backend.calls[-1] == "disconnect"


def test_record_writer_failure_still_disconnects(backend, monkeypatch, tmp_path):
    monkeypatch.setattr(
        "powermonitor.storage.CaptureWriter",
        lambda *args: (_ for _ in ()).throw(OSError("writer failed")),
    )
    assert (
        commands.main(
            [
                "capture",
                "--seconds",
                "0.02",
                "--out",
                str(tmp_path / "a.pmcap"),
                "--backend",
                "simulator",
            ]
        )
        == 1
    )
    assert backend.calls[-1] == "disconnect"
    assert "stop" not in backend.calls


@pytest.mark.parametrize("failure", ["stop", "disconnect"])
def test_record_cleanup_failure_retains_data_incomplete(failure, backend, tmp_path, capsys):
    backend.fail = failure
    capture = tmp_path / "failure.pmcap"
    assert (
        commands.main(
            [
                "capture",
                "--seconds",
                "0.06",
                "--out",
                str(capture),
                "--backend",
                "simulator",
                "--json",
            ]
        )
        == 4
    )
    result = json.loads(capsys.readouterr().out)
    assert result["complete"] is False
    assert result["errors"]
    reader = CaptureReader(capture)
    assert not reader.metadata["complete"]
    assert len(reader.records) > 0
    if failure == "stop":
        assert backend.calls[-1] == "stop"
        assert "disconnect" not in backend.calls
    else:
        assert backend.calls[-2:] == ["stop", "disconnect"]
    assert "off" not in backend.calls


def test_calibration_duration_is_cancellable_and_no_samples_is_incomplete(backend, tmp_path):
    def calibrate(cancel):
        assert cancel.wait(1)
        raise AcquisitionCancelled("calibration cancelled")
        yield

    backend.sample_batches = calibrate
    capture = tmp_path / "cal.pmcap"
    assert (
        commands.main(
            ["capture", "--seconds", "0.02", "--out", str(capture), "--backend", "simulator"]
        )
        == 4
    )
    assert not CaptureReader(capture).metadata["complete"]
    assert backend.calls[-2:] == ["stop", "disconnect"]


@pytest.mark.parametrize("value", ["0", "nan", "inf", "-1"])
def test_bad_duration_rejected_before_usb(value, backend, tmp_path):
    with pytest.raises(SystemExit) as exc:
        commands.main(["capture", "--seconds", value, "--out", str(tmp_path / "a.pmcap")])
    assert exc.value.code == 2
    assert backend.calls == []


def test_existing_recording_never_overwritten_before_usb(backend, tmp_path):
    capture = tmp_path / "existing.pmcap"
    capture.mkdir()
    with pytest.raises(SystemExit) as exc:
        commands.main(["capture", "--seconds", "1", "--out", str(capture)])
    assert exc.value.code == 2
    assert backend.calls == []


@pytest.fixture
def recording(tmp_path):
    path = tmp_path / "input.pmcap"
    records = np.zeros(3, dtype=SAMPLE_DTYPE)
    records["index"] = np.arange(3)
    records["time_s"] = np.arange(3) * SAMPLE_PERIOD_S
    records["current_a"] = [0.001, 0.002, 0.003]
    records["voltage_v"] = 3.3
    with CaptureWriter(path) as writer:
        writer.append(SampleBatch(records))
    return path


@pytest.mark.parametrize("command", ["info", "stats", "export"])
def test_offline_reuses_original_samples_without_hardware(command, recording, monkeypatch, capsys):
    monkeypatch.setattr(commands, "make_backend", lambda *a, **k: pytest.fail("offline USB access"))
    args = [command, str(recording), "--json"]
    csv = recording.parent / "samples.csv"
    if command == "export":
        args += ["--out", str(csv)]
    assert commands.main(args) == 0
    result = json.loads(capsys.readouterr().out)
    if command == "info":
        assert result["sample_count"] == 3
        assert result["metadata"]["complete"]
    elif command == "stats":
        assert result["mean_current_a"] == pytest.approx(0.002)
        assert result["mean_voltage_v"] == pytest.approx(3.3)
        assert result["mean_power_w"] == pytest.approx(0.0066)
        assert result["charge_c"] == pytest.approx(0.002 * 3 * SAMPLE_PERIOD_S)
        assert result["coverage"] == pytest.approx(1)
    else:
        assert len(csv.read_text().splitlines()) == 4


def test_stats_selection_and_csv_overlap(recording, capsys):
    assert commands.main(["stats", str(recording), "--window", "0.0002", "0.0004", "--json"]) == 0
    stats = json.loads(capsys.readouterr().out)
    assert stats["sample_count"] == 1
    assert stats["mean_current_a"] == pytest.approx(0.002)
    csv = recording.parent / "selection.csv"
    assert (
        commands.main(["export", str(recording), "--window", "0.0002", "0.0004", "--out", str(csv)])
        == 0
    )
    assert len(csv.read_text().splitlines()) == 2


def test_export_refuses_overwrite(recording):
    csv = recording.parent / "existing.csv"
    csv.write_text("KEEP")
    assert commands.main(["export", str(recording), "--out", str(csv)]) == 1
    assert csv.read_text() == "KEEP"


def test_incomplete_info_reports_warnings(recording, capsys):
    metadata_path = recording / "metadata.json"
    metadata = json.loads(metadata_path.read_text())
    metadata["complete"] = False
    metadata_path.write_text(json.dumps(metadata))
    assert commands.main(["info", str(recording), "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["warnings"]


def test_window_rejected_before_reading(recording):
    with pytest.raises(SystemExit) as exc:
        commands.main(["stats", str(recording), "--window", "nan", "1"])
    assert exc.value.code == 2


def test_lock_is_held_across_power_command_and_disconnect(backend, monkeypatch):
    class Lease:
        def __enter__(self):
            backend.calls.append("lock")

        def __exit__(self, *args):
            backend.calls.append("unlock")

    monkeypatch.setattr(commands, "HardwareLease", Lease)
    assert commands.main(["output", "on", "--voltage", "3.3", "--yes"]) == 0
    assert backend.calls == [
        "lock",
        "scan",
        ("connect", "TEST"),
        ("voltage", 3.3),
        "disconnect",
        "unlock",
    ]


def test_device_busy_has_distinct_exit_without_usb(monkeypatch):
    class BusyLease:
        def __enter__(self):
            raise commands.DeviceBusy("device busy")

        def __exit__(self, *args):
            pass

    monkeypatch.setattr(commands, "HardwareLease", BusyLease)
    monkeypatch.setattr(commands, "make_backend", lambda *a, **k: pytest.fail("USB while busy"))
    assert commands.main(["status"]) == 3


def run_cli(args, tmp_path):
    return subprocess.run(
        [sys.executable, "-I", "-m", "powermonitor", *args],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=10,
    )


def test_installed_headless_workflow_capture_stats_export(tmp_path):
    capture = tmp_path / "boot.pmcap"
    csv = tmp_path / "boot.csv"
    result = run_cli(
        ["capture", "--backend", "simulator", "--seconds", "0.06", "--out", str(capture), "--json"],
        tmp_path,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["complete"]
    for args in (
        ["info", str(capture), "--json"],
        ["stats", str(capture), "--json"],
        ["export", str(capture), "--out", str(csv), "--json"],
    ):
        result = run_cli(args, tmp_path)
        assert result.returncode == 0, result.stderr
        json.loads(result.stdout)
    assert csv.exists()
    assert CaptureReader(capture).metadata["power_control"] == "unchanged"
    assert np.all(CaptureReader(capture).records["current_a"] == 0)


def test_cli_no_qt_for_all_commands(tmp_path):
    code = (
        "import sys; from powermonitor.cli import main; "
        "assert main(['status', '--backend', 'simulator', '--json']) == 0; "
        "assert not any(name.startswith('PySide6') for name in sys.modules)"
    )
    result = subprocess.run(
        [sys.executable, "-I", "-c", code], cwd=tmp_path, capture_output=True, text=True, timeout=10
    )
    assert result.returncode == 0, result.stderr


def test_live_json_is_valid_json_lines(tmp_path):
    result = run_cli(["live", "--backend", "simulator", "--seconds", "0.06", "--json"], tmp_path)
    assert result.returncode == 0, result.stderr
    rows = [json.loads(line) for line in result.stdout.splitlines()]
    assert rows
    assert rows[0]["current_a"] == 0
    assert "output unchanged" in result.stderr
    assert list(tmp_path.iterdir()) == []


def test_sigint_capture_preserves_partial_recording_and_stops(tmp_path):
    import os
    import signal
    import time

    capture = tmp_path / "interrupted.pmcap"
    process = subprocess.Popen(
        [
            sys.executable,
            "-I",
            "-m",
            "powermonitor",
            "capture",
            "--backend",
            "simulator",
            "--seconds",
            "30",
            "--out",
            str(capture),
            "--json",
        ],
        cwd=tmp_path,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            samples = capture / "samples.bin"
            if samples.exists() and samples.stat().st_size > 0:
                break
            assert process.poll() is None
            time.sleep(0.01)
        assert samples.exists() and samples.stat().st_size > 0
        os.kill(process.pid, signal.SIGINT)
        stdout, stderr = process.communicate(timeout=5)
        assert process.returncode == 0, stderr
        result = json.loads(stdout)
        assert result["interrupted"]
        assert result["complete"]
        assert CaptureReader(capture).metadata["complete"]
    finally:
        if process.poll() is None:
            process.kill()
        process.communicate(timeout=5)


def test_sdk_prints_do_not_pollute_json(backend, capsys):
    original = backend.connect

    def noisy(serial):
        print("SDK diagnostic")
        return original(serial)

    backend.connect = noisy
    assert commands.main(["status", "--backend", "simulator", "--json"]) == 0
    output = capsys.readouterr()
    assert json.loads(output.out)["serial"] == "TEST"
    assert "SDK diagnostic" in output.err


def test_unknown_command_is_usage_error_without_qt(tmp_path):
    result = run_cli(["misspelled"], tmp_path)
    assert result.returncode == 2
    assert "Unknown command" in result.stderr
    assert "Qt" not in result.stderr


def test_capture_quality_warning_is_in_json_and_metadata(backend, tmp_path, capsys):
    from powermonitor.models import Quality

    def lossy(cancel):
        records = np.zeros(1, dtype=SAMPLE_DTYPE)
        records["flags"] = int(Quality.LOSS_UNKNOWN)
        yield SampleBatch(records)
        assert cancel.wait(1)

    backend.sample_batches = lossy
    capture = tmp_path / "loss.pmcap"
    assert (
        commands.main(
            [
                "capture",
                "--seconds",
                "0.02",
                "--out",
                str(capture),
                "--backend",
                "simulator",
                "--json",
            ]
        )
        == 0
    )
    result = capsys.readouterr()
    assert json.loads(result.out)["warnings"]
    assert "loss reported" in result.err
    assert CaptureReader(capture).metadata["loss_unknown"]
