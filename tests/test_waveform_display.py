"""Pure display-data contracts for truthful current waveforms."""

import numpy as np
import pytest

from powermonitor.models import SAMPLE_DTYPE, SAMPLE_PERIOD_S, Quality
from powermonitor.statistics import compute_statistics
from powermonitor.ui.plots import DisplayData, build_display_data


def _records(values: np.ndarray) -> np.ndarray:
    records = np.zeros(len(values), dtype=SAMPLE_DTYPE)
    records["index"] = np.arange(len(values), dtype=np.uint64)
    records["time_s"] = np.arange(len(values)) * SAMPLE_PERIOD_S
    records["host_elapsed_s"] = records["time_s"]
    records["current_a"] = values
    records["voltage_v"] = 3.3
    return records


def _bucket_reference(
    records: np.ndarray, lo: float, hi: float, columns: int
) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    bucket = np.floor((records["time_s"] - lo) * columns / (hi - lo)).astype(int)
    bucket = np.clip(bucket, 0, columns - 1)
    occupied = np.unique(bucket)
    centers = lo + (occupied + 0.5) * (hi - lo) / columns
    means = np.array([np.mean(records["current_a"][bucket == item]) for item in occupied])
    minima = np.array([np.min(records["current_a"][bucket == item]) for item in occupied])
    maxima = np.array([np.max(records["current_a"][bucket == item]) for item in occupied])
    return centers, means, minima, maxima


def test_constant_dense_display_has_mean_and_vertical_envelope() -> None:
    records = _records(np.full(10_000, 0.04))
    display = build_display_data(records, 0.0, 2.0, 100)
    assert isinstance(display, DisplayData)
    assert display.mode == "aggregate"
    assert np.allclose(display.line_y, 0.04)
    envelope_x = display.envelope_x.reshape(-1, 2)
    assert np.all(envelope_x[:, 0] == envelope_x[:, 1])
    assert np.all(display.envelope_y == 0.04)


def test_ramp_dense_display_matches_exact_bucket_statistics() -> None:
    records = _records(np.linspace(-0.2, 0.3, 12_345))
    lo, hi, columns = 0.0, 12_345 * SAMPLE_PERIOD_S, 173
    display = build_display_data(records, lo, hi, columns)
    centers, means, minima, maxima = _bucket_reference(records, lo, hi, columns)
    assert display.line_x == pytest.approx(centers)
    assert display.line_y == pytest.approx(means)
    assert display.envelope_x.reshape(-1, 2)[:, 0] == pytest.approx(centers)
    assert display.envelope_y.reshape(-1, 2)[:, 0] == pytest.approx(minima)
    assert display.envelope_y.reshape(-1, 2)[:, 1] == pytest.approx(maxima)
    assert np.all(np.diff(display.line_y) >= 0)


def test_sine_dense_display_uses_means_and_true_extrema() -> None:
    times = np.arange(30_000) * SAMPLE_PERIOD_S
    records = _records(0.1 + 0.03 * np.sin(2 * np.pi * 17 * times))
    lo, hi, columns = 0.0, 6.0, 239
    display = build_display_data(records, lo, hi, columns)
    centers, means, minima, maxima = _bucket_reference(records, lo, hi, columns)
    assert display.line_x == pytest.approx(centers)
    assert display.line_y == pytest.approx(means)
    assert display.envelope_y.reshape(-1, 2)[:, 0] == pytest.approx(minima)
    assert display.envelope_y.reshape(-1, 2)[:, 1] == pytest.approx(maxima)


def test_dense_envelope_preserves_positive_and_negative_single_sample_spikes() -> None:
    values = np.zeros(20_000)
    values[1234] = 2.5
    values[17_654] = -1.75
    display = build_display_data(_records(values), 0.0, 4.0, 200)
    pairs = display.envelope_y.reshape(-1, 2)
    assert np.max(pairs) == 2.5
    assert np.min(pairs) == -1.75
    assert np.max(display.line_y) < 2.5
    assert np.min(display.line_y) > -1.75


def test_fixed_seed_noise_matches_reference_without_mutating_data_or_statistics() -> None:
    rng = np.random.default_rng(20261013)
    records = _records(rng.normal(0.05, 0.004, 50_000))
    original = records.tobytes()
    before = compute_statistics(records, 0.0, 10.0)
    display = build_display_data(records, 0.0, 10.0, 257)
    centers, means, minima, maxima = _bucket_reference(records, 0.0, 10.0, 257)
    assert display.line_x == pytest.approx(centers)
    assert display.line_y == pytest.approx(means)
    assert display.envelope_y.reshape(-1, 2)[:, 0] == pytest.approx(minima)
    assert display.envelope_y.reshape(-1, 2)[:, 1] == pytest.approx(maxima)
    assert records.tobytes() == original
    assert compute_statistics(records, 0.0, 10.0) == before


def test_raw_mode_keeps_every_sample_and_inserts_break_before_gap_sample() -> None:
    records = _records(np.linspace(0.01, 0.02, 20))
    records["flags"][10] = int(Quality.GAP_BEFORE | Quality.LOSS_UNKNOWN)
    original = records.tobytes()
    display = build_display_data(records, 0.0, 20 * SAMPLE_PERIOD_S, 100)
    assert display.mode == "raw"
    assert len(display.line_x) == len(records) + 1
    assert np.isnan(display.line_y[10])
    assert display.line_x[10] == records["time_s"][10]
    assert display.line_x[11] == records["time_s"][10]
    assert display.line_y[11] == records["current_a"][10]
    assert np.array_equal(display.line_x[np.isfinite(display.line_y)], records["time_s"])
    assert np.array_equal(display.line_y[np.isfinite(display.line_y)], records["current_a"])
    assert len(display.envelope_x) == len(display.envelope_y) == 0
    assert records.tobytes() == original


def test_gap_invalid_and_device_error_split_dense_runs_within_same_bucket() -> None:
    records = _records(np.linspace(0.01, 0.02, 2000))
    records["flags"][20] = int(Quality.GAP_BEFORE)
    records["flags"][40] = int(Quality.INVALID)
    records["flags"][60] = int(Quality.DEVICE_ERROR)
    records["flags"][80] = int(Quality.LOSS_UNKNOWN)
    records["current_a"][100] = np.nan
    records["time_s"][120:] += 2 * SAMPLE_PERIOD_S
    display = build_display_data(records, 0.0, 0.5, 10)
    assert display.mode == "aggregate"
    assert np.count_nonzero(~np.isfinite(display.line_y)) >= 5
    pair_x = display.envelope_x.reshape(-1, 2)[:, 0]
    assert len(pair_x) > len(np.unique(pair_x)), "Runs in one bucket were incorrectly merged"
    records["current_a"][80] = 0.2
    display = build_display_data(records, 0.0, 0.5, 10)
    assert np.max(display.envelope_y) == 0.2


def test_chunk_boundary_groups_merge_into_one_exact_bucket_result() -> None:
    records = _records(np.linspace(-1.0, 1.0, 250_001))
    display = build_display_data(records, 0.0, 60.0, 10)
    centers, means, minima, maxima = _bucket_reference(records, 0.0, 60.0, 10)
    assert display.line_x == pytest.approx(centers)
    assert display.line_y == pytest.approx(means)
    assert display.envelope_y.reshape(-1, 2)[:, 0] == pytest.approx(minima)
    assert display.envelope_y.reshape(-1, 2)[:, 1] == pytest.approx(maxima)
