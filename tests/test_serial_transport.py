from __future__ import annotations

import time
import unittest

from aha_cli.services.hardware_session import _ThreadedSerialTransport


class _FakeSerial:
    """Stand-in for pyserial.Serial: yields queued RX, records TX."""

    def __init__(self, chunks: list[bytes]) -> None:
        self._inbox = list(chunks)
        self.written: list[bytes] = []
        self.timeout = None

    def read(self, _size: int) -> bytes:
        if self._inbox:
            return self._inbox.pop(0)
        time.sleep(0.02)
        return b""

    def write(self, data: bytes) -> int:
        self.written.append(bytes(data))
        return len(data)

    def close(self) -> None:
        self._inbox.clear()


class ThreadedSerialTransportTests(unittest.TestCase):
    """Exercises the pyserial transport logic that runs identically on Windows."""

    def test_pumps_serial_reads_into_selectable_fileno(self) -> None:
        fake = _FakeSerial([b"hel", b"lo"])
        transport = _ThreadedSerialTransport(fake)
        try:
            deadline = time.monotonic() + 2.0
            received = b""
            while time.monotonic() < deadline and b"hello" not in received:
                chunk = transport.read(100)
                if chunk:
                    received += chunk
                else:
                    time.sleep(0.01)
            self.assertIn(b"hello", received)
        finally:
            transport.close()

    def test_write_goes_to_serial_port(self) -> None:
        fake = _FakeSerial([])
        transport = _ThreadedSerialTransport(fake)
        try:
            self.assertEqual(transport.write(b"world"), 5)
        finally:
            transport.close()
        self.assertEqual(b"".join(fake.written), b"world")

    def test_close_signals_eof(self) -> None:
        transport = _ThreadedSerialTransport(_FakeSerial([]))
        transport.close()
        # After close, the read end reports EOF (b"") once drained.
        deadline = time.monotonic() + 1.0
        result = b"_pending_"
        while time.monotonic() < deadline and result == b"_pending_":
            result = transport.read(100)
            if result is None:
                time.sleep(0.01)
        self.assertEqual(result, b"")


if __name__ == "__main__":
    unittest.main()
