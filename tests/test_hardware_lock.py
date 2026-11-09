"""Exercise real hwlock without USB in an isolated state directory."""

import os
import selectors
import shutil
import signal
import subprocess
import sys
from unittest.mock import Mock

import pytest

from powermonitor.hardware_lock import DeviceBusy, HardwareLease


@pytest.fixture
def isolated_hwlock(monkeypatch, tmp_path):
    if shutil.which("hwlock") is None:
        pytest.skip("hwlock executable is not installed")
    monkeypatch.setenv("HWLOCK_STATE_DIR", str(tmp_path / "locks"))


def test_lease_contends_with_same_resource_then_releases(isolated_hwlock):
    first = HardwareLease()
    second = HardwareLease()
    try:
        first.acquire()
        with pytest.raises(DeviceBusy, match="timeout"):
            second.acquire()
        first.release()
        second.acquire()
    finally:
        first.release()
        second.release()
        second.release()


def test_owner_death_releases_hwlock_without_manual_recovery(isolated_hwlock):
    code = (
        "import time; from powermonitor.hardware_lock import HardwareLease; "
        "lease=HardwareLease(); lease.acquire(); print('READY', flush=True); time.sleep(30)"
    )
    owner = subprocess.Popen(
        [sys.executable, "-I", "-c", code],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        with selectors.DefaultSelector() as selector:
            selector.register(owner.stdout, selectors.EVENT_READ)
            assert selector.select(5)
            assert owner.stdout.readline().strip() == "READY"
        owner.kill()
        owner.wait(timeout=5)
        with HardwareLease():
            pass
    finally:
        if owner.poll() is None:
            owner.kill()
        owner.communicate(timeout=5)


def test_terminal_signals_do_not_release_wrapper_before_owner_cleanup(isolated_hwlock):
    with HardwareLease() as lease:
        wrapper = lease._process
        assert wrapper is not None
        os.kill(wrapper.pid, signal.SIGINT)
        os.kill(wrapper.pid, signal.SIGTERM)
        assert wrapper.poll() is None
        with pytest.raises(DeviceBusy):
            with HardwareLease():
                pass


def test_missing_hwlock_fails_without_launching_helper(monkeypatch):
    monkeypatch.setattr("powermonitor.hardware_lock.shutil.which", lambda _: None)
    launch = Mock()
    monkeypatch.setattr("powermonitor.hardware_lock.subprocess.Popen", launch)
    with pytest.raises(RuntimeError, match="requires hwlock"):
        HardwareLease().acquire()
    launch.assert_not_called()


def test_gui_can_acquire_from_worker_thread(isolated_hwlock):
    from concurrent.futures import ThreadPoolExecutor

    def check():
        with HardwareLease():
            return True

    with ThreadPoolExecutor(max_workers=1) as pool:
        assert pool.submit(check).result(timeout=10)
