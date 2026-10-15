from threading import Event

import numpy as np
import pytest

from powermonitor.backends.simulator import SimulatorBackend
from powermonitor.models import SAMPLE_DTYPE, SAMPLE_PERIOD_S


def test_simulator_requires_connect_and_starts_off():
    backend = SimulatorBackend(batch_size=10)
    with pytest.raises(RuntimeError):
        backend.set_voltage(3.3)
    device = backend.connect()
    assert device.serial == "SIM-HVPM-0001"
    assert not backend.output_enabled


def test_simulator_batches_are_deterministic_si_records():
    backend = SimulatorBackend(batch_size=8)
    backend.connect()
    backend.set_voltage(3.3)
    cancel = Event()
    batches = backend.sample_batches(cancel)
    first = next(batches).records.copy()
    second = next(batches).records.copy()
    cancel.set()
    list(batches)

    assert first.dtype == SAMPLE_DTYPE
    assert np.array_equal(first["index"], np.arange(8))
    times = np.concatenate((first["time_s"], second["time_s"]))
    assert np.allclose(np.diff(times), SAMPLE_PERIOD_S)
    assert np.all(first["voltage_v"] == 3.3)
    assert np.all(np.isfinite(first["current_a"]))


def test_simulator_output_and_sampling_are_independent():
    backend = SimulatorBackend(batch_size=5)
    backend.connect()
    backend.set_voltage(5.0)
    cancel = Event()
    stream = backend.sample_batches(cancel)
    next(stream)
    cancel.set()
    list(stream)
    assert backend.output_enabled
    backend.disable_output()
    assert not backend.output_enabled
