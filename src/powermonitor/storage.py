"""Append-only, versioned capture storage and streaming SI-unit CSV exchange."""

import csv
import json
import math
import os
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock, RLock
from typing import Any

import numpy as np

from .models import SAMPLE_DTYPE, SAMPLE_PERIOD_S, SAMPLE_RATE_HZ, Quality, SampleBatch

FORMAT = "powermonitor-capture"
FORMAT_VERSION = 1
CSV_FIELDS = [
    "index",
    "time_s",
    "host_elapsed_s",
    "current_a",
    "voltage_v",
    "power_w",
    "flags",
    "dropped",
]
MAX_METADATA_BYTES = 1024 * 1024


def _json_write(path: Path, data: dict) -> None:
    temporary = path.with_suffix(".json.tmp")
    encoded = json.dumps(data, ensure_ascii=False, allow_nan=False, indent=2) + "\n"
    with temporary.open("x", encoding="utf-8") as stream:
        stream.write(encoded)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


def _member(path: Path, name: str) -> Path:
    member = path / name
    if member.is_symlink() or not member.is_file():
        raise ValueError(f"Capture member must be a regular file: {name}")
    return member


def _read_capture_metadata(path: Path) -> dict[str, Any]:
    metadata_path = _member(path, "metadata.json")
    if metadata_path.stat().st_size > MAX_METADATA_BYTES:
        raise ValueError("Recording metadata is too large")
    with metadata_path.open(encoding="utf-8") as stream:
        metadata = json.load(stream)
    if not isinstance(metadata, dict):
        raise ValueError("Invalid capture metadata")
    expected = {
        "format": FORMAT,
        "version": FORMAT_VERSION,
        "sample_rate_hz": SAMPLE_RATE_HZ,
        "sample_period_s": SAMPLE_PERIOD_S,
        "record_size": SAMPLE_DTYPE.itemsize,
    }
    if any(
        metadata.get(key) != value or type(metadata.get(key)) is not type(value)
        for key, value in expected.items()
    ):
        raise ValueError("Unsupported capture format, sample rate or record layout")
    if type(metadata.get("complete")) is not bool:
        raise ValueError("Invalid capture completion flag")
    if type(metadata.get("loss_unknown", False)) is not bool:
        raise ValueError("Invalid sample-loss flag")
    units = {"time_s": "s", "host_elapsed_s": "s", "current_a": "A", "voltage_v": "V"}
    if metadata.get("units") != units:
        raise ValueError("Unsupported capture units")
    count_meta = metadata.get("sample_count")
    if type(count_meta) is not int or count_meta < 0:
        raise ValueError("Invalid capture sample count")
    return metadata


@dataclass(frozen=True)
class _CaptureSnapshot:
    records: np.ndarray
    time_bounds: tuple[float, float]
    warnings: tuple[str, ...]


