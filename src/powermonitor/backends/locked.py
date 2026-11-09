"""Apply the same hardware lease to GUI scans and connected USB sessions."""

from __future__ import annotations

from typing import Any

from powermonitor.backends.sampling import HostCleanupMonsoonBackend
from powermonitor.hardware_lock import HardwareLease
from powermonitor.models import DeviceInfo


class LockedMonsoonBackend(HostCleanupMonsoonBackend):
    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._lease: HardwareLease | None = None

    def scan(self) -> list[DeviceInfo]:
        if self._lease is not None:
            return super().scan()
        with HardwareLease():
            return super().scan()

    def connect(self, serial: str | None = None) -> DeviceInfo:
        if self._lease is not None:
            raise RuntimeError("Disconnect before acquiring another device session")
        lease = HardwareLease()
        lease.acquire()
        self._lease = lease
        try:
            devices = super().scan()
            if serial is None:
                if len(devices) != 1:
                    raise RuntimeError(
                        "Select --serial when zero or multiple HVPM devices are found"
                    )
                serial = devices[0].serial
            elif serial not in {device.serial for device in devices}:
                raise RuntimeError(f"HVPM device not found: {serial}")
            return super().connect(serial)
        except BaseException:
            # A failed disposal retains both device and lease until disconnect.
            if self._device is None:
                self._lease = None
                lease.release()
            raise

    def disconnect(self) -> None:
        super().disconnect()
        lease, self._lease = self._lease, None
        if lease is not None:
            lease.release()
