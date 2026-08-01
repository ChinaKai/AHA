from __future__ import annotations

import unittest

from aha_cli.services.serial_ports import list_serial_ports


class SerialPortsTests(unittest.TestCase):
    def test_returns_list_without_raising(self) -> None:
        ports = list_serial_ports()
        self.assertIsInstance(ports, list)
        for port in ports:
            self.assertIsInstance(port, dict)
            self.assertIn("device", port)
            self.assertIsInstance(port["device"], str)


if __name__ == "__main__":
    unittest.main()