class CaptureWriter:
    """Create a new recording; existing paths are never overwritten."""

    def __init__(self, path: Path, metadata: dict | None = None):
        self.path = Path(path)
        self.metadata: dict[str, Any] = dict(metadata or {})
        self.metadata.update(
            format=FORMAT,
            version=FORMAT_VERSION,
            sample_rate_hz=SAMPLE_RATE_HZ,
            sample_period_s=SAMPLE_PERIOD_S,
            record_size=SAMPLE_DTYPE.itemsize,
            units={"time_s": "s", "host_elapsed_s": "s", "current_a": "A", "voltage_v": "V"},
            time_basis="sample index / nominal 5000 Hz; not hardware timestamps",
            created_at=datetime.now(timezone.utc).isoformat(),
            complete=False,
            sample_count=0,
            loss_unknown=False,
        )
        # Validate metadata before creating the directory.
        json.dumps(self.metadata, allow_nan=False)
        self.path.mkdir(parents=True, exist_ok=False)
        self.sample_count = 0
        self._last_time = self._last_index = None
        self._closed = False
        self._failed = False
        _json_write(self.path / "metadata.json", self.metadata)
        self._samples = (self.path / "samples.bin").open("xb")
        try:
            self._events = (self.path / "events.jsonl").open("x", encoding="utf-8")
        except OSError:
            self._samples.close()
            raise

    def append(self, batch: SampleBatch) -> None:
        if self._closed:
            raise ValueError("Recording is closed")
        SampleBatch(batch.records)
        if not len(batch):
            return
        a = batch.records
        if self._last_time is not None:
            if a["time_s"][0] < self._last_time + SAMPLE_PERIOD_S * (1 - 1e-6):
                raise ValueError("Recording sample supports overlap")
            if a["index"][0] <= self._last_index:
                raise ValueError("Recording sample indices are not increasing")
        try:
            data = a.tobytes(order="C")
            if self._samples.write(data) != len(data):
                raise OSError("Short write while recording samples")
            # Readers and the live UI must only see complete committed batches.
            self._samples.flush()
        except OSError:
            self._failed = True
            raise
        self.sample_count += len(a)
        self._last_time, self._last_index = float(a["time_s"][-1]), int(a["index"][-1])
        self.metadata["loss_unknown"] |= bool(np.any(a["flags"] & int(Quality.LOSS_UNKNOWN)))

    def event(self, kind: str, payload: dict | None = None) -> None:
        if self._closed:
            raise ValueError("Recording is closed")
        record = {"kind": str(kind), "sample_count": self.sample_count, "payload": payload or {}}
        try:
            self._events.write(json.dumps(record, ensure_ascii=False, allow_nan=False) + "\n")
            self._events.flush()
        except OSError:
            self._failed = True
            raise

    def close(self, complete: bool = True) -> None:
        if self._closed:
            return
        self._closed = True
        error = None
        for stream in (self._samples, self._events):
            try:
                stream.flush()
                os.fsync(stream.fileno())
            except OSError as exc:
                self._failed = True
                error = exc
            finally:
                try:
                    stream.close()
                except OSError as exc:
                    self._failed = True
                    error = exc
        self.metadata.update(
            sample_count=self.sample_count, complete=bool(complete and not self._failed)
        )
        self.metadata["finished_at"] = datetime.now(timezone.utc).isoformat()
        _json_write(self.path / "metadata.json", self.metadata)
        if error:
            raise error

    def __enter__(self) -> "CaptureWriter":
        return self

    def __exit__(self, exc_type, _exc, _traceback) -> None:
        self.close(complete=exc_type is None)


def _iter_record_chunks(
    records: np.ndarray,
    start: float | None = None,
    end: float | None = None,
    chunk_size: int = 100000,
) -> Iterator[np.ndarray]:
    if chunk_size < 1:
        raise ValueError("Chunk size must be positive")
    if start is not None and (not math.isfinite(start) or start < 0):
        raise ValueError("Window start must be finite and nonnegative")
    if end is not None and not math.isfinite(end):
        raise ValueError("Window end must be finite")
    if start is not None and end is not None and end <= start:
        raise ValueError("Window end must be after its start")
    times = records["time_s"]
    lo = 0 if start is None else int(np.searchsorted(times, start - SAMPLE_PERIOD_S, side="right"))
    hi = len(times) if end is None else int(np.searchsorted(times, end, side="left"))
    for offset in range(lo, hi, chunk_size):
        chunk = records[offset : min(hi, offset + chunk_size)]
        if start is not None:
            chunk = chunk[chunk["time_s"] + SAMPLE_PERIOD_S - start > 1e-12]
        if len(chunk):
            yield chunk


