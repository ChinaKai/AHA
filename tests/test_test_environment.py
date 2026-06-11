from __future__ import annotations

import os
from pathlib import Path
import tempfile
import unittest

from tests.helpers import AHA_RUNTIME_ENV_KEYS


class TestEnvironmentIsolationTests(unittest.TestCase):
    def test_pytest_clears_inherited_aha_runtime_environment(self) -> None:
        for key in AHA_RUNTIME_ENV_KEYS:
            self.assertNotIn(key, os.environ)

    def test_pytest_uses_temporary_home(self) -> None:
        home = Path.home().resolve()
        tmp_root = Path(tempfile.gettempdir()).resolve()

        self.assertTrue(home == tmp_root or home.is_relative_to(tmp_root))
        self.assertEqual(home.name, "home")


if __name__ == "__main__":
    unittest.main()
