#!/usr/bin/env python3
"""Show live HVPM current in a terminal; never change output or save samples."""

from __future__ import annotations

import argparse
import json
import math
import shutil
import signal
import sys
import time
from collections import deque
from collections.abc import Generator
from contextlib import nullcontext, redirect_stdout
from threading import Event, Timer

import numpy as np

from powermonitor.backends.base import AcquisitionCancelled, Backend, BackendError
from powermonitor.hardware_lock import DeviceBusy, HardwareLease
from powermonitor.models import Quality, SampleBatch


def trend(values: deque[float]) -> str:
    """An ASCII sparkline, scaled to the displayed history in mA."""
    low, high = min(values), max(values)
    levels = ".:-=+*#%@"
    span = high - low
    bars = "".join(
        levels[round((value - low) / span * (len(levels) - 1))] if span else "-" for value in values
    )
    return f"[{bars}] {low:.3f}..{high:.3f} mA"


def waveform(history: deque[tuple[float, float]], columns: int, rows: int) -> list[str]:
    """Plot the recent refresh samples with a current axis and time labels."""
    width = min(100, max(2, columns - 14))
    height = min(10, max(2, rows - 8))
    points = list(history)[-width:]
    values = [value for _, value in points if math.isfinite(value)]
    if not values:
        return ["Waveform: waiting for valid samples"]
    low, high = min(values), max(values)
    padding = max((high - low) * 0.05, abs(high) * 0.01, 0.001)
    low, high = low - padding, high + padding
    grid = [[" " for _ in range(width)] for _ in range(height)]
    previous = None
    # Newest point stays at the right edge; invalid refreshes leave a gap.
    for column, (_, value) in enumerate(points, width - len(points)):
        if not math.isfinite(value):
            previous = None
            continue
        row = round((high - value) / (high - low) * (height - 1))
        if previous is not None:
            for bridge in range(min(previous, row) + 1, max(previous, row)):
                grid[bridge][column] = "|"
        grid[row][column] = "*"
        previous = row
    lines = ["Current (mA), auto-scaled"]
    for row, cells in enumerate(grid):
        level = high - row / (height - 1) * (high - low)
        lines.append(f"{level:10.3f} |" + "".join(cells))
    lines.append("           +" + "-" * width)
    lines.append(f"Time: {points[0][0]:.1f}s -> {points[-1][0]:.1f}s (latest)")
    return lines


def diagnostic_batches(
    stream: Generator[SampleBatch, None, None],
    machine: bool,
) -> Generator[SampleBatch, None, None]:
    """Keep SDK prints off JSON stdout without redirecting the caller's display."""
    while True:
        try:
            with redirect_stdout(sys.stderr) if machine else nullcontext():
                batch = next(stream)
        except StopIteration:
            return
        yield batch


