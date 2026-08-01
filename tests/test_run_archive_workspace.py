from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from aha_cli.services.run_archive import _make_workspace_resolver


class WorkspaceResolverTests(unittest.TestCase):
    """P3.6: best-effort workspace_path relocation on cross-machine import."""

    def test_relocates_unique_basename_match(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "proj").mkdir()
            resolve = _make_workspace_resolver([root])
            self.assertEqual(resolve("/__aha_nonexistent__/elsewhere/proj"), str(root / "proj"))

    def test_keeps_existing_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            real = root / "real"
            real.mkdir()
            resolve = _make_workspace_resolver([root])
            self.assertEqual(resolve(str(real)), str(real))

    def test_no_match_leaves_unchanged(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            resolve = _make_workspace_resolver([root])
            self.assertEqual(resolve("/__aha_nonexistent__/nope"), "/__aha_nonexistent__/nope")

    def test_ambiguous_match_leaves_unchanged(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            r1, r2 = root / "a", root / "b"
            r1.mkdir(); r2.mkdir()
            (r1 / "proj").mkdir(); (r2 / "proj").mkdir()
            resolve = _make_workspace_resolver([r1, r2])
            self.assertEqual(resolve("/__aha_nonexistent__/proj"), "/__aha_nonexistent__/proj")

    def test_empty_roots_leaves_unchanged(self) -> None:
        resolve = _make_workspace_resolver([])
        self.assertEqual(resolve("/anything/proj"), "/anything/proj")


if __name__ == "__main__":
    unittest.main()
