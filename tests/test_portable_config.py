from __future__ import annotations

import os
import unittest

from aha_cli.store.config import _apply_portable_overrides


class PortableConfigTests(unittest.TestCase):
    """P3.4: machine-local config paths resolve ~/$VAR tokens and env overrides."""

    def setUp(self) -> None:
        # Snapshot and clear the override env vars so tests are deterministic.
        self._saved = {k: os.environ.pop(k, None) for k in ("AHA_WORKSPACE_ROOTS", "AHA_CODEX_BIN", "AHA_CLAUDE_BIN")}

    def tearDown(self) -> None:
        for key, value in self._saved.items():
            if value is not None:
                os.environ[key] = value
            else:
                os.environ.pop(key, None)

    def test_workspace_roots_expand_env_token(self) -> None:
        os.environ["AHA_TEST_WS"] = "/opt/proj"
        try:
            cfg = {
                "workspace_roots": ["$AHA_TEST_WS", "/abs/path"],
                "codex": {"bin": "codex"},
                "claude": {"bin": "claude"},
            }
            _apply_portable_overrides(cfg)
            self.assertEqual(cfg["workspace_roots"], ["/opt/proj", "/abs/path"])
            self.assertEqual(cfg["codex"]["bin"], "codex")  # bare name passthrough
        finally:
            del os.environ["AHA_TEST_WS"]

    def test_codex_bin_env_override(self) -> None:
        os.environ["AHA_CODEX_BIN"] = "/custom/bin/codex"
        cfg = {"workspace_roots": [], "codex": {"bin": "codex"}, "claude": {"bin": "claude"}}
        _apply_portable_overrides(cfg)
        self.assertEqual(cfg["codex"]["bin"], "/custom/bin/codex")
        self.assertEqual(cfg["claude"]["bin"], "claude")  # untouched

    def test_workspace_roots_env_override_splits_on_pathsep(self) -> None:
        os.environ["AHA_WORKSPACE_ROOTS"] = f"/a{os.pathsep}/b"
        cfg = {"workspace_roots": ["/old"], "codex": {"bin": "codex"}, "claude": {"bin": "claude"}}
        _apply_portable_overrides(cfg)
        self.assertEqual(cfg["workspace_roots"], ["/a", "/b"])


if __name__ == "__main__":
    unittest.main()
