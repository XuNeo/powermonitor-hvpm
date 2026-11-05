"""Sampling-only HVPM adapter: no output control and no USB reset on cleanup."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from powermonitor._vendor.pymonsoon import HVPM, Operations, pmapi, sampleEngine
from powermonitor.backends.base import BackendError
from powermonitor.backends.monsoon import MonsoonBackend, verify_sdk_revision


class _SamplingDevice(HVPM.Monsoon):
    def setup_usb(self, serialno: str | None = None, Protocol: Any = None) -> None:
        if Protocol is not None:
            raise BackendError("Sampling requires a fresh, internal USB protocol")
        # Retain a fresh protocol before Connect: even a partial failure must be
        # cleaned up without the SDK's shared default protocol or close/reset.
        self.Protocol = pmapi.USB_protocol()
        self.Protocol.Connect(Operations.HardwareModel.HVPM, serialno)


class SamplingOnlyMonsoonBackend(MonsoonBackend):
    """Reuse the pinned sampler but forbid power commands and reset cleanup."""

    def __init__(
        self,
        *,
        device_factory: Callable[[], Any] | None = None,
        engine_factory: Callable[[Any], Any] | None = None,
    ) -> None:
        verify_sdk_revision(sampleEngine.SampleEngine, HVPM.Monsoon)
        if (device_factory is None) != (engine_factory is None):
            raise ValueError("Mock factories must be supplied together")
        super().__init__(
            device_factory=device_factory or _SamplingDevice,
            engine_factory=engine_factory or sampleEngine.SampleEngine,
            reset_on_close=False,
        )

    def _close_device(self, device: Any) -> None:
        protocol = getattr(device, "Protocol", None)
        if protocol is None:
            return
        handle = getattr(protocol, "DEVICE", None)
        if handle is None:
            return
        from usb.util import dispose_resources

        dispose_resources(handle)
        # Retain the handle until disposal succeeds so disconnect can be retried.
        protocol.DEVICE = None

    def _best_effort_close(self, device: Any) -> None:
        try:
            self._close_device(device)
        except Exception:
            # Preserve the original connection error and allow cleanup to retry.
            self._device = device

    def disconnect(self) -> None:
        if self._sampling or self._engine is not None:
            raise BackendError("Cannot disconnect while sampling; stop sampling first")
        super().disconnect()

    def set_voltage(self, voltage_v: float) -> None:
        raise BackendError("Output voltage control is forbidden in the sampling backend")

    def disable_output(self) -> None:
        raise BackendError("Output switching is forbidden in the sampling backend")