class CaptureReader:
    """Read complete records, including those recoverable from interrupted files."""

    def __init__(self, path: Path, validate: bool = True):
        self.path = Path(path)
        self.metadata = _read_capture_metadata(self.path)
        count_meta = self.metadata["sample_count"]
        binary = _member(self.path, "samples.bin")
        count, remainder = divmod(binary.stat().st_size, SAMPLE_DTYPE.itemsize)
        self.warnings: list[str] = []
        if not self.metadata["complete"]:
            self.warnings.append("Recording was not completed; complete records are recoverable")
        if remainder:
            self.warnings.append(f"Ignoring {remainder} trailing bytes of an incomplete sample")
        if self.metadata["complete"] and count_meta != count:
            self.warnings.append("Recorded sample count does not match file length")
        if self.metadata.get("loss_unknown"):
            self.warnings.append(
                "Device sample loss reported; reconstructed time and integrals incomplete"
            )
        self.records = (
            np.memmap(binary, dtype=SAMPLE_DTYPE, mode="r", shape=(count,))
            if count
            else np.empty(0, dtype=SAMPLE_DTYPE)
        )
        self.records.flags.writeable = False
        if validate:
            last_time = last_index = None
            loss_detected = False
            for a in self.iter_chunks():
                SampleBatch(a)
                loss_detected |= bool(np.any(a["flags"] & int(Quality.LOSS_UNKNOWN)))
                if last_time is not None:
                    if a["time_s"][0] < last_time + SAMPLE_PERIOD_S * (1 - 1e-6):
                        raise ValueError("Recording time is out of order")
                    if a["index"][0] <= last_index:
                        raise ValueError("Recording indices are out of order")
                last_time, last_index = float(a["time_s"][-1]), int(a["index"][-1])
            if loss_detected and not self.metadata.get("loss_unknown"):
                self.metadata["loss_unknown"] = True
                self.warnings.append(
                    "Sample records report loss; reconstructed time and integrals incomplete"
                )

    @property
    def time_bounds(self) -> tuple[float, float]:
        if not len(self.records):
            return 0.0, 0.0
        return float(self.records["time_s"][0]), float(self.records["time_s"][-1] + SAMPLE_PERIOD_S)

    def iter_chunks(
        self, start: float | None = None, end: float | None = None, chunk_size: int = 100000
    ) -> Iterator[np.ndarray]:
        records = self.records
        yield from _iter_record_chunks(records, start, end, chunk_size)


