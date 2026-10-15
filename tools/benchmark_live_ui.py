"""Measure simulator-driven Qt refresh cost without touching real hardware."""

import argparse
import json
import time
from pathlib import Path

import numpy as np
import psutil
from benchmark_plot import summarize_display
from PySide6 import QtTest, QtWidgets

from powermonitor.ui.window import MainWindow


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("output", type=Path)
    parser.add_argument("--seconds", type=int, default=10)
    args = parser.parse_args()
    app = QtWidgets.QApplication([])
    window = MainWindow()
    # Drive the same 50 ms callback manually so the benchmark does not double
    # refresh work with MainWindow's own QTimer.
    window.poll_timer.stop()
    window.show()
    process = psutil.Process()
    warmup_costs = []
    live_costs = []

    def wait_for(predicate, timeout=10):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            started = time.perf_counter()
            window._poll_controller()
            app.processEvents()
            warmup_costs.append(time.perf_counter() - started)
            if predicate():
                return
            QtTest.QTest.qWait(10)
        raise TimeoutError(window.controller.state)

    try:
        window.controller.request("connect", backend="simulator")
        wait_for(lambda: window.controller.state.connection == "connected")
        window.controller.request("enable_output", confirmed=True)
        wait_for(lambda: window.controller.state.output == "on")
        capture = args.output.with_suffix(".pmcap")
        window.controller.request("start", path=capture)
        wait_for(lambda: window.controller.state.capture == "running")
        started = time.monotonic()
        while time.monotonic() - started < args.seconds:
            tick = time.perf_counter()
            window._poll_controller()
            app.processEvents()
            live_costs.append(time.perf_counter() - tick)
            QtTest.QTest.qWait(50)
        active_costs = [cost for cost in live_costs if cost >= 0.005]
        # Push one final poll so the newest records reach the plot, then cross at
        # least one macOS 2 Hz display frame before reading the curve composition.
        window._poll_controller()
        app.processEvents()
        QtTest.QTest.qWait(60)
        app.processEvents()
        live_display = summarize_display(window.plot_widget)
        elapsed_total = sum(live_costs)
        result = {
            "seconds": args.seconds,
            "samples": window.controller.state.sample_count,
            "ui_tick_p50_ms": float(np.percentile(live_costs, 50) * 1000),
            "ui_tick_p95_ms": float(np.percentile(live_costs, 95) * 1000),
            "ui_tick_max_ms": max(live_costs) * 1000,
            "active_refresh_p95_ms": (
                float(np.percentile(active_costs, 95) * 1000) if active_costs else 0.0
            ),
            "active_refresh_count": len(active_costs),
            "ui_work_total_ms": elapsed_total * 1000,
            "ui_work_duty_percent": elapsed_total / args.seconds * 100,
            "warmup_tick_max_ms": max(warmup_costs) * 1000,
            "rss_mb": process.memory_info().rss / 1024**2,
            "display_final": live_display,
        }
        window.controller.request("stop")
        wait_for(lambda: window.controller.state.capture == "idle")
        window.controller.request("disable_output")
        wait_for(lambda: window.controller.state.output == "off")
        args.output.write_text(json.dumps(result, indent=2) + "\n")
        print(json.dumps(result, indent=2))
        return 0
    finally:
        window.controller.shutdown(timeout=3)
        deadline = time.monotonic() + 5
        while not window._wait_for_background_tasks() and time.monotonic() < deadline:
            app.processEvents()
            QtTest.QTest.qWait(10)
        window.close()


if __name__ == "__main__":
    raise SystemExit(main())
