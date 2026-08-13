from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from aha_cli.services.wsl_backend import (
    _parse_probe_output,
    cached_wsl_backends,
    cache_wsl_backends,
    probe_wsl_backends,
    wsl_backends_cache_path,
    wsl_backends_for_workspace,
)


class WslBackendTests(unittest.TestCase):
    def test_parse_probe_output(self) -> None:
        sample = (
            "codex=/home/kaikai/.nvm/versions/node/v24.18.0/bin/codex\n"
            "claude=/home/kaikai/.local/bin/claude\n"
            "node=/home/kaikai/.nvm/versions/node/v24.18.0/bin/node\n"
        )
        parsed = _parse_probe_output(sample)
        self.assertEqual(parsed["codex"], "/home/kaikai/.nvm/versions/node/v24.18.0/bin/codex")
        self.assertEqual(parsed["claude"], "/home/kaikai/.local/bin/claude")
        self.assertIn("node", parsed)

    def test_parse_probe_output_drops_empty(self) -> None:
        self.assertEqual(_parse_probe_output("codex=\nclaude=NOT_FOUND\n"), {})

    def test_cache_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cache_wsl_backends(root, "Ubuntu-24.04", {"codex": "/x/codex", "claude": "/x/claude"})

            cached = cached_wsl_backends(root, "Ubuntu-24.04")
            self.assertEqual(cached["codex"], "/x/codex")
            self.assertIsNone(cached_wsl_backends(root, "OtherDistro"))

    def test_wsl_backends_for_workspace_probes_and_caches(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch(
                "aha_cli.services.wsl_backend.probe_wsl_backends",
                return_value={"codex": "/x/codex", "claude": "/x/claude", "node": "/x/node"},
            ) as probe:
                result = wsl_backends_for_workspace(root, "Ubuntu-24.04")
                probe.assert_called_once()
            self.assertEqual(result["codex"], "/x/codex")

            # Second call uses cache, no re-probe.
            with mock.patch("aha_cli.services.wsl_backend.probe_wsl_backends") as probe:
                result2 = wsl_backends_for_workspace(root, "Ubuntu-24.04")
                probe.assert_not_called()
            self.assertEqual(result2, result)

    def test_wsl_backends_for_workspace_empty_on_probe_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch("aha_cli.services.wsl_backend.probe_wsl_backends", return_value={}):
                result = wsl_backends_for_workspace(root, "Ubuntu-24.04")
            self.assertEqual(result, {})

    def test_cache_path_under_aha_home(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.assertEqual(wsl_backends_cache_path(root), root / ".aha" / "wsl-backends.json")


if __name__ == "__main__":
    unittest.main()