class GrowingCaptureReader:
    """Read stable snapshots from an append-only capture while it is being written."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.metadata = _read_capture_metadata(self.path)
        self._binary = _member(self.path, "samples.bin")
        stat = self._binary.stat()
        self._file_identity = (stat.st_dev, stat.st_ino)
        self._file_size = 0
        self._lock = RLock()
        self._refresh_lock = Lock()
        empty = np.empty(0, dtype=SAMPLE_DTYPE)
        empty.flags.writeable = False
        self._snapshot = _CaptureSnapshot(empty, (0.0, 0.0), ())
        self.refresh()

    @property
    def records(self) -> np.ndarray:
        with self._lock:
            return self._snapshot.records

    @property
    def time_bounds(self) -> tuple[float, float]:
        with self._lock:
            return self._snapshot.time_bounds

    @property
    def warnings(self) -> list[str]:
        with self._lock:
            return list(self._snapshot.warnings)

    def refresh(self) -> bool:
        """Publish a larger validated snapshot; leave the old one intact on failure."""
        with self._refresh_lock:
            stat = self._binary.stat()
            if (stat.st_dev, stat.st_ino) != self._file_identity:
                raise ValueError("Capture sample file was replaced")
            count, remainder = divmod(stat.st_size, SAMPLE_DTYPE.itemsize)
            with self._lock:
                previous = self._snapshot
                previous_count = len(previous.records)
                previous_size = self._file_size
                if stat.st_size < previous_size:
                    raise ValueError("Capture sample file shrank")
                if count == previous_count:
                    warnings = self._warnings_for(
                        previous.warnings, remainder, bool(self.metadata.get("loss_unknown"))
                    )
                    if warnings != previous.warnings:
                        self._snapshot = _CaptureSnapshot(
                            previous.records, previous.time_bounds, warnings
                        )
                    self._file_size = stat.st_size
                    return False

            records = np.memmap(self._binary, dtype=SAMPLE_DTYPE, mode="r", shape=(count,))
            records.flags.writeable = False
            new_records = records[previous_count:]
            try:
                SampleBatch(new_records)
            except ValueError as exc:
                raise ValueError(f"Invalid appended capture records: {exc}") from exc
            if previous_count:
                if new_records["time_s"][0] < (
                    previous.records["time_s"][-1] + SAMPLE_PERIOD_S * (1 - 1e-6)
                ):
                    raise ValueError("Recording time is out of order at appended boundary")
                if new_records["index"][0] <= previous.records["index"][-1]:
                    raise ValueError("Recording indices are out of order at appended boundary")

            loss_detected = bool(
                self.metadata.get("loss_unknown")
                or np.any(new_records["flags"] & int(Quality.LOSS_UNKNOWN))
            )
            warnings = self._warnings_for(previous.warnings, remainder, loss_detected)
            bounds = (
                float(records["time_s"][0]),
                float(records["time_s"][-1] + SAMPLE_PERIOD_S),
            )
            snapshot = _CaptureSnapshot(records, bounds, warnings)
            with self._lock:
                self._snapshot = snapshot
                self._file_size = stat.st_size
            return True

    @staticmethod
    def _warnings_for(
        previous: tuple[str, ...], remainder: int, loss_detected: bool
    ) -> tuple[str, ...]:
        warnings = [warning for warning in previous if "trailing bytes" not in warning]
        if remainder:
            warnings.append(f"Ignoring {remainder} trailing bytes of an incomplete sample")
        loss_warning = "Sample records report loss; reconstructed time and integrals incomplete"
        if loss_detected and loss_warning not in warnings:
            warnings.append(loss_warning)
        return tuple(warnings)

    def iter_chunks(
        self, start: float | None = None, end: float | None = None, chunk_size: int = 100000
    ) -> Iterator[np.ndarray]:
        with self._lock:
            records = self._snapshot.records
        yield from _iter_record_chunks(records, start, end, chunk_size)


LiveCaptureReader = GrowingCaptureReader


def export_csv(
    reader: CaptureReader, destination: Path, start: float | None = None, end: float | None = None
) -> None:
    """Export original overlapping samples, not cropped or downsampled values."""
    destination = Path(destination)
    with destination.open("x", encoding="utf-8", newline="") as stream:
        writer = csv.writer(stream)
        writer.writerow(CSV_FIELDS)
        for chunk in reader.iter_chunks(start, end):
            for record in chunk:
                current, voltage = float(record["current_a"]), float(record["voltage_v"])
                writer.writerow(
                    [
                        int(record["index"]),
                        float(record["time_s"]),
                        float(record["host_elapsed_s"]),
                        current,
                        voltage,
                        current * voltage,
                        int(record["flags"]),
                        int(record["dropped"]),
                    ]
                )


def import_csv(source: Path, destination: Path) -> None:
    """Import this application's SI-unit CSV format; never evaluate file data."""
    with Path(source).open(encoding="utf-8", newline="") as stream:
        reader = csv.DictReader(stream)
        if reader.fieldnames != CSV_FIELDS:
            raise ValueError("CSV must contain the Power Monitor SI-unit columns in order")
        with CaptureWriter(destination, {"source": "csv"}) as writer:
            rows = []
            for line, row in enumerate(reader, 2):
                try:
                    if None in row or any(value is None for value in row.values()):
                        raise ValueError("Wrong column count")
                    index, flags, dropped = (
                        int(row["index"]),
                        int(row["flags"]),
                        int(row["dropped"]),
                    )
                    if not (0 <= index < 2**64 and 0 <= flags < 2**16 and 0 <= dropped < 2**16):
                        raise ValueError("Integer out of range")
                    time_s = float(row["time_s"])
                    host = float(row["host_elapsed_s"])
                    current, voltage = float(row["current_a"]), float(row["voltage_v"])
                    power = float(row["power_w"])
                    calculated = current * voltage
                    if math.isfinite(calculated):
                        if not math.isclose(calculated, power, rel_tol=1e-12, abs_tol=1e-15):
                            raise ValueError("CSV power is inconsistent with current and voltage")
                    elif math.isfinite(power):
                        raise ValueError("CSV power is inconsistent with invalid measurements")
                    rows.append((index, time_s, host, current, voltage, flags, dropped))
                except (ValueError, TypeError, OverflowError) as exc:
                    raise ValueError(f"Invalid CSV sample at line {line}: {exc}") from exc
                if len(rows) == 100000:
                    writer.append(SampleBatch(np.array(rows, dtype=SAMPLE_DTYPE)))
                    rows.clear()
            if rows:
                writer.append(SampleBatch(np.array(rows, dtype=SAMPLE_DTYPE)))
