"""Crash-safe hwlock lease shared by desktop and command-line device sessions."""

from __future__ import annotations

import os
import selectors
import shutil
import signal
import subprocess
import sys
from types import TracebackType

from powermonitor.backends.base import BackendError


class DeviceBusy(BackendError):
    """The shared HVPM resource could not be acquired before its deadline."""


class HardwareLease:
    """Hold hwlock exec while this process owns USB, including GUI worker threads.

    A pipe keeps the guarded child alive. On owner death the pipe closes, so the
    helper exits and hwlock releases its kernel lock. The helpers ignore terminal
    signals: only the USB owner decides when sampling and cleanup have finished.
    """

    def __init__(self) -> None:
        self._process: subprocess.Popen[str] | None = None

    def acquire(self) -> None:
        if self._process is not None:
            raise BackendError("Hardware lease is already held")
        if shutil.which("hwlock") is None:
            raise BackendError("Hardware access requires hwlock; install it before use")
        process = subprocess.Popen(
            [sys.executable, "-I", "-m", "powermonitor.hardware_lock", "--wrapper"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        self._process = process
        try:
            assert process.stdout is not None
            with selectors.DefaultSelector() as selector:
                selector.register(process.stdout, selectors.EVENT_READ)
                if not selector.select(timeout=5):
                    raise DeviceBusy("Timed out acquiring monsoon-hvpm hardware lock")
                if process.stdout.readline().strip() != "LOCKED":
                    assert process.stderr is not None
                    error = process.stderr.read().strip()
                    raise DeviceBusy(error or "Unable to acquire monsoon-hvpm hardware lock")
        except BaseException:
            self.release()
            raise

    def release(self) -> None:
        process, self._process = self._process, None
        if process is None:
            return
        if process.stdin is not None:
            process.stdin.close()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.terminate()
            process.wait(timeout=5)
        finally:
            for stream in (process.stdout, process.stderr):
                if stream is not None:
                    stream.close()

    def __enter__(self) -> HardwareLease:
        self.acquire()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.release()


def main() -> int:
    for signum in (signal.SIGINT, signal.SIGTERM):
        signal.signal(signum, signal.SIG_IGN)
    if sys.argv[1:] == ["--wrapper"]:
        executable = shutil.which("hwlock")
        if executable is None:
            return 1
        os.execv(
            executable,
            [
                executable,
                "exec",
                "--resource",
                "monsoon-hvpm",
                "--label",
                f"powermonitor-{os.getpid()}",
                "--timeout",
                "2",
                "--",
                sys.executable,
                "-I",
                "-m",
                "powermonitor.hardware_lock",
                "--hold",
            ],
        )
    if sys.argv[1:] == ["--hold"]:
        print("LOCKED", flush=True)
        sys.stdin.read()
        return 0
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
