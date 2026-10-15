"""Deterministic boundary tests for the pure core: models, statistics, storage.

Complements test_core.py: large-timestamp float windowing, cross-chunk
ordering, signed/non-finite measurements, metadata validation and strict
CSV exchange. All data is synthetic; no hardware or system configuration.
"""

import json

import numpy as np
import pytest

from powermonitor.models import SAMPLE_DTYPE, SAMPLE_PERIOD_S, Quality, SampleBatch
from powermonitor.statistics import compute_statistics
from powermonitor.storage import (
    CSV_FIELDS,
    FORMAT,
    FORMAT_VERSION,
    MAX_METADATA_BYTES,
    CaptureReader,
    CaptureWriter,
    export_csv,
    import_csv,
)


def make_samples(count, current=0.1, voltage=3.3, index0=0):
    a = np.zeros(count, dtype=SAMPLE_DTYPE)
    index = np.arange(index0, index0 + count)
    a["index"] = index
    a["time_s"] = index / 5000
    a["host_elapsed_s"] = np.floor(index / 5) / 1000
    a["current_a"], a["voltage_v"] = current, voltage
    return a


def raw_capture(path, records, *, complete=True, sample_count=None, metadata=None):
    """Write a capture directory bypassing CaptureWriter validation."""
    path.mkdir(parents=True)
    (path / "samples.bin").write_bytes(records.tobytes(order="C"))
    meta = {
        "format": FORMAT,
        "version": FORMAT_VERSION,
        "sample_rate_hz": 5000,
        "sample_period_s": SAMPLE_PERIOD_S,
        "record_size": SAMPLE_DTYPE.itemsize,
        "units": {"time_s": "s", "host_elapsed_s": "s", "current_a": "A", "voltage_v": "V"},
        "complete": complete,
        "loss_unknown": False,
        "sample_count": len(records) if sample_count is None else sample_count,
    }
    if metadata:
        meta.update(metadata)
    (path / "metadata.json").write_text(json.dumps(meta), encoding="utf-8")
    return path


def patch_metadata(path, **changes):
    metadata_path = path / "metadata.json"
    meta = json.loads(metadata_path.read_text(encoding="utf-8"))
    for key, value in changes.items():
        if value is ...:
            meta.pop(key, None)
        else:
            meta[key] = value
    metadata_path.write_text(json.dumps(meta), encoding="utf-8")


# --- large timestamps and half-open support boundaries --------------------


def test_large_timestamp_exact_window_boundaries_are_half_open():
    # 1e9 s base: float64 still resolves 0.2 ms spacing (~1700 ulp per step).
    a = make_samples(8, index0=5_000_000_000)
    t = a["time_s"]
    stats = compute_statistics(a, t[2], t[6])
    assert stats.sample_count == 4
    assert stats.valid_sample_count == 4
    # Boundary samples share the exact same float bits as the window edges.
    assert stats.valid_duration_s == pytest.approx(4 * SAMPLE_PERIOD_S, abs=1e-9)
    assert stats.coverage == pytest.approx(1.0, abs=1e-9)


def test_large_timestamp_partial_support_weights():
    a = make_samples(6, index0=5_000_000_000, current=0.1)
    t = a["time_s"]
    start, end = t[1] + 0.0001, t[4]
    stats = compute_statistics(a, start, end)
    # Supports 1 (half), 2 and 3 (full): 0.1 ms + 0.2 ms + 0.2 ms.
    assert stats.sample_count == 3
    assert stats.valid_duration_s == pytest.approx(0.0005, abs=2e-6)
    assert stats.charge_c == pytest.approx(0.1 * 0.0005, abs=2e-7)


def test_large_timestamp_iter_chunks_matches_support_semantics(tmp_path):
    a = make_samples(8, index0=5_000_000_000)
    path = raw_capture(tmp_path / "large.pmcap", a)
    reader = CaptureReader(path)
    t = reader.records["time_s"]
    selected = np.concatenate(list(reader.iter_chunks(t[2], t[6], chunk_size=3)))
    assert list(selected["index"]) == list(a["index"][2:6])


def test_timestamp_above_float_resolution_rejected_as_overlap():
    # Near 1e13 s the float64 ulp (~1.9 ms) exceeds the 0.2 ms support:
    # adjacent timestamps collapse, which must be rejected, not integrated.
    a = make_samples(4, index0=0)
    a["time_s"] = 1e13 + np.arange(4) * SAMPLE_PERIOD_S
    with pytest.raises(ValueError, match="overlap"):
        SampleBatch(a)
    with pytest.raises(ValueError, match="overlap"):
        compute_statistics(a, 1e13, 1e13 + 1)


