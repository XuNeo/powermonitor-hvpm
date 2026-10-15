"""Hardware-free performance checks. Never select or open a real backend."""

import argparse
import json
import math
import time
from pathlib import Path

import numpy as np
import psutil

from powermonitor.models import SAMPLE_DTYPE, SampleBatch
from powermonitor.statistics import compute_statistics
from powermonitor.storage import CaptureReader, CaptureWriter, export_csv


def synthetic(output: Path, seconds: int) -> dict:
    count = seconds * 5000
    process = psutil.Process()
    baseline = process.memory_info().rss
    started = time.monotonic()
    with CaptureWriter(output, {"backend": "benchmark", "synthetic": True}) as writer:
        for begin in range(0, count, 100000):
            a = np.zeros(min(100000, count - begin), dtype=SAMPLE_DTYPE)
            a["index"] = np.arange(begin, begin + len(a))
            a["time_s"] = a["index"] / 5000
            a["host_elapsed_s"] = a["time_s"]
            a["current_a"] = 0.1
            a["voltage_v"] = 3.3
            writer.append(SampleBatch(a))
    write_s = time.monotonic() - started
    started = time.monotonic()
    reader = CaptureReader(output)
    open_s = time.monotonic() - started
    started = time.monotonic()
    stats = compute_statistics(reader.iter_chunks(), 0, seconds)
    stats_s = time.monotonic() - started
    assert stats.sample_count == count
    assert stats.charge_c is not None and math.isclose(stats.charge_c, 0.1 * seconds, rel_tol=1e-8)
    assert stats.energy_j is not None and math.isclose(stats.energy_j, 0.33 * seconds, rel_tol=1e-8)
    start = max(0, seconds / 2 - 5)
    end = min(seconds, start + 10)
    started = time.monotonic()
    export_csv(reader, output.with_suffix(".selection.csv"), start, end)
    csv_s = time.monotonic() - started
    return {
        "mode": "synthetic",
        "seconds": seconds,
        "samples": count,
        "bytes": (output / "samples.bin").stat().st_size,
        "write_s": write_s,
        "open_validate_s": open_s,
        "statistics_s": stats_s,
        "selection_csv_s": csv_s,
        "charge_c": stats.charge_c,
        "energy_j": stats.energy_j,
        "rss_baseline_mb": baseline / 1024**2,
        "rss_final_mb": process.memory_info().rss / 1024**2,
        "note": "Read-only mapped pages may remain in RSS; this is not the live ring-buffer limit.",
    }


def realtime(output: Path, seconds: int) -> dict:
    from powermonitor.acquisition import AcquisitionController

    controller = AcquisitionController()
    process = psutil.Process()
    errors = []

    def wait_for(predicate, timeout=10):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            for event in controller.poll_events():
                if event.kind == "error":
                    errors.append(str(event.payload))
            if errors:
                raise RuntimeError("; ".join(errors))
            if predicate():
                return
            time.sleep(0.01)
        raise TimeoutError(f"Timed out waiting for simulator: {controller.state}")

    try:
        controller.request("connect", backend="simulator")
        wait_for(lambda: controller.state.connection == "connected")
        controller.request("set_voltage", voltage_v=3.3)
        controller.request("enable_output", confirmed=True)
        wait_for(lambda: controller.state.output == "on")
        controller.request("start", path=output)
        wait_for(lambda: controller.state.capture == "running")
        started = time.monotonic()
        tick_times, rss, recent_sizes = [], [], []
        while time.monotonic() - started < seconds:
            tick = time.monotonic()
            for event in controller.poll_events():
                if event.kind == "error":
                    raise RuntimeError(str(event.payload))
            a = controller.recent_records()
            recent_sizes.append(len(a))
            rss.append(process.memory_info().rss)
            tick_times.append(time.monotonic() - tick)
            time.sleep(max(0, 0.05 - (time.monotonic() - tick)))
        stop_started = time.monotonic()
        controller.request("stop")
        wait_for(lambda: controller.state.capture == "idle")
        stop_s = time.monotonic() - stop_started
        assert controller.state.output == "on", "Stopping capture changed output state"
        count = controller.state.sample_count
        controller.request("disable_output")
        wait_for(lambda: controller.state.output == "off")
        reader = CaptureReader(output)
        assert len(reader.records) == count
        # Verify every original sample across storage blocks; no UI sample queue is involved.
        previous = -1
        for a in reader.iter_chunks():
            assert int(a["index"][0]) == previous + 1
            assert np.all(np.diff(a["index"]) == 1)
            previous = int(a["index"][-1])
        return {
            "mode": "realtime-simulator",
            "requested_seconds": seconds,
            "wall_seconds": time.monotonic() - started,
            "samples": count,
            "recorded_seconds": reader.time_bounds[1],
            "stop_s": stop_s,
            "poll_count": len(tick_times),
            "poll_work_p95_ms": float(np.percentile(tick_times, 95) * 1000),
            "rss_min_mb": min(rss) / 1024**2,
            "rss_max_mb": max(rss) / 1024**2,
            "rss_final_mb": rss[-1] / 1024**2,
            "max_recent_samples": max(recent_sizes),
            "contiguous_samples": True,
            "metadata_complete": reader.metadata["complete"],
            "note": (
                "Controller + recorder + 20 Hz display snapshot; "
                "actual Qt rendering is a separate smoke test."
            ),
        }
    finally:
        controller.shutdown(timeout=5)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=["synthetic", "realtime"])
    parser.add_argument("output", type=Path)
    parser.add_argument("--seconds", type=int, default=None)
    args = parser.parse_args()
    seconds = args.seconds or (3600 if args.mode == "synthetic" else 600)
    if seconds < 1:
        parser.error("seconds must be positive")
    result = (synthetic if args.mode == "synthetic" else realtime)(args.output, seconds)
    rendered = json.dumps(result, indent=2)
    args.output.with_suffix(".benchmark.json").write_text(rendered + "\n")
    print(rendered)


if __name__ == "__main__":
    main()