def monitor(
    backend: Backend,
    serial: str | None,
    interval: float,
    seconds: float | None,
    plain: bool,
    machine: bool = False,
) -> int:
    cancel = Event()
    stream = None
    sampling_requested = False
    duration_timer: Timer | None = None
    failed = False
    previous_handlers = {}
    terminal = sys.stdout.isatty() and not machine
    history: deque[tuple[float, float]] = deque(maxlen=100)
    displayed_rows = 0
    plot_screen = False

    def stop(signum: int, frame: object) -> None:
        cancel.set()

    try:
        for signum in (signal.SIGINT, signal.SIGTERM):
            previous_handlers[signum] = signal.signal(signum, stop)
        if machine:
            with redirect_stdout(sys.stderr):
                device = backend.connect(serial)
        else:
            device = backend.connect(serial)
        print(
            f"HVPM {device.serial} | Ctrl+C to stop | output unchanged",
            file=sys.stderr if machine else sys.stdout,
            flush=True,
        )
        print(
            "I = latest valid sample; avg/peak = since previous refresh; "
            "waveform = I history, auto-scaled",
            file=sys.stderr if machine else sys.stdout,
            flush=True,
        )
        started = time.monotonic()
        if seconds is not None:
            duration_timer = Timer(seconds, cancel.set)
            duration_timer.daemon = True
            duration_timer.start()
        refresh = started
        total = 0.0
        count = 0
        peak = -math.inf
        quality_warning = False
        if cancel.is_set():
            raise AcquisitionCancelled("Sampling cancelled before startup")
        latest = math.nan
        sampling_requested = True
        stream = backend.sample_batches(cancel)
        for batch in diagnostic_batches(stream, machine):
            records = batch.records
            if len(records):
                flags = records["flags"]
                quality_warning |= bool(np.any(flags != int(Quality.GOOD)))
                valid = np.isfinite(records["current_a"]) & (
                    (flags & int(Quality.INVALID | Quality.DEVICE_ERROR)) == 0
                )
                quality_warning |= bool(np.any(~valid))
                currents = records["current_a"][valid] * 1000
                if len(currents):
                    latest = float(currents[-1])
                    total += float(currents.sum())
                    count += len(currents)
                    peak = max(peak, float(currents.max()))
            now = time.monotonic()
            done = cancel.is_set() or (seconds is not None and now - started >= seconds)
            if now >= refresh or done:
                if machine:
                    print(
                        json.dumps(
                            {
                                "elapsed_s": now - started,
                                "current_a": latest / 1000 if count else None,
                                "mean_current_a": total / count / 1000 if count else None,
                                "peak_current_a": peak / 1000 if count else None,
                                "sample_count": count,
                                "quality_warning": quality_warning,
                            },
                            allow_nan=False,
                        ),
                        flush=True,
                    )
                    total, count, peak = 0.0, 0, -math.inf
                    refresh = now + interval
                    if done:
                        break
                    continue
                if count:
                    line = (
                        f"{now - started:7.1f}s  I {latest:9.3f} mA"
                        f"  avg {total / count:9.3f}  peak {peak:9.3f} mA"
                    )
                else:
                    line = f"{now - started:7.1f}s  no valid current samples"
                if not plain:
                    history.append((now - started, latest if count else math.nan))
                if quality_warning and not terminal:
                    line += "  [sample quality warning]"
                if terminal:
                    size = shutil.get_terminal_size()
                    width, height = size.columns or 80, size.lines or 24
                    lines = [line]
                    if not plain and width >= 40 and height >= 12:
                        if not plot_screen:
                            # A separate screen prevents resize/reflow from
                            # overwriting the user's shell or leaving old rows.
                            sys.stdout.write("\033[?1049h")
                            plot_screen = True
                        lines.insert(0, "HVPM | Ctrl+C to stop | output unchanged")
                        lines += [
                            "[sample quality warning]"
                            if quality_warning
                            else "Sample quality: no warning observed"
                        ]
                        lines += waveform(history, width - 1, height)
                    elif quality_warning:
                        # Keep warnings on their own row: clipping a long numeric
                        # line must not hide quality problems in a narrow terminal.
                        lines.append("[sample quality warning]")
                    if plot_screen:
                        sys.stdout.write("\033[H")
                    elif displayed_rows > 1:
                        sys.stdout.write(f"\033[{displayed_rows - 1}A")
                    sys.stdout.write(
                        "\r\033[J" + "\n".join(item[: max(1, width - 1)] for item in lines)
                    )
                    sys.stdout.flush()
                    displayed_rows = len(lines)
                else:
                    values = deque(value for _, value in history if math.isfinite(value))
                    if not plain and values:
                        line += "  " + trend(values)
                    print(line, flush=True)
                total, count, peak = 0.0, 0, -math.inf
                refresh = now + interval
            if done:
                break
    except AcquisitionCancelled as exc:
        if not cancel.is_set():
            report_error(f"Live current failed: {exc}")
            failed = True
    except Exception as exc:
        report_error(f"Live current failed: {exc}")
        failed = True
    finally:
        cancel.set()
        if duration_timer is not None:
            duration_timer.cancel()
        if terminal:
            try:
                if plot_screen:
                    sys.stdout.write("\033[?1049l")
                print(flush=True)
            except OSError:
                # A closed terminal must not skip sampling STOP or USB cleanup.
                failed = True
        if stream is not None:
            try:
                stream.close()
            except Exception as exc:
                report_error(f"Sample stream cleanup failed: {exc}")
                failed = True
        stopped = not sampling_requested
        if sampling_requested:
            try:
                backend.stop_sampling()
                stopped = True
            except Exception as exc:
                report_error(f"Sampling STOP failed: {exc}; output unchanged")
                failed = True
        # Sampling-only cleanup releases host USB resources; no reset or Vout.
        if stopped:
            try:
                backend.disconnect()
            except Exception as exc:
                report_error(f"USB cleanup failed: {exc}")
                failed = True
        for signum, handler in previous_handlers.items():
            signal.signal(signum, handler)
    return 1 if failed else 0


def report_error(message: str) -> None:
    """A closed output stream must never prevent sampling STOP or USB cleanup."""
    try:
        print(message, file=sys.stderr, flush=True)
    except OSError:
        pass


def make_backend(name: str) -> Backend:
    if name == "simulator":
        from powermonitor.backends.simulator import SimulatorBackend

        return SimulatorBackend()
    from usb.backend import libusb1

    from powermonitor.backends.sampling import SamplingOnlyMonsoonBackend

    if libusb1.get_backend() is None:
        raise BackendError("System libusb is required for HVPM sampling")
    return SamplingOnlyMonsoonBackend()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--serial", default=None, help="HVPM serial number")
    parser.add_argument(
        "--interval", type=float, default=0.2, help="refresh interval in seconds (default: 0.2)"
    )
    parser.add_argument(
        "--seconds",
        type=float,
        default=None,
        help="stop after this duration, including calibration",
    )
    parser.add_argument("--plain", action="store_true", help="numbers only")
    parser.add_argument("--backend", choices=("monsoon", "simulator"), default="monsoon")
    parser.add_argument("--json", action="store_true", help="JSON lines on stdout, no waveform")
    args = parser.parse_args(argv)
    for name, value in (("interval", args.interval), ("seconds", args.seconds)):
        if value is not None and (not math.isfinite(value) or value <= 0):
            parser.error(f"--{name} must be finite and positive")
    try:
        from powermonitor.commands import select_device

        lease = HardwareLease() if args.backend == "monsoon" else nullcontext()
        with lease:
            # SDK diagnostics must not pollute JSON stdout.
            with redirect_stdout(sys.stderr):
                backend = make_backend(args.backend)
                serial = select_device(backend, args.serial)
            return monitor(backend, serial, args.interval, args.seconds, args.plain, args.json)
    except DeviceBusy as exc:
        report_error(str(exc))
        return 3
    except (BackendError, ImportError, OSError) as exc:
        report_error(f"Live current setup failed: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