# --- cross-chunk gaps, duplicates and ordering -----------------------------


def test_cross_chunk_gap_counted_at_boundary():
    first = make_samples(2)
    second = make_samples(2, index0=5)
    second["time_s"] = [0.001, 0.0012]
    stats = compute_statistics([first, second], 0.0, 0.01)
    assert stats.sample_count == 4
    assert stats.gap_count == 1
    assert stats.valid_duration_s == pytest.approx(4 * SAMPLE_PERIOD_S)


def test_cross_chunk_gap_flag_alone_also_counts():
    first = make_samples(2)
    second = make_samples(2, index0=2)
    second["flags"][0] = Quality.GAP_BEFORE
    stats = compute_statistics([first, second], 0.0, 0.001)
    assert stats.gap_count == 1


def test_cross_chunk_duplicate_index_and_time_overlap_rejected():
    first = make_samples(2)
    duplicate_index = make_samples(2, index0=1)
    duplicate_index["time_s"] = [0.0004, 0.0006]
    with pytest.raises(ValueError, match="indices overlap across chunks"):
        compute_statistics([first, duplicate_index], 0.0, 0.001)
    time_overlap = make_samples(2, index0=5)
    time_overlap["time_s"] = [0.0001, 0.0003]
    with pytest.raises(ValueError, match="overlap or are out of order"):
        compute_statistics([first, time_overlap], 0.0, 0.001)


def test_writer_accepts_cross_batch_gap(tmp_path):
    path = tmp_path / "gap.pmcap"
    with CaptureWriter(path) as writer:
        writer.append(SampleBatch(make_samples(2)))
        second = make_samples(2, index0=5)
        second["time_s"] = [0.001, 0.0012]
        writer.append(SampleBatch(second))
    assert len(CaptureReader(path).records) == 4


def test_reader_detects_disorder_across_chunk_boundary(tmp_path):
    records = np.zeros(150_000, dtype=SAMPLE_DTYPE)
    records["index"] = np.arange(150_000)
    records["time_s"][:100_000] = np.arange(100_000) / 5000
    # Internally sorted, but earlier than the preceding 100k chunk.
    records["time_s"][100_000:] = np.arange(50_000) / 5000
    path = raw_capture(tmp_path / "disordered-time.pmcap", records)
    with pytest.raises(ValueError, match="time is out of order"):
        CaptureReader(path)
    reader = CaptureReader(path, validate=False)
    assert len(reader.records) == 150_000


def test_reader_detects_index_reset_across_chunk_boundary(tmp_path):
    records = np.zeros(150_000, dtype=SAMPLE_DTYPE)
    records["time_s"] = np.arange(150_000) / 5000
    records["index"][:100_000] = np.arange(100_000)
    records["index"][100_000:] = np.arange(50_000)
    path = raw_capture(tmp_path / "disordered-index.pmcap", records)
    with pytest.raises(ValueError, match="indices are out of order"):
        CaptureReader(path)


# --- signed and non-finite measurements ------------------------------------


def test_negative_current_is_preserved_not_clipped():
    a = make_samples(4, voltage=3.3)
    a["current_a"] = [-0.2, -0.2, 0.4, 0.4]
    stats = compute_statistics(a, 0.0, 0.0008)
    assert stats.min_current_a == pytest.approx(-0.2)
    assert stats.max_current_a == pytest.approx(0.4)
    assert stats.mean_current_a == pytest.approx(0.1)
    assert stats.charge_c == pytest.approx(0.1 * 0.0008)
    assert stats.mean_power_w == pytest.approx(0.33)
    assert stats.energy_j == pytest.approx(0.33 * 0.0008)


@pytest.mark.parametrize(
    "current, voltage",
    [
        (np.nan, 3.3),
        (np.inf, 3.3),
        (-np.inf, 3.3),
        (0.1, np.nan),
        (np.inf, 0.0),  # inf * 0 is NaN
        (1e308, 1e308),  # finite inputs, non-finite product
    ],
)
def test_nonfinite_measurements_excluded_from_every_integral(current, voltage):
    a = make_samples(2, current=0.1, voltage=3.3)
    a["current_a"][0], a["voltage_v"][0] = current, voltage
    stats = compute_statistics(a, 0.0, 0.0004)
    assert stats.sample_count == 2
    assert stats.valid_sample_count == 1
    assert stats.invalid_count == 1
    assert stats.valid_duration_s == pytest.approx(SAMPLE_PERIOD_S)
    assert stats.coverage == pytest.approx(0.5)
    assert stats.min_current_a == stats.max_current_a == pytest.approx(0.1)
    assert stats.mean_current_a == pytest.approx(0.1)


