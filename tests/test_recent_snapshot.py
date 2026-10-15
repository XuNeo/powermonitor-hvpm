"""Deterministic tests for the bounded, cached recent-record snapshot."""

import numpy as np
import pytest

from powermonitor.acquisition import _RECENT_LIMIT, AcquisitionController
from powermonitor.models import SAMPLE_DTYPE, SAMPLE_PERIOD_S, SampleBatch


def make_batch(start: int, count: int) -> SampleBatch:
    records = np.empty(count, dtype=SAMPLE_DTYPE)
    records["index"] = np.arange(start, start + count, dtype=np.uint64)
    timestamps = np.arange(start, start + count, dtype=np.float64) * SAMPLE_PERIOD_S
    records["time_s"] = timestamps
    records["host_elapsed_s"] = timestamps
    records["current_a"] = np.linspace(0.1, 0.2, count)
    records["voltage_v"] = 3.7
    records["flags"] = 0
    records["dropped"] = 0
    return SampleBatch(records)


@pytest.fixture
def controller():
    value = AcquisitionController()
    try:
        yield value
    finally:
        assert value.shutdown()


@pytest.fixture
def counted_concatenate(monkeypatch):
    calls = []
    real_concatenate = np.concatenate

    def wrapper(arrays, *args, **kwargs):
        calls.append(len(calls) + 1)
        return real_concatenate(arrays, *args, **kwargs)

    monkeypatch.setattr(np, "concatenate", wrapper)
    return calls


def test_empty_before_any_batch(controller, counted_concatenate):
    records = controller.recent_records()
    assert records.dtype == SAMPLE_DTYPE
    assert len(records) == 0
    assert counted_concatenate == []
    assert controller._recent_cache is None


def test_first_call_concatenates_batches(controller, counted_concatenate):
    controller._append_recent(make_batch(0, 100))
    controller._append_recent(make_batch(100, 50))

    assert counted_concatenate == []
    records = controller.recent_records()
    assert counted_concatenate == [1]
    assert len(records) == 150
    assert records["index"][0] == 0
    assert records["index"][-1] == 149


def test_repeated_calls_do_not_concatenate_again(controller, counted_concatenate):
    controller._append_recent(make_batch(0, 100))
    first = controller.recent_records()
    second = controller.recent_records()
    third = controller.recent_records()

    assert counted_concatenate == [1]
    assert np.array_equal(first, second)
    assert np.array_equal(first, third)


def test_returned_copy_does_not_poison_cache(controller):
    controller._append_recent(make_batch(0, 100))
    first = controller.recent_records()
    original_current = float(first["current_a"][0])
    first["current_a"][0] = -999.0
    first["voltage_v"][:] = 0.0

    second = controller.recent_records()
    assert not np.shares_memory(first, second)
    assert second["current_a"][0] == pytest.approx(original_current)
    assert second["voltage_v"][0] == pytest.approx(3.7)
    cached = controller._recent_cache
    assert cached is not None
    assert cached["current_a"][0] == pytest.approx(original_current)


def test_append_invalidates_cache_and_includes_new_samples(controller, counted_concatenate):
    controller._append_recent(make_batch(0, 100))
    controller.recent_records()
    assert controller._recent_cache is not None
    controller.recent_records()
    controller.recent_records()
    assert counted_concatenate == [1]

    controller._append_recent(make_batch(100, 25))
    assert controller._recent_cache is None

    records = controller.recent_records()
    assert counted_concatenate == [1, 2]
    assert len(records) == 125
    assert records["index"][0] == 0
    assert records["index"][-1] == 124


def test_clear_leaves_empty_snapshot(controller, counted_concatenate):
    controller._append_recent(make_batch(0, 100))
    controller.recent_records()
    assert controller._recent_cache is not None

    controller._clear_recent()
    assert controller._recent_cache is None

    records = controller.recent_records()
    assert len(records) == 0
    assert records.dtype == SAMPLE_DTYPE
    assert counted_concatenate == [1]

    controller._append_recent(make_batch(0, 10))
    rebuilt = controller.recent_records()
    assert counted_concatenate == [1, 2]
    assert len(rebuilt) == 10


def test_overflow_keeps_only_last_limit_samples(controller, counted_concatenate):
    batch_size = 30_000
    for start in range(0, 3 * batch_size, batch_size):
        controller._append_recent(make_batch(start, batch_size))

    assert controller._recent_count < _RECENT_LIMIT + batch_size
    records = controller.recent_records()
    assert len(records) == _RECENT_LIMIT
    assert records["index"][0] == 40_000
    assert records["index"][-1] == 89_999
    assert counted_concatenate == [1]

    cached = controller._recent_cache
    assert cached is not None
    assert len(cached) == _RECENT_LIMIT


def test_cache_copy_is_independent_after_trim(controller):
    controller._append_recent(make_batch(0, _RECENT_LIMIT + 1_000))
    records = controller.recent_records()
    assert len(records) == _RECENT_LIMIT
    assert records["index"][0] == 1_000
    records["current_a"][:] = -1.0

    again = controller.recent_records()
    assert len(again) == _RECENT_LIMIT
    assert again["current_a"][0] != -1.0
