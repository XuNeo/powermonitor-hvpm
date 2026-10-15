"""Acceptance properties for truthful oscilloscope-style display data."""

import numpy as np

from powermonitor.models import SAMPLE_DTYPE, Quality
from powermonitor.statistics import compute_statistics
from powermonitor.ui.plots import build_display_data


def records(values, *, sample_rate=5000):
    result = np.zeros(len(values), dtype=SAMPLE_DTYPE)
    result["index"] = np.arange(len(values))
    result["time_s"] = result["index"] / sample_rate
    result["host_elapsed_s"] = result["time_s"]
    result["current_a"] = values
    result["voltage_v"] = 3.3
    return result


def test_dense_constant_has_no_artificial_saw_wave():
    data = records(np.full(10_000, 0.04))
    display = build_display_data(data, 0.0, 2.0, 100)
    assert display.mode == "aggregate"
    assert np.allclose(display.line_y[np.isfinite(display.line_y)], 0.04)
    pairs = display.envelope_y.reshape(-1, 2)
    assert np.allclose(pairs[:, 0], pairs[:, 1])
    assert np.allclose(pairs, 0.04)


def test_dense_envelope_is_vertical_pairs_and_keeps_single_spike():
    values = np.zeros(20_000)
    values[12_345] = 2.5
    data = records(values)
    display = build_display_data(data, 0.0, 4.0, 200)
    x_pairs = display.envelope_x.reshape(-1, 2)
    y_pairs = display.envelope_y.reshape(-1, 2)
    assert np.all(x_pairs[:, 0] == x_pairs[:, 1])
    assert np.max(y_pairs) == 2.5
    assert np.count_nonzero(np.max(y_pairs, axis=1) == 2.5) == 1
    # The trend line is an actual bucket mean, never the extrema sequence.
    assert np.max(display.line_y) < 2.5


def test_dense_white_noise_matches_bruteforce_bucket_statistics():
    rng = np.random.default_rng(20261013)
    values = rng.normal(0.05, 0.004, 50_000)
    data = records(values)
    columns = 257
    display = build_display_data(data, 0.0, 10.0, columns)
    assert display.mode == "aggregate"
    pair_x = display.envelope_x.reshape(-1, 2)[:, 0]
    pair_y = display.envelope_y.reshape(-1, 2)
    for x, (minimum, maximum) in zip(pair_x, pair_y):
        bucket = min(columns - 1, int(x / 10.0 * columns))
        lo, hi = bucket * 10.0 / columns, (bucket + 1) * 10.0 / columns
        mask = (data["time_s"] >= lo) & (data["time_s"] < hi)
        assert minimum == np.min(values[mask])
        assert maximum == np.max(values[mask])


def test_raw_zoom_returns_every_original_sample_and_no_envelope():
    values = np.sin(np.linspace(0, 3, 1000))
    data = records(values)
    display = build_display_data(data, 0.0, 0.2, 1000)
    assert display.mode == "raw"
    assert np.array_equal(display.line_x, data["time_s"])
    assert np.array_equal(display.line_y, data["current_a"])
    assert len(display.envelope_x) == len(display.envelope_y) == 0


def test_gap_splits_mean_line_and_does_not_change_raw_statistics():
    data = records(np.linspace(0.01, 0.02, 2000))
    data["flags"][1000] = int(Quality.GAP_BEFORE)
    before = compute_statistics(data, 0.0, 0.4)
    original = data.tobytes()
    display = build_display_data(data, 0.0, 0.4, 100)
    assert data.tobytes() == original
    assert np.any(~np.isfinite(display.line_y))
    assert len(display.envelope_x) % 2 == 0
    after = compute_statistics(data, 0.0, 0.4)
    assert before == after