def test_invalid_and_device_error_flags_exclude_finite_values():
    a = make_samples(3)
    a["flags"][0] = Quality.INVALID
    a["flags"][1] = Quality.DEVICE_ERROR
    stats = compute_statistics(a, 0.0, 0.0006)
    assert stats.sample_count == 3
    assert stats.valid_sample_count == 1
    assert stats.invalid_count == 2
    assert stats.coverage == pytest.approx(1 / 3)


def test_all_invalid_window_reports_none_integrals():
    a = make_samples(2, current=np.nan)
    stats = compute_statistics(a, 0.0, 0.0004)
    assert stats.sample_count == 2 and stats.valid_sample_count == 0
    assert stats.valid_duration_s == 0 and stats.coverage == 0
    assert stats.mean_current_a is None
    assert stats.min_current_a is None
    assert stats.charge_c is None and stats.energy_j is None


# --- LOSS_UNKNOWN recording warning ----------------------------------------


def test_loss_unknown_is_sticky_in_metadata_and_warning(tmp_path):
    clean = tmp_path / "clean.pmcap"
    with CaptureWriter(clean) as writer:
        writer.append(SampleBatch(make_samples(2)))
    clean_reader = CaptureReader(clean)
    assert clean_reader.metadata["loss_unknown"] is False
    assert not any("loss" in warning for warning in clean_reader.warnings)

    lossy = tmp_path / "lossy.pmcap"
    with CaptureWriter(lossy) as writer:
        writer.append(SampleBatch(make_samples(2)))
        second = make_samples(2, index0=2)
        second["flags"][0] = Quality.LOSS_UNKNOWN
        writer.append(SampleBatch(second))
    lossy_reader = CaptureReader(lossy)
    assert lossy_reader.metadata["loss_unknown"] is True
    assert any("sample loss" in warning for warning in lossy_reader.warnings)


def test_loss_unknown_warning_present_for_incomplete_recording(tmp_path):
    path = tmp_path / "lossy-incomplete.pmcap"
    writer = CaptureWriter(path)
    a = make_samples(2)
    a["flags"][0] = Quality.LOSS_UNKNOWN
    writer.append(SampleBatch(a))
    writer.close(complete=False)
    reader = CaptureReader(path)
    text = " ".join(reader.warnings)
    assert "not completed" in text and "sample loss" in text


def test_loss_unknown_flag_scoped_to_selected_samples():
    a = make_samples(4)
    a["flags"][3] = Quality.LOSS_UNKNOWN
    assert compute_statistics(a, 0.0, 0.0004).loss_unknown is False
    assert compute_statistics(a, 0.0, 0.0008).loss_unknown is True


# --- metadata validation, truncation and recovery --------------------------


def test_writer_rejects_non_json_safe_metadata(tmp_path):
    path = tmp_path / "nan-meta.pmcap"
    with pytest.raises(ValueError):
        CaptureWriter(path, {"note": float("nan")})
    assert not path.exists()


@pytest.mark.parametrize(
    "change, match",
    [
        ({"format": "other"}, "Unsupported"),
        ({"format": 1}, "Unsupported"),
        ({"version": FORMAT_VERSION + 1}, "Unsupported"),
        ({"version": 1.0}, "Unsupported"),
        ({"version": "1"}, "Unsupported"),
        ({"sample_rate_hz": 48000}, "Unsupported"),
        ({"sample_rate_hz": 5000.0}, "Unsupported"),
        ({"sample_period_s": 0.001}, "Unsupported"),
        ({"record_size": SAMPLE_DTYPE.itemsize + 1}, "Unsupported"),
        ({"record_size": float(SAMPLE_DTYPE.itemsize)}, "Unsupported"),
        ({"complete": 1}, "completion flag"),
        ({"complete": "true"}, "completion flag"),
        ({"complete": ...}, "completion flag"),
        ({"loss_unknown": 1}, "sample-loss flag"),
        ({"loss_unknown": "true"}, "sample-loss flag"),
        ({"sample_count": -1}, "sample count"),
        ({"sample_count": 1.0}, "sample count"),
        ({"sample_count": "5"}, "sample count"),
        ({"sample_count": ...}, "sample count"),
    ],
)
def test_reader_metadata_field_types_and_format(tmp_path, change, match):
    key = next(iter(change))
    path = raw_capture(tmp_path / f"meta-{key}.pmcap", make_samples(2))
    patch_metadata(path, **change)
    with pytest.raises(ValueError, match=match):
        CaptureReader(path)


