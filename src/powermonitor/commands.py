"""Headless device control, recording and offline analysis commands."""

from __future__ import annotations

import argparse
import json
import math
import signal
import sys
import time
from contextlib import nullcontext, redirect_stdout
from dataclasses import asdict
from pathlib import Path
from threading import Event, Timer
from typing import Any

from powermonitor.backends.base import AcquisitionCancelled, Backend, BackendError
from powermonitor.hardware_lock import DeviceBusy, HardwareLease
from powermonitor.models import SDK_REVISION, DeviceInfo, validate_voltage

SUCCESS = 0
FAILURE = 1
USAGE = 2
BUSY = 3
INCOMPLETE = 4
HARDWARE_COMMANDS = {"scan", "status", "output", "capture"}


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(description="HVPM command-line control and offline analysis")
    sub = root.add_subparsers(dest="command", required=True)
    scan = sub.add_parser("scan", help="list devices without changing output")
    status = sub.add_parser("status", help="read configured settings, not measured output")
    output = sub.add_parser("output", help="explicitly change main output")
    actions = output.add_subparsers(dest="action", required=True)
    on = actions.add_parser("on", help="enable at an explicit voltage; requires OFF readback")
    on.add_argument("--voltage", required=True, type=float, help="0.80..13.50 V, 0.01 V steps")
    off = actions.add_parser("off", help="set main output to zero")
    capture = sub.add_parser("capture", help="record raw samples without changing output")
    capture.add_argument(
        "--seconds", required=True, type=float, help="duration including calibration"
    )
    capture.add_argument("--out", required=True, type=Path, help="new .pmcap directory")
    for command in (scan, status, on, off, capture):
        command.add_argument("--backend", choices=("monsoon", "simulator"), default="monsoon")
        command.add_argument("--serial", default=None)
        command.add_argument(
            "--json", action="store_true", help="machine-readable result on stdout"
        )
    for command in (on, off):
        # Accept older scripts without requiring or advertising a confirmation flag.
        command.add_argument("--yes", action="store_true", help=argparse.SUPPRESS)
    info = sub.add_parser("info", help="read recording metadata and data-quality warnings")
    stats = sub.add_parser("stats", help="compute original-sample window statistics")
    export = sub.add_parser("export", help="export original samples to a new CSV")
    for command in (info, stats, export):
        command.add_argument("capture", type=Path)
        command.add_argument("--json", action="store_true")
    for command in (stats, export):
        command.add_argument("--window", nargs=2, type=float, metavar=("START", "END"))
    export.add_argument("--format", choices=("csv",), default="csv")
    export.add_argument("--out", required=True, type=Path)
    return root


def emit(result: dict[str, Any], machine: bool) -> None:
    if machine:
        print(json.dumps(result, ensure_ascii=False, allow_nan=False))
    elif "devices" in result:
        devices = result["devices"]
        if not devices:
            print("No devices found.")
            return
        print(f"Found {len(devices)} {'device' if len(devices) == 1 else 'devices'}:")
        for device in devices:
            print(f"  {device['serial']}  {device['name']} ({device['backend']})")
    else:
        units = {
            "current_a": (1000, "mA"),
            "mean_current_a": (1000, "mA"),
            "min_current_a": (1000, "mA"),
            "max_current_a": (1000, "mA"),
            "mean_voltage_v": (1, "V"),
            "configured_voltage_v": (1, "V"),
            "mean_power_w": (1000, "mW"),
            "charge_c": (1 / 3.6, "mAh"),
            "energy_j": (1 / 3.6, "mWh"),
        }
        for key, value in result.items():
            if key in units and isinstance(value, (int, float)):
                factor, unit = units[key]
                print(f"{key}: {value * factor:.6g} {unit}")
                continue
            if isinstance(value, (dict, list)):
                value = json.dumps(value, ensure_ascii=False, allow_nan=False)
            print(f"{key}: {value}")


def make_backend(name: str, *, control: bool = False) -> Backend:
    if name == "simulator":
        from powermonitor.backends.simulator import SimulatorBackend

        return SimulatorBackend()
    from usb.backend import libusb1

    if libusb1.get_backend() is None:
        raise BackendError("System libusb is required for HVPM access")
    if control:
        from powermonitor.backends.control import ControlMonsoonBackend

        return ControlMonsoonBackend()
    from powermonitor.backends.sampling import SamplingOnlyMonsoonBackend

    return SamplingOnlyMonsoonBackend()


