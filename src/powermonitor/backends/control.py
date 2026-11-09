"""Explicit HVPM controls with configured-voltage readback, never inferred Vout."""

from __future__ import annotations

import math
from typing import Any

from powermonitor._vendor.pymonsoon import Operations
from powermonitor.backends.base import BackendError
from powermonitor.backends.monsoon import MonsoonBackend
from powermonitor.backends.sampling import HostCleanupMonsoonBackend
from powermonitor.models import validate_voltage


class ControlMonsoonBackend(HostCleanupMonsoonBackend):
    def configured_voltage(self) -> float:
        device = self._require_device()
        try:
            raw = device.Protocol.getValue(Operations.OpCodes.setMainVoltage, 4)
            voltage = float(raw) / Operations.Conversion.FLOAT_TO_INT
        except Exception as exc:
            raise BackendError(f"Unable to read configured voltage: {exc}") from exc
        if not math.isfinite(voltage) or not (voltage == 0 or 0.795 <= voltage <= 13.505):
            raise BackendError("Unrecognized configured-voltage readback; output state unknown")
        return voltage

    def read_status(self) -> dict[str, Any]:
        device = self._require_device()
        try:
            device.fillStatusPacket()
            packet = device.statusPacket
            return {
                "serial": str(packet.serialNumber),
                "firmware": str(packet.firmwareVersion),
                "protocol_version": int(packet.protocolVersion),
                "configured_voltage_v": self.configured_voltage(),
                "output_state": "unknown",
                "output_state_note": (
                    "Configured voltage is not measured output or protection state"
                ),
                "powerup_current_limit_a": float(packet.powerupCurrentLimit),
                "runtime_current_limit_a": float(packet.runtimeCurrentLimit),
                "usb_passthrough_mode": int(packet.usbPassthroughMode),
            }
        except BackendError:
            raise
        except Exception as exc:
            raise BackendError(f"Unable to read HVPM status: {exc}") from exc

    def set_voltage(self, voltage_v: float) -> None:
        voltage = validate_voltage(voltage_v)
        if self.configured_voltage() != 0:
            raise BackendError("Output is not configured OFF; explicitly run output off first")
        MonsoonBackend.set_voltage(self, voltage)
        self._verify_voltage(voltage)

    def disable_output(self) -> None:
        MonsoonBackend.disable_output(self)
        self._verify_voltage(0.0)

    def _verify_voltage(self, expected: float) -> None:
        if abs(self.configured_voltage() - expected) > 0.005:
            raise BackendError("Output command readback mismatch; actual output state unknown")