def test_reader_rejects_wrong_units(tmp_path):
    path = raw_capture(tmp_path / "units-ma.pmcap", make_samples(2))
    patch_metadata(path, units={"time_s": "s", "current_a": "mA", "voltage_v": "V"})
    with pytest.raises(ValueError, match="[Uu]nits"):
        CaptureReader(path)
    path = raw_capture(tmp_path / "units-missing.pmcap", make_samples(2))
    patch_metadata(path, units=...)
    with pytest.raises(ValueError, match="[Uu]nits"):
        CaptureReader(path)


def test_recoverable_residual_loss_flag_upgrades_metadata_warning(tmp_path):
    # Metadata written with loss_unknown=False but recovered records carry the flag.
    records = make_samples(3)
    records["flags"][2] = Quality.LOSS_UNKNOWN
    path = raw_capture(tmp_path / "residual-loss.pmcap", records, complete=False)
    reader = CaptureReader(path)
    assert reader.metadata["loss_unknown"] is True
    assert any("records report loss" in warning for warning in reader.warnings)
    # Only the recovered-records warning, not the metadata-driven wording.
    assert not any("Device sample loss reported" in warning for warning in reader.warnings)


def test_loss_flag_scan_spans_multiple_reader_chunks(tmp_path):
    records = make_samples(150_000)
    records["flags"][120_000] = Quality.LOSS_UNKNOWN
    path = raw_capture(tmp_path / "late-loss.pmcap", records, complete=False)
    reader = CaptureReader(path)
    assert reader.metadata["loss_unknown"] is True
    assert any("records report loss" in warning for warning in reader.warnings)


def test_bool_sample_count_is_not_accepted_as_int(tmp_path):
    path = raw_capture(tmp_path / "bool-count.pmcap", make_samples(2))
    patch_metadata(path, sample_count=True)
    with pytest.raises(ValueError, match="sample count"):
        CaptureReader(path)


def test_reader_metadata_shape_and_member_rules(tmp_path):
    missing = tmp_path / "missing-meta.pmcap"
    missing.mkdir()
    (missing / "samples.bin").write_bytes(b"")
    with pytest.raises(ValueError, match="regular file"):
        CaptureReader(missing)

    path = raw_capture(tmp_path / "dict.pmcap", make_samples(1))
    metadata_path = path / "metadata.json"
    metadata_path.rename(path / "metadata-real.json")
    metadata_path.symlink_to(path / "metadata-real.json")
    with pytest.raises(ValueError, match="regular file"):
        CaptureReader(path)

    non_dict = tmp_path / "non-dict.pmcap"
    non_dict.mkdir()
    (non_dict / "samples.bin").write_bytes(b"")
    (non_dict / "metadata.json").write_text("[1, 2, 3]", encoding="utf-8")
    with pytest.raises(ValueError, match="Invalid capture metadata"):
        CaptureReader(non_dict)

    oversized = tmp_path / "oversized.pmcap"
    oversized.mkdir()
    (oversized / "samples.bin").write_bytes(b"")
    (oversized / "metadata.json").write_text("a" * (MAX_METADATA_BYTES + 1), encoding="utf-8")
    with pytest.raises(ValueError, match="too large"):
        CaptureReader(oversized)


def test_completed_capture_count_mismatch_warns(tmp_path):
    path = tmp_path / "truncated-complete.pmcap"
    with CaptureWriter(path) as writer:
        writer.append(SampleBatch(make_samples(5)))
    binary = path / "samples.bin"
    binary.write_bytes(binary.read_bytes()[: 3 * SAMPLE_DTYPE.itemsize])
    reader = CaptureReader(path)
    assert len(reader.records) == 3
    assert any("does not match file length" in warning for warning in reader.warnings)


def test_incomplete_trailing_bytes_warn_without_count_check(tmp_path):
    path = tmp_path / "trailing.pmcap"
    with CaptureWriter(path) as writer:
        writer.append(SampleBatch(make_samples(3)))
    patch_metadata(path, complete=False)
    with (path / "samples.bin").open("ab") as stream:
        stream.write(b"partial-record")
    reader = CaptureReader(path)
    assert len(reader.records) == 3
    text = " ".join(reader.warnings)
    assert "not completed" in text and "trailing bytes" in text
    assert "does not match file length" not in text


# --- CSV strict exchange ----------------------------------------------------

CSV_HEADER = ",".join(CSV_FIELDS)


def write_csv(tmp_path, name, text):
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return path


