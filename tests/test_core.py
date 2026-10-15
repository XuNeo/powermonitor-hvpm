import json

import numpy as np
import pytest

from powermonitor.models import SAMPLE_DTYPE, Quality, SampleBatch, validate_voltage
from powermonitor.statistics import compute_statistics
from powermonitor.storage import CaptureReader, CaptureWriter, export_csv, import_csv


def samples(count=5000, current=0.1, voltage=3.3, offset=0):
    a = np.zeros(count, dtype=SAMPLE_DTYPE)
    a["index"] = np.arange(offset, offset + count)
    a["time_s"] = a["index"] / 5000
    a["host_elapsed_s"] = np.floor(a["index"] / 5) / 1000
    a["current_a"], a["voltage_v"] = current, voltage
    return a


@pytest.mark.parametrize("voltage", [0.8, 3.3, 13.5])
def test_voltage_valid(voltage):
    assert validate_voltage(voltage) == voltage


@pytest.mark.parametrize("voltage", [0, 0.79, 13.51, 3.333, float("nan"), float("inf")])
def test_voltage_rejected(voltage):
    with pytest.raises(ValueError):
        validate_voltage(voltage)


def test_constant_integrals():
    s = compute_statistics(samples(), 0, 1)
    assert s.sample_count == 5000
    assert s.coverage == pytest.approx(1)
    assert s.mean_current_a == pytest.approx(0.1)
    assert s.mean_voltage_v == pytest.approx(3.3)
    assert s.mean_power_w == pytest.approx(0.33)
    assert s.charge_c == pytest.approx(0.1)
    assert s.energy_j == pytest.approx(0.33)
    assert s.charge_mah == pytest.approx(0.1 / 3.6)
    assert s.energy_mwh == pytest.approx(0.33 / 3.6)


def test_partial_half_open_window():
    a = samples(3)
    a["current_a"] = [1, 2, 4]
    s = compute_statistics(a, 0.0001, 0.0004)
    assert s.sample_count == 2
    assert s.valid_duration_s == pytest.approx(0.0003)
    assert s.mean_current_a == pytest.approx(5 / 3)
    assert s.charge_c == pytest.approx(0.0005)


def test_empty_and_single_sample():
    s = compute_statistics(samples(0), 0, 1)
    assert s.mean_current_a is None and s.charge_c is None
    assert s.coverage == 0
    s = compute_statistics(samples(1), 0, 0.0002)
    assert s.sample_count == 1 and s.coverage == 1
    assert s.charge_c == pytest.approx(0.00002)


def test_gap_invalid_and_unknown_loss():
    a = samples(5)[[0, 1, 3, 4]].copy()
    a["current_a"][1] = np.nan
    a["flags"][2] = Quality.LOSS_UNKNOWN | Quality.GAP_BEFORE
    s = compute_statistics(a, 0, 0.001)
    assert s.sample_count == 4 and s.valid_sample_count == 3
    assert s.coverage == pytest.approx(0.6)
    assert s.invalid_count == 1 and s.gap_count == 1 and s.loss_unknown
    assert s.mean_current_a == pytest.approx(0.1)
    assert s.charge_c == pytest.approx(0.00006)


def test_nonfinite_voltage_and_error_flags():
    a = samples(4)
    a["voltage_v"][0] = np.inf
    a["flags"][1] = Quality.DEVICE_ERROR
    a["flags"][2] = Quality.INVALID
    s = compute_statistics(a, 0, 0.0008)
    assert s.valid_sample_count == 1
    assert s.invalid_count == 3
    assert s.coverage == pytest.approx(0.25)


def test_chunk_invariance_preserves_spike():
    a = samples(10000)
    a["current_a"][5678] = 1.7
    whole = compute_statistics(a, 0.017, 1.991)
    chunks = compute_statistics((a[i : i + 137] for i in range(0, len(a), 137)), 0.017, 1.991)
    assert whole.sample_count == chunks.sample_count
    assert chunks.max_current_a == 1.7
    assert chunks.charge_c == pytest.approx(whole.charge_c, rel=1e-13)
    assert chunks.mean_current_a == pytest.approx(whole.mean_current_a, rel=1e-13)


@pytest.mark.parametrize("start,end", [(1, 1), (2, 1), (-1, 1), (0, np.inf), (np.nan, 1)])
def test_invalid_window(start, end):
    with pytest.raises(ValueError):
        compute_statistics(samples(), start, end)


