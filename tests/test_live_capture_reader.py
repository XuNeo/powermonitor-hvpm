import gc
import json
import os

import numpy as np
import pytest

from powermonitor.models import SAMPLE_DTYPE, SAMPLE_PERIOD_S, Quality, SampleBatch
from powermonitor.storage import CaptureWriter, GrowingCaptureReader


def samples(count: int, offset: int = 0) -> np.ndarray:
    records = np.zeros(count, dtype=SAMPLE_DTYPE)
    records["index"] = np.arange(offset, offset + count)
    records["time_s"] = records["index"] * SAMPLE_PERIOD_S
    records["host_elapsed_s"] = records["time_s"]
    records["current_a"] = 0.1
    records["voltage_v"] = 3.3
    return records


def append_raw(path, records: np.ndarray, trailing: bytes = b"") -> None:
    with (path / "samples.bin").open("ab") as stream:
        stream.write(records.tobytes(order="C"))
        stream.write(trailing)
        stream.flush()


def test_stale_metadata_and_open_writer_are_ignored(tmp_path):
    path = tmp_path / "live.pmcap"
    writer = CaptureWriter(path)
    writer.append(SampleBatch(samples(3)))

    reader = GrowingCaptureReader(path)

    assert reader.metadata["sample_count"] == 0
    assert reader.metadata["complete"] is False
    np.testing.assert_array_equal(reader.records, samples(3))
    assert reader.time_bounds == (0.0, 3 * SAMPLE_PERIOD_S)
    assert not reader.records.flags.writeable
    writer.close()


def test_append_refresh_and_no_growth_preserve_identity(tmp_path):
    path = tmp_path / "live.pmcap"
    writer = CaptureWriter(path)
    writer.append(SampleBatch(samples(2)))
    reader = GrowingCaptureReader(path)
    first = reader.records

    assert reader.refresh() is False
    assert reader.records is first

    writer.append(SampleBatch(samples(3, offset=2)))
    assert reader.refresh() is True
    second = reader.records
    assert second is not first
    assert len(second) == 5
    assert list(second["index"]) == list(range(5))
    assert reader.refresh() is False
    assert reader.records is second
    writer.close()


def test_old_snapshot_and_chunk_iterator_survive_refresh(tmp_path):
    path = tmp_path / "live.pmcap"
    writer = CaptureWriter(path)
    writer.append(SampleBatch(samples(4)))
    reader = GrowingCaptureReader(path)
    old = reader.records
    chunks = reader.iter_chunks(chunk_size=2)
    first_chunk = next(chunks)

    writer.append(SampleBatch(samples(3, offset=4)))
    assert reader.refresh() is True

    np.testing.assert_array_equal(old, samples(4))
    assert list(first_chunk["index"]) == [0, 1]
    assert [list(chunk["index"]) for chunk in chunks] == [[2, 3]]
    assert len(reader.records) == 7
    writer.close()


def test_partial_trailing_bytes_are_ignored_and_warning_clears(tmp_path):
    path = tmp_path / "live.pmcap"
    writer = CaptureWriter(path)
    writer.append(SampleBatch(samples(2)))
    reader = GrowingCaptureReader(path)
    first = reader.records

    append_raw(path, np.empty(0, dtype=SAMPLE_DTYPE), b"partial")
    assert reader.refresh() is False
    assert reader.records is first
    assert any("7 trailing bytes" in warning for warning in reader.warnings)

    writer.close(complete=False)


def test_invalid_append_keeps_last_stable_snapshot(tmp_path):
    path = tmp_path / "live.pmcap"
    writer = CaptureWriter(path)
    writer.append(SampleBatch(samples(3)))
    reader = GrowingCaptureReader(path)
    stable = reader.records
    bad = samples(2, offset=3)
    bad["time_s"] = [0.0, SAMPLE_PERIOD_S]
    append_raw(path, bad)

    with pytest.raises(ValueError, match="boundary"):
        reader.refresh()

    assert reader.records is stable
    assert len(reader.records) == 3
    np.testing.assert_array_equal(stable, samples(3))
    writer.close(complete=False)


