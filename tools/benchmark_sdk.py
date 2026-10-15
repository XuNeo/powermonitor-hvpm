"""Measure the pinned decoder with synthetic raw USB packets, without hardware."""

import argparse
import json
import struct
import time
from collections import deque
from pathlib import Path
from threading import Event
from types import SimpleNamespace

import numpy as np

from powermonitor._vendor.pymonsoon import HVPM, sampleEngine
from powermonitor.backends.monsoon import MonsoonBackend


def packet(kind: int) -> np.ndarray:
    observation = [1000 if kind == 0x10 else 2000 if kind == 0x30 else 1500] * 6
    observation += [13200, 0, kind, 0]
    data = struct.pack("<HBB" + "HHHHHHHHBB" * 3, 0, 0, 3, *(observation * 3))
    raw = np.zeros(64, dtype=np.uint8)
    raw[: len(data)] = np.frombuffer(data, dtype=np.uint8)
    original = raw.copy()
    raw[4::2], raw[5::2] = original[5::2], original[4::2]
    return raw


class FixtureDevice:
    fineThreshold = 64000
    auxFineThreshold = 30000
    mainvoltageScale = 4
    usbVoltageScale = 2
    swizzlePacket = HVPM.Monsoon.swizzlePacket
    padding = np.zeros(64)

    def __init__(self):
        self.calibration = deque([packet(kind) for _ in range(5) for kind in (0x10, 0x30)])
        self.measurement = packet(0)
        self.statusPacket = SimpleNamespace(
            hardwareModel=2,
            serialNumber=17,
            firmwareVersion=32,
            mainCoarseScale=1.0,
            mainFineScale=1000.0,
            usbCoarseScale=1.0,
            usbFineScale=1000.0,
            auxCoarseScale=1.0,
            auxFineScale=1000.0,
            mainCoarseZeroOffset=0.0,
            mainFineZeroOffset=0.0,
            usbCoarseZeroOffset=0.0,
            usbFineZeroOffset=0.0,
        )
        self.Protocol = SimpleNamespace(DEVICE=object())

    def setup_usb(self, serialno=None):
        pass

    def fillStatusPacket(self):
        pass

    def StartSampling(self, *args):
        pass

    def stopSampling(self):
        pass

    def BulkRead(self):
        return self.calibration.popleft() if self.calibration else self.measurement


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("output", type=Path)
    parser.add_argument("--samples", type=int, default=50000)
    args = parser.parse_args()
    device = FixtureDevice()
    backend = MonsoonBackend(
        device_factory=lambda: device, engine_factory=sampleEngine.SampleEngine
    )
    backend.connect()
    cancel = Event()
    stream = backend.sample_batches(cancel)
    count = 0
    started = time.monotonic()
    for batch in stream:
        count += len(batch)
        if count >= args.samples:
            cancel.set()
    backend.stop_sampling()
    elapsed = time.monotonic() - started
    result = {
        "samples": count,
        "elapsed_s": elapsed,
        "samples_per_s": count / elapsed,
        "above_nominal_5000_hz": count / elapsed > 5000,
        "scope": "Real SDK calibration + raw packet decode + adapter, without USB latency",
    }
    text = json.dumps(result, indent=2)
    args.output.write_text(text + "\n")
    print(text)


if __name__ == "__main__":
    main()
