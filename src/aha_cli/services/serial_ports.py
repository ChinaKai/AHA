"""Cross-platform serial port detection for the hardware-debug UI.

Populates the serial-device dropdown. Prefers ``pyserial`` (rich descriptions,
VID/PID) when installed; falls back to stdlib enumeration so the dropdown works
with zero extra dependencies:

* POSIX: ``/dev/ttyUSB*``, ``/dev/ttyACM*``, ``/dev/ttyS*``, and macOS
  ``/dev/tty.usb*``.
* Windows: the ``HKLM\\HARDWARE\\DEVICEMAP\\SERIALCOMM`` registry key, which the
  kernel populates with active ``COMx`` names.

All functions are best-effort and never raise: a broken probe yields an empty
list rather than aborting the hardware panel.
"""
from __future__ import annotations

import sys
from pathlib import Path

_WIN = sys.platform == "win32"


def list_serial_ports() -> list[dict]:
    """Detected serial ports as ``{"device", "description", "hwid"}`` dicts."""
    try:
        ports = _via_pyserial()
        if ports:
            return ports
    except Exception:
        pass
    try:
        return _via_stdlib()
    except Exception:
        return []


def _via_pyserial() -> list[dict]:
    from serial.tools import list_ports  # type: ignore

    ports: list[dict] = []
    for info in list_ports.comports():
        ports.append(
            {
                "device": info.device,
                "description": info.description or info.device,
                "hwid": getattr(info, "hwid", "") or "",
            }
        )
    return ports


def _via_stdlib() -> list[dict]:
    if _WIN:
        return _windows_registry_ports()
    return _posix_dev_ports()


def _posix_dev_ports() -> list[dict]:
    ports: list[dict] = []
    seen: set[str] = set()
    for pattern in ("ttyUSB*", "ttyACM*", "tty.usbserial*", "tty.usbmodem*", "ttyS*"):
        for path in Path("/dev").glob(pattern):
            device = str(path)
            if device in seen:
                continue
            seen.add(device)
            ports.append({"device": device, "description": path.name, "hwid": ""})
    ports.sort(key=lambda item: item["device"])
    return ports


def _windows_registry_ports() -> list[dict]:
    import winreg

    ports: list[dict] = []
    try:
        key = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"HARDWARE\DEVICEMAP\SERIALCOMM")
    except OSError:
        return ports
    try:
        index = 0
        while True:
            try:
                _name, value, _type = winreg.EnumValue(key, index)
            except OSError:
                break
            index += 1
            if isinstance(value, str) and value:
                ports.append({"device": value, "description": value, "hwid": ""})
    finally:
        winreg.CloseKey(key)
    ports.sort(key=lambda item: _natural_key(item["device"]))
    return ports


def _natural_key(value: str) -> list:
    """Sort COM1, COM2, ..., COM10 naturally (not COM1, COM10, COM2)."""
    parts: list = []
    run = ""
    digit = False
    for ch in value:
        if ch.isdigit() is digit:
            run += ch
        else:
            if run:
                parts.append(int(run) if digit else run)
            run = ch
            digit = ch.isdigit()
    if run:
        parts.append(int(run) if digit else run)
    return parts