def select_device(backend: Backend, serial: str | None) -> str:
    devices = backend.scan()
    if serial is not None:
        if serial not in {device.serial for device in devices}:
            raise BackendError(f"Device not found: {serial}")
        return serial
    if len(devices) != 1:
        raise BackendError("Exactly one device is required without --serial")
    return devices[0].serial


def record(
    backend: Backend, device: DeviceInfo, args: argparse.Namespace
) -> tuple[dict[str, Any], int]:
    from powermonitor.storage import CaptureWriter

    try:
        writer = CaptureWriter(
            args.out,
            {
                "backend": args.backend,
                "device_serial": device.serial,
                "device_name": device.name,
                "device_firmware": device.firmware,
                "sdk_revision": SDK_REVISION,
                "power_control": "unchanged",
                "output_command_state": "unknown",
                "requested_duration_s": args.seconds,
            },
        )
    except BaseException:
        try:
            backend.disconnect()
        except Exception as exc:
            print(f"USB cleanup also failed: {exc}", file=sys.stderr)
        raise
    cancel = Event()
    timer = Timer(args.seconds, cancel.set)
    timer.daemon = True
    stream = None
    requested = False
    errors: list[str] = []
    handlers = {}
    interrupted = False
    started = time.monotonic()

    def stop(signum: int, frame: object) -> None:
        nonlocal interrupted
        interrupted = True
        cancel.set()

    try:
        for signum in (signal.SIGINT, signal.SIGTERM):
            handlers[signum] = signal.signal(signum, stop)
        writer.event("capture_started", {"seconds": args.seconds})
        timer.start()
        requested = True
        stream = backend.sample_batches(cancel)
        for batch in stream:
            writer.append(batch)
            if cancel.is_set():
                break
    except AcquisitionCancelled as exc:
        if not cancel.is_set():
            errors.append(str(exc))
    except Exception as exc:
        errors.append(str(exc))
    finally:
        cancel.set()
        timer.cancel()
        if stream is not None:
            try:
                stream.close()
            except Exception as exc:
                errors.append(f"Sample stream cleanup failed: {exc}")
        stopped = not requested
        if requested:
            try:
                backend.stop_sampling()
                stopped = True
            except Exception as exc:
                errors.append(f"Sampling STOP failed: {exc}; output unchanged")
        if stopped:
            try:
                backend.disconnect()
            except Exception as exc:
                errors.append(f"USB cleanup failed: {exc}")
        elapsed = time.monotonic() - started
        if not interrupted and elapsed < args.seconds and not errors:
            errors.append("Sample stream ended before the requested duration")
        if writer.sample_count == 0 and not errors:
            errors.append("No measurement samples recorded")
        try:
            writer.event(
                "capture_error" if errors else "capture_stopped",
                {
                    "errors": errors,
                    "interrupted": interrupted,
                    "host_elapsed_s": elapsed,
                },
            )
        except Exception as exc:
            errors.append(f"Event write failed: {exc}")
        try:
            writer.close(complete=not errors)
        except Exception as exc:
            errors.append(f"Recording close failed: {exc}")
        for signum, handler in handlers.items():
            signal.signal(signum, handler)
    warnings = []
    if writer.metadata.get("loss_unknown"):
        warnings.append("Device sample loss reported; reconstructed time and integrals incomplete")
    for message in [*errors, *warnings]:
        print(message, file=sys.stderr)
    return (
        {
            "path": str(args.out),
            "sample_count": writer.sample_count,
            "complete": not errors,
            "interrupted": interrupted,
            "output": "unchanged",
            "errors": errors,
            "warnings": warnings,
        },
        INCOMPLETE if errors else SUCCESS,
    )