def test_invalid_order_inside_new_range_keeps_snapshot(tmp_path):
    path = tmp_path / "live.pmcap"
    writer = CaptureWriter(path)
    writer.append(SampleBatch(samples(2)))
    reader = GrowingCaptureReader(path)
    stable = reader.records
    bad = samples(3, offset=2)
    bad["index"] = [2, 4, 3]
    append_raw(path, bad)

    with pytest.raises(ValueError, match="indices must increase"):
        reader.refresh()

    assert reader.records is stable
    writer.close(complete=False)


def test_loss_warning_is_sticky_across_refreshes(tmp_path):
    path = tmp_path / "live.pmcap"
    writer = CaptureWriter(path)
    writer.append(SampleBatch(samples(2)))
    reader = GrowingCaptureReader(path)
    lossy = samples(2, offset=2)
    lossy["flags"][0] = Quality.LOSS_UNKNOWN
    writer.append(SampleBatch(lossy))

    assert reader.refresh() is True
    assert any("report loss" in warning for warning in reader.warnings)
    writer.append(SampleBatch(samples(2, offset=4)))
    assert reader.refresh() is True
    assert any("report loss" in warning for warning in reader.warnings)
    writer.close()


def test_empty_capture_uses_plain_readonly_array(tmp_path):
    path = tmp_path / "empty.pmcap"
    writer = CaptureWriter(path)
    reader = GrowingCaptureReader(path)

    assert isinstance(reader.records, np.ndarray)
    assert not isinstance(reader.records, np.memmap)
    assert len(reader.records) == 0
    assert not reader.records.flags.writeable
    assert reader.time_bounds == (0.0, 0.0)
    assert list(reader.iter_chunks()) == []
    writer.close()


def test_shrink_and_inode_replacement_are_rejected(tmp_path):
    path = tmp_path / "live.pmcap"
    writer = CaptureWriter(path)
    writer.append(SampleBatch(samples(3)))
    reader = GrowingCaptureReader(path)
    binary = path / "samples.bin"

    writer._samples.close()
    with binary.open("r+b") as stream:
        stream.truncate(3 * SAMPLE_DTYPE.itemsize - 1)
    with pytest.raises(ValueError, match="shrank"):
        reader.refresh()

    replacement = path / "replacement.bin"
    replacement.write_bytes(samples(3).tobytes())
    os.replace(replacement, binary)
    with pytest.raises(ValueError, match="replaced"):
        reader.refresh()

    writer._closed = True
    writer._events.close()


def test_metadata_validation_is_shared_with_capture_reader(tmp_path):
    path = tmp_path / "bad.pmcap"
    writer = CaptureWriter(path)
    writer.close()
    metadata_path = path / "metadata.json"
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    metadata["record_size"] += 1
    metadata_path.write_text(json.dumps(metadata), encoding="utf-8")

    with pytest.raises(ValueError, match="Unsupported capture"):
        GrowingCaptureReader(path)


@pytest.mark.performance
def test_one_million_records_remain_memmapped(tmp_path):
    path = tmp_path / "million.pmcap"
    writer = CaptureWriter(path)
    block = samples(100_000)
    for offset in range(0, 1_000_000, len(block)):
        records = block.copy()
        records["index"] += offset
        records["time_s"] = records["index"] * SAMPLE_PERIOD_S
        records["host_elapsed_s"] = records["time_s"]
        writer.append(SampleBatch(records))

    reader = GrowingCaptureReader(path)
    assert isinstance(reader.records, np.memmap)
    assert len(reader.records) == 1_000_000
    old = reader.records
    writer.append(SampleBatch(samples(2, offset=1_000_000)))
    assert reader.refresh() is True
    assert isinstance(reader.records, np.memmap)
    assert len(reader.records) == 1_000_002
    del reader
    gc.collect()
    assert int(old["index"][-1]) == 999_999
    writer.close()
