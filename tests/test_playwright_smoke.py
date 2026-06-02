from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
SMOKE_PLAYWRIGHT_UI = REPO_ROOT / "scripts" / "smoke_playwright_ui.py"


class PlaywrightSmokeTests(unittest.TestCase):
    def test_playwright_smoke_can_skip_without_writing_runs(self) -> None:
        with tempfile.TemporaryDirectory(prefix="aha-playwright-smoke-test-") as tmp:
            tmp_path = Path(tmp)
            env = os.environ.copy()
            env.pop("AHA_HOME", None)
            env.pop("AHA_RUN_ID", None)
            env["HOME"] = str(tmp_path / "home")
            env["XDG_CONFIG_HOME"] = str(tmp_path / "config")
            env["AHA_PLAYWRIGHT_SKIP"] = "1"
            result = subprocess.run(
                [sys.executable, str(SMOKE_PLAYWRIGHT_UI), "--json"],
                cwd=REPO_ROOT,
                env=env,
                capture_output=True,
                text=True,
                check=False,
                timeout=10,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            payload = json.loads(result.stdout)
            self.assertEqual(payload["status"], "skipped")
            self.assertEqual(payload["reason"], "AHA_PLAYWRIGHT_SKIP=1")
            self.assertFalse((tmp_path / "home" / ".aha" / "runs").exists())


if __name__ == "__main__":
    unittest.main()