def hardware(args: argparse.Namespace) -> tuple[dict[str, Any], int]:
    lease = HardwareLease() if args.backend == "monsoon" else nullcontext()
    with lease:
        backend = make_backend(args.backend, control=args.command in {"status", "output"})
        # The SDK occasionally prints diagnostic messages. Keep JSON stdout clean.
        with redirect_stdout(sys.stderr):
            if args.command == "scan":
                return {"devices": [asdict(device) for device in backend.scan()]}, SUCCESS
            recording_owns_cleanup = False
            try:
                serial = select_device(backend, args.serial)
                device = backend.connect(serial)
                if args.command == "capture":
                    recording_owns_cleanup = True
                    return record(backend, device, args)
                if args.command == "status":
                    if args.backend == "simulator":
                        result = {
                            "serial": device.serial,
                            "firmware": device.firmware,
                            "configured_voltage_v": 0.0,
                            "output_state": "off",
                            "note": "New simulator process; no persistent physical output",
                        }
                    else:
                        from powermonitor.backends.control import ControlMonsoonBackend

                        assert isinstance(backend, ControlMonsoonBackend)
                        result = backend.read_status()
                    return result, SUCCESS
                if args.action == "on":
                    backend.set_voltage(args.voltage)
                else:
                    backend.disable_output()
                return {
                    "serial": device.serial,
                    "command": f"output {args.action}",
                    "configured_voltage_v": args.voltage if args.action == "on" else 0.0,
                    "output_state": "unknown" if args.backend == "monsoon" else args.action,
                    "note": "Readback is a configured value, not measured output; "
                    "commanded setting is preserved after exit",
                }, SUCCESS
            finally:
                original_error = sys.exc_info()[1]
                if not recording_owns_cleanup:
                    # Also clean up partial connection failures.
                    try:
                        backend.disconnect()
                    except Exception as exc:
                        if original_error is None:
                            raise
                        print(f"USB cleanup also failed: {exc}", file=sys.stderr)


def offline(args: argparse.Namespace) -> tuple[dict[str, Any], int]:
    from powermonitor.storage import CaptureReader, export_csv

    reader = CaptureReader(args.capture)
    if args.command == "info":
        return {
            "path": str(args.capture),
            "metadata": reader.metadata,
            "time_bounds_s": list(reader.time_bounds),
            "sample_count": len(reader.records),
            "warnings": reader.warnings,
        }, SUCCESS
    start, end = args.window if args.window else reader.time_bounds
    if not math.isfinite(start) or not math.isfinite(end) or start < 0 or end <= start:
        raise ValueError("Window must be finite, nonnegative and have positive duration")
    if args.command == "stats":
        from powermonitor.statistics import compute_statistics

        result = asdict(compute_statistics(reader.iter_chunks(), start, end))
        result["warnings"] = reader.warnings
        return result, SUCCESS
    export_csv(reader, args.out, start, end)
    return {
        "path": str(args.out),
        "format": "csv",
        "window_s": [start, end],
        "warnings": reader.warnings,
    }, SUCCESS


def main(argv: list[str] | None = None) -> int:
    root = parser()
    args = root.parse_args(argv)
    if args.command == "output" and args.action == "on":
        try:
            args.voltage = validate_voltage(args.voltage)
        except ValueError as exc:
            root.error(str(exc))
    if args.command == "capture":
        if not math.isfinite(args.seconds) or args.seconds <= 0:
            root.error("--seconds must be finite and positive")
        if args.out.suffix != ".pmcap":
            root.error("--out must name a new .pmcap directory")
        if args.out.exists():
            root.error("--out already exists; recordings are never overwritten")
    if args.command in {"stats", "export"} and args.window is not None:
        start, end = args.window
        if not math.isfinite(start) or not math.isfinite(end) or start < 0 or end <= start:
            root.error("--window must be finite, nonnegative and have positive duration")
    try:
        result, code = hardware(args) if args.command in HARDWARE_COMMANDS else offline(args)
        emit(result, args.json)
        return code
    except DeviceBusy as exc:
        print(str(exc), file=sys.stderr)
        return BUSY
    except (BackendError, OSError, ValueError, ImportError) as exc:
        suffix = "; actual output state unknown" if args.command == "output" else ""
        print(f"{args.command} failed: {exc}{suffix}", file=sys.stderr)
        return FAILURE
    except KeyboardInterrupt:
        print("Interrupted", file=sys.stderr)
        return FAILURE