@pytest.mark.parametrize(
    "row",
    [
        "x,0,0,1,3,3,0,0",
        "-1,0,0,1,3,3,0,0",
        "18446744073709551616,0,0,1,3,3,0,0",
        "1.0,0,0,1,3,3,0,0",
        "0,0,0,1,3,3,-1,0",
        "0,0,0,1,3,3,65536,0",
        "0,0,0,1,3,3,0,-1",
        "0,0,0,1,3,3,0,65536",
    ],
)
def test_csv_rejects_invalid_integers(tmp_path, row):
    source = write_csv(tmp_path, "bad-int.csv", f"{CSV_HEADER}\n{row}\n")
    with pytest.raises(ValueError, match=r"Invalid CSV sample at line 2"):
        import_csv(source, tmp_path / "out.pmcap")


def test_csv_rejects_wrong_column_counts_and_header_order(tmp_path):
    short = write_csv(tmp_path, "short.csv", f"{CSV_HEADER}\n0,0,0,1,3,3,0\n")
    with pytest.raises(ValueError, match=r"line 2.*Wrong column count"):
        import_csv(short, tmp_path / "short.pmcap")
    long_row = write_csv(tmp_path, "long.csv", f"{CSV_HEADER}\n0,0,0,1,3,3,0,0,9\n")
    with pytest.raises(ValueError, match=r"line 2.*Wrong column count"):
        import_csv(long_row, tmp_path / "long.pmcap")
    reordered = write_csv(
        tmp_path,
        "reordered.csv",
        "index,time_s,host_elapsed_s,voltage_v,current_a,power_w,flags,dropped\n0,0,0,3,1,3,0,0\n",
    )
    with pytest.raises(ValueError, match="SI-unit"):
        import_csv(reordered, tmp_path / "reordered.pmcap")


def test_csv_tampered_power_reports_exact_line(tmp_path):
    source = write_csv(
        tmp_path,
        "tampered.csv",
        f"{CSV_HEADER}\n"
        "0,0.0,0.0,1,3,3,0,0\n"
        "1,0.0002,0.0002,1,3,3,0,0\n"
        "2,0.0004,0.0004,1,3,9,0,0\n",
    )
    with pytest.raises(ValueError, match=r"line 4.*inconsistent"):
        import_csv(source, tmp_path / "tampered.pmcap")


def test_csv_finite_power_with_nonfinite_product_rejected(tmp_path):
    source = write_csv(tmp_path, "bad-inf.csv", f"{CSV_HEADER}\n0,0,0,inf,1,3,0,0\n")
    with pytest.raises(ValueError, match="invalid measurements"):
        import_csv(source, tmp_path / "bad-inf.pmcap")


def test_csv_roundtrip_preserves_nonfinite_measurement(tmp_path):
    records = make_samples(2)
    records["current_a"][0] = np.nan
    capture = tmp_path / "nan.pmcap"
    with CaptureWriter(capture) as writer:
        writer.append(SampleBatch(records))
    csv_path = tmp_path / "nan.csv"
    export_csv(CaptureReader(capture), csv_path)
    assert "nan" in csv_path.read_text(encoding="utf-8")
    imported = tmp_path / "nan-imported.pmcap"
    import_csv(csv_path, imported)
    assert np.isnan(CaptureReader(imported).records["current_a"][0])


def test_csv_import_destination_is_never_overwritten(tmp_path):
    source = write_csv(tmp_path, "ok.csv", f"{CSV_HEADER}\n0,0,0,1,3,3,0,0\n")
    destination = tmp_path / "once.pmcap"
    import_csv(source, destination)
    with pytest.raises(FileExistsError):
        import_csv(source, destination)


def test_csv_200k_flush_boundary_with_gap_imports_and_stats(tmp_path):
    # Crosses the 100,000-row append boundary with a 1 s gap exactly there.
    lines = [CSV_HEADER]
    for i in range(200_000):
        t = i / 5000 + (1.0 if i >= 100_000 else 0.0)
        lines.append(f"{i},{t},{t},0.001,3.3,0.0033,0,0")
    source = tmp_path / "big.csv"
    source.write_text("\n".join(lines) + "\n", encoding="utf-8")
    capture = tmp_path / "big.pmcap"
    import_csv(source, capture)
    reader = CaptureReader(capture)
    assert len(reader.records) == 200_000
    stats = compute_statistics(reader.iter_chunks(chunk_size=100_000), 0.0, 100.0)
    assert stats.sample_count == 200_000
    assert stats.gap_count == 1
    assert stats.mean_current_a == pytest.approx(0.001, rel=1e-9)
    assert stats.valid_duration_s == pytest.approx(200_000 * SAMPLE_PERIOD_S, rel=1e-9)
