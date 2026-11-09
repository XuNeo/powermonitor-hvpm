"""Single-threaded backend ownership and capture orchestration."""

from __future__ import annotations

import queue
import threading
from collections import deque
from dataclasses import replace
from pathlib import Path
from typing import Any, Callable

import numpy as np

from powermonitor.backends.base import Backend, BackendError
from powermonitor.backends.locked import LockedMonsoonBackend
from powermonitor.backends.simulator import SimulatorBackend
from powermonitor.models import (
    SAMPLE_DTYPE,
    SDK_REVISION,
    ControllerEvent,
    ControllerState,
    SampleBatch,
    validate_voltage,
)
from powermonitor.storage import CaptureWriter

_RECENT_LIMIT = 50_000


class AcquisitionController:
    """Serialize every SDK operation on one dedicated worker thread."""

    def __init__(
        self,
        *,
        backend_factories: dict[str, Callable[[], Backend]] | None = None,
        writer_factory: Callable[..., CaptureWriter] = CaptureWriter,
    ) -> None:
        self._backend_factories = backend_factories or {
            "simulator": SimulatorBackend,
            "monsoon": LockedMonsoonBackend,
        }
        self._writer_factory = writer_factory
        self._commands: queue.Queue[tuple[str, dict[str, Any]]] = queue.Queue()
        self._events: queue.Queue[ControllerEvent] = queue.Queue()
        self._lock = threading.Lock()
        self._state = ControllerState()
        self._state_event_pending = False
        self._start_pending = False
        self._backend: Backend | None = None
        self._cancel = threading.Event()
        self._recent: deque[np.ndarray] = deque()
        self._recent_count = 0
        self._recent_cache: np.ndarray | None = None
        self._shutdown_requested = False
        self._thread = threading.Thread(
            target=self._worker_main, name="powermonitor-sdk", daemon=True
        )
        self._thread.start()

    @property
    def state(self) -> ControllerState:
        with self._lock:
            return self._state

    def request(self, action: str, **kwargs: Any) -> None:
        allowed = {
            "scan",
            "connect",
            "disconnect",
            "set_voltage",
            "enable_output",
            "disable_output",
            "start",
            "stop",
        }
        if action not in allowed:
            raise ValueError(f"Unknown controller action: {action}")
        if self._shutdown_requested:
            raise RuntimeError("Controller is shutting down")
        with self._lock:
            state = self._state
            start_pending = self._start_pending
        blocked_during_capture = {
            "scan",
            "connect",
            "disconnect",
            "set_voltage",
            "enable_output",
            "start",
        }
        if (
            state.capture not in {"idle", "error"} or (start_pending and action != "disconnect")
        ) and action in blocked_during_capture:
            message = f"{action} is disabled while capture is active"
            self._set_state(error=message)
            self._events.put(ControllerEvent("error", message))
            return
        if action == "start":
            with self._lock:
                if self._start_pending:
                    message = "Capture start is already pending"
                else:
                    self._start_pending = True
                    message = ""
            if message:
                self._set_state(error=message)
                self._events.put(ControllerEvent("error", message))
                return
            self._cancel.clear()
        elif action in {"stop", "disable_output"} or (action == "disconnect" and start_pending):
            self._cancel.set()
        self._commands.put((action, kwargs))

    def poll_events(self) -> list[ControllerEvent]:
        events = []
        while True:
            try:
                event = self._events.get_nowait()
            except queue.Empty:
                break
            if event.kind == "state":
                continue
            events.append(event)
        with self._lock:
            if self._state_event_pending:
                events.append(ControllerEvent("state", self._state))
                self._state_event_pending = False
        return events

    def recent_records(self) -> np.ndarray:
        with self._lock:
            if self._recent_cache is None:
                if not self._recent:
                    return np.empty(0, dtype=SAMPLE_DTYPE)
                records = np.concatenate(tuple(self._recent))
                self._recent_cache = records[-_RECENT_LIMIT:].copy()
            cached = self._recent_cache
            assert cached is not None
            return cached.copy()

    def shutdown(self, timeout: float = 3.0) -> bool:
        if timeout < 0:
            raise ValueError("Shutdown timeout must be nonnegative")
        if not self._shutdown_requested:
            self._shutdown_requested = True
            self._cancel.set()
            self._commands.put(("shutdown", {}))
        self._thread.join(timeout)
        if self._thread.is_alive():
            self._set_state(
                output="unknown",
                error="SDK worker did not stop before the shutdown timeout",
            )
            self._events.put(ControllerEvent("error", self.state.error))
            return False
        return True

    def _worker_main(self) -> None:
        while True:
            action, kwargs = self._commands.get()
            if action == "shutdown":
                self._do_shutdown()
                return
            try:
                getattr(self, f"_do_{action}")(**kwargs)
            except Exception as exc:
                self._handle_command_error(action, exc)
            finally:
                if action == "start":
                    with self._lock:
                        self._start_pending = False

    def _do_scan(self, backend: str = "simulator") -> None:
        self._require_capture_idle("scan")
        factory = self._get_factory(backend)
        devices = factory().scan()
        self._set_state(backend=backend, error="")
        self._events.put(ControllerEvent("devices", devices))

    def _do_connect(self, backend: str = "simulator", serial: str | None = None) -> None:
        self._require_capture_inactive("connect")
        if self.state.connection not in {"disconnected", "error"} or self._backend is not None:
            raise BackendError("Disconnect the current device before connecting")
        factory = self._get_factory(backend)
        self._set_state(
            backend=backend,
            connection="connecting",
            output="unknown",
            device=None,
            error="",
        )
        candidate = factory()
        try:
            device = candidate.connect(serial)
        except Exception:
            try:
                candidate.disconnect()
            except Exception:
                self._backend = candidate
            raise
        self._backend = candidate
        output = "off" if backend == "simulator" else "unknown"
        self._set_state(
            connection="connected",
            output=output,
            capture="idle",
            device=device,
            recording_path=None,
            output_owned=False,
            error="",
        )

    def _do_disconnect(self) -> None:
        self._require_capture_inactive("disconnect")
        state = self.state
        backend = self._backend
        if backend is None:
            self._set_state(
                connection="disconnected",
                output="unknown",
                capture="idle",
                device=None,
                recording_path=None,
                output_owned=False,
                error="",
            )
            return
        if state.output_owned:
            try:
                backend.disable_output()
            except Exception as exc:
                self._set_state(connection="error", output="unknown", error=str(exc))
                raise
            self._set_state(output="off", output_owned=False)
        try:
            backend.disconnect()
        except Exception:
            self._set_state(connection="error", output="unknown")
            raise
        self._backend = None
        self._set_state(
            connection="disconnected",
            output="unknown",
            capture="idle",
            device=None,
            recording_path=None,
            output_owned=False,
            error="",
        )

    def _do_set_voltage(self, voltage_v: float) -> None:
        self._require_capture_idle("set_voltage")
        state = self.state
        self._require_connected()
        if state.output != "off":
            raise BackendError("Explicitly disable output before changing voltage")
        voltage = validate_voltage(voltage_v)
        self._set_state(target_voltage_v=voltage, error="")

    def _do_enable_output(self, confirmed: bool = False) -> None:
        self._require_capture_idle("enable_output")
        state = self.state
        backend = self._require_connected()
        if confirmed is not True:
            raise BackendError("Explicit output confirmation is required")
        if state.output != "off":
            raise BackendError("Output must first be explicitly disabled")
        self._set_state(output_owned=True, error="")
        try:
            backend.set_voltage(state.target_voltage_v)
        except Exception:
            self._set_state(output="unknown")
            raise
        self._set_state(output="on", error="")

    def _do_disable_output(self) -> None:
        self._cancel.set()
        if self.state.capture not in {"idle", "error"}:
            self._finish_capture_requested()
        backend = self._backend
        if backend is None:
            raise BackendError("No device handle is available for output off")
        try:
            # Fault recovery is explicit and uses the existing handle, never a reconnect.
            backend.stop_sampling()
            backend.disable_output()
        except Exception:
            self._set_state(output="unknown")
            raise
        self._set_state(output="off", output_owned=False, error="")

    def _do_start(self, path: Path) -> None:
        if self.state.capture not in {"idle", "error"}:
            raise BackendError("Capture is already active")
        capture_path = Path(path)
        if self._cancel.is_set():
            self._set_state(capture="idle", recording_path=None, error="")
            return
        self._set_state(
            capture="starting",
            recording_path=capture_path,
            sample_count=0,
            error="",
        )
        backend = self._require_connected()
        self._clear_recent()
        writer = None
        batches = None
        complete = False
        started = False
        try:
            state = self.state
            device = state.device
            metadata = {
                "backend": state.backend,
                "device_serial": device.serial if device else "unknown",
                "device_name": device.name if device else "unknown",
                "device_firmware": device.firmware if device else "unknown",
                "target_voltage_v": state.target_voltage_v,
                "output_command_state": state.output,
            }
            if state.backend == "monsoon":
                metadata["sdk_revision"] = SDK_REVISION
            writer = self._writer_factory(capture_path, metadata)
            writer.event("capture_started")
            batches = backend.sample_batches(self._cancel)
            for batch in batches:
                if not started:
                    started = True
                    self._set_state(capture="running")
                writer.append(batch)
                self._append_recent(batch)
                self._set_state(sample_count=writer.sample_count)
                if self._cancel.is_set():
                    break
            self._set_state(capture="stopping")
            if batches is not None:
                batches.close()
                batches = None
            backend.stop_sampling()
            writer.event("capture_stopped")
            complete = True
        except Exception as exc:
            self._mark_communication_uncertain()
            original_message = str(exc)
            if writer is not None:
                try:
                    writer.event("capture_error", {"message": original_message})
                except Exception:
                    pass
            if batches is not None:
                try:
                    batches.close()
                except Exception as close_exc:
                    exc = BackendError(f"{exc}; sample stream close also failed: {close_exc}")
            try:
                backend.stop_sampling()
            except Exception as stop_exc:
                exc = BackendError(f"{exc}; sampling stop also failed: {stop_exc}")
                self._mark_communication_uncertain()
            self._set_state(capture="error", recording_path=None, error=str(exc))
            self._events.put(ControllerEvent("error", str(exc)))
        finally:
            if writer is not None:
                try:
                    writer.close(complete=complete)
                except Exception as exc:
                    complete = False
                    self._set_state(capture="error", recording_path=None, error=str(exc))
                    self._events.put(ControllerEvent("error", str(exc)))
            if complete:
                self._set_state(capture="idle", recording_path=capture_path, error="")
            if writer is not None:
                self._events.put(ControllerEvent("capture_finished", capture_path))

    def _do_stop(self) -> None:
        self._cancel.set()
        if self.state.capture in {"idle", "error"}:
            return
        self._finish_capture_requested()

    def _finish_capture_requested(self) -> None:
        self._cancel.set()
        if self.state.capture not in {"idle", "error"}:
            self._set_state(capture="stopping")

    def _do_shutdown(self) -> None:
        self._cancel.set()
        backend = self._backend
        if backend is None:
            return
        failures = []
        try:
            backend.stop_sampling()
        except Exception as exc:
            failures.append(f"stop failed: {exc}")
        if self.state.output_owned:
            try:
                backend.disable_output()
            except Exception as exc:
                failures.append(f"output off failed: {exc}")
        try:
            backend.disconnect()
        except Exception as exc:
            failures.append(f"close failed: {exc}")
            # Retain a failed USB session and its hardware lease until process
            # exit; do not advertise the hardware as free while STOP/close failed.
        else:
            self._backend = None
        if failures:
            message = "; ".join(failures)
            self._set_state(connection="error", output="unknown", error=message)
            self._events.put(ControllerEvent("error", message))
        else:
            self._set_state(
                connection="disconnected",
                output="unknown",
                device=None,
                output_owned=False,
            )

    def _require_connected(self) -> Backend:
        if self.state.connection != "connected" or self._backend is None:
            raise BackendError("No device is connected")
        return self._backend

    def _get_factory(self, backend: str) -> Callable[[], Backend]:
        try:
            return self._backend_factories[backend]
        except KeyError as exc:
            raise ValueError(f"Unknown backend: {backend}") from exc

    def _require_capture_idle(self, action: str) -> None:
        if self.state.capture != "idle":
            raise BackendError(f"{action} is disabled while capture is active")

    def _require_capture_inactive(self, action: str) -> None:
        if self.state.capture not in {"idle", "error"}:
            raise BackendError(f"{action} is disabled while capture is active")

    def _set_state(self, **changes: Any) -> None:
        with self._lock:
            self._state = replace(self._state, **changes)
            self._state_event_pending = True

    def _handle_command_error(self, action: str, exc: Exception) -> None:
        changes: dict[str, Any] = {"error": str(exc)}
        if action == "connect":
            changes.update(connection="error", output="unknown", device=None)
        elif action == "start":
            changes.update(capture="error", recording_path=None)
        self._set_state(**changes)
        self._events.put(ControllerEvent("error", str(exc)))

    def _mark_communication_uncertain(self) -> None:
        self._set_state(connection="error", output="unknown")

    def _clear_recent(self) -> None:
        with self._lock:
            self._recent.clear()
            self._recent_count = 0
            self._recent_cache = None

    def _append_recent(self, batch: SampleBatch) -> None:
        records = batch.records.copy()
        with self._lock:
            self._recent.append(records)
            self._recent_count += len(records)
            while self._recent and self._recent_count - len(self._recent[0]) >= _RECENT_LIMIT:
                self._recent_count -= len(self._recent.popleft())
            self._recent_cache = None