def test_invalid_sample_order():
    a = samples(2)
    a["time_s"][1] = 0.0001
    with pytest.raises(ValueError, match="overlap"):
        SampleBatch(a)
    a = samples(2)
    with pytest.raises(ValueError, match="overlap"):
        compute_statistics([a, a], 0, 1)


def test_capture_roundtrip_and_csv(tmp_path):
    a = samples(7000)
    a["flags"][321] = Quality.LOSS_UNKNOWN
    capture = tmp_path / "source.pmcap"
    with CaptureWriter(capture, {"device": "simulator"}) as writer:
        writer.append(SampleBatch(a[:3333]))
        writer.event("example", {"value": 1})
        writer.append(SampleBatch(a[3333:]))
    reader = CaptureReader(capture)
    np.testing.assert_array_equal(reader.records, a)
    assert reader.metadata["complete"] is True
    assert reader.time_bounds == (0, 1.4)
    assert reader.warnings and reader.metadata["loss_unknown"]
    assert not reader.records.flags.writeable
    csv_path = tmp_path / "capture.csv"
    export_csv(reader, csv_path)
    imported = tmp_path / "imported.pmcap"
    import_csv(csv_path, imported)
    np.testing.assert_array_equal(CaptureReader(imported).records, a)
    with pytest.raises(FileExistsError):
        export_csv(reader, csv_path)
    with pytest.raises(FileExistsError):
        CaptureWriter(capture)


def test_selection_export_matches_support_semantics(tmp_path):
    path = tmp_path / "capture.pmcap"
    with CaptureWriter(path) as writer:
        writer.append(SampleBatch(samples(10)))
    reader = CaptureReader(path)
    exported = tmp_path / "window.csv"
    export_csv(reader, exported, 0.0001, 0.0004)
    assert len(exported.read_text().splitlines()) == 3
    selected = np.concatenate(list(reader.iter_chunks(0.0002, 0.0004, chunk_size=1)))
    assert list(selected["index"]) == [1]


def test_interrupted_and_partial_recovery(tmp_path):
    path = tmp_path / "interrupted.pmcap"
    writer = CaptureWriter(path)
    writer.append(SampleBatch(samples(3)))
    writer.close(complete=False)
    with (path / "samples.bin").open("ab") as stream:
        stream.write(b"partial")
    reader = CaptureReader(path)
    assert len(reader.records) == 3
    assert len(reader.warnings) == 2


def test_empty_capture(tmp_path):
    path = tmp_path / "empty.pmcap"
    CaptureWriter(path).close()
    reader = CaptureReader(path)
    assert reader.time_bounds == (0, 0)
    assert list(reader.iter_chunks()) == []


def test_writer_rejects_duplicate_batches(tmp_path):
    writer = CaptureWriter(tmp_path / "capture.pmcap")
    writer.append(SampleBatch(samples(2)))
    with pytest.raises(ValueError):
        writer.append(SampleBatch(samples(2)))
    writer.close()


def test_malformed_metadata_and_symlink_rejected(tmp_path):
    path = tmp_path / "capture.pmcap"
    CaptureWriter(path).close()
    metadata_path = path / "metadata.json"
    original = json.loads(metadata_path.read_text())
    original["record_size"] += 1
    metadata_path.write_text(json.dumps(original))
    with pytest.raises(ValueError, match="Unsupported"):
        CaptureReader(path)
    original["record_size"] -= 1
    metadata_path.write_text(json.dumps(original))
    binary = path / "samples.bin"
    binary.rename(path / "original.bin")
    binary.symlink_to(path / "original.bin")
    with pytest.raises(ValueError, match="regular"):
        CaptureReader(path)


def test_csv_rejects_wrong_units_and_malformed_records(tmp_path):
    path = tmp_path / "bad.csv"
    path.write_text("time,current_mA\n0,1\n")
    with pytest.raises(ValueError, match="SI-unit"):
        import_csv(path, tmp_path / "one.pmcap")
    path.write_text(
        "index,time_s,host_elapsed_s,current_a,voltage_v,power_w,flags,dropped\n0,0,0,1,3,999,0,0\n"
    )
    with pytest.raises(ValueError, match="inconsistent"):
        import_csv(path, tmp_path / "two.pmcap")
