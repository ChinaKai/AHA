from __future__ import annotations

import asyncio
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from aha_cli.web.server import run_ui_server


class WebServerLifecycleTests(unittest.TestCase):
    def test_start_failure_reaps_bridges_and_managed_processes(self) -> None:
        calls: list[str] = []
        with tempfile.TemporaryDirectory() as tmp, mock.patch(
            "aha_cli.web.server.asyncio.start_server",
            new=mock.AsyncMock(side_effect=OSError("bind failed")),
        ), mock.patch(
            "aha_cli.web.server.stop_all_hardware_bridges",
            side_effect=lambda _root: calls.append("serial"),
        ), mock.patch(
            "aha_cli.web.server.stop_all_network_terminals",
            side_effect=lambda _root: calls.append("network"),
        ), mock.patch(
            "aha_cli.web.server.stop_all_managed_processes",
            side_effect=lambda _root: calls.append("managed"),
        ), mock.patch(
            "aha_cli.web.server.write_service_runtime"
        ) as write_runtime:
            with self.assertRaisesRegex(OSError, "bind failed"):
                asyncio.run(run_ui_server(Path(tmp), "", "127.0.0.1", 8766, 1000))

        self.assertCountEqual(calls, ["serial", "network", "managed"])
        self.assertEqual(write_runtime.call_args_list[0].kwargs["status"], "starting")
        self.assertEqual(write_runtime.call_args_list[-1].kwargs["status"], "stopped")


if __name__ == "__main__":
    unittest.main()
