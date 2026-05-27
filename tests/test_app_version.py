from __future__ import annotations

import unittest
from unittest import mock

from aha_cli.services.app_version import aha_version


class AppVersionTests(unittest.TestCase):
    def test_aha_version_uses_builtin_value(self) -> None:
        with (
            mock.patch.dict("os.environ", {}, clear=True),
            mock.patch("aha_cli.services.app_version.BUILD_VERSION", "20260527.057e500"),
        ):
            self.assertEqual(aha_version(), "20260527.057e500")

    def test_aha_version_allows_environment_override(self) -> None:
        with (
            mock.patch.dict("os.environ", {"AHA_VERSION": "20260528.override"}, clear=True),
            mock.patch("aha_cli.services.app_version.BUILD_VERSION", "20260527.057e500"),
        ):
            self.assertEqual(aha_version(), "20260528.override")

    def test_aha_version_falls_back_to_source_tree_version(self) -> None:
        with (
            mock.patch.dict("os.environ", {}, clear=True),
            mock.patch("aha_cli.services.app_version.BUILD_VERSION", ""),
            mock.patch("aha_cli.services.app_version._source_tree_version", return_value="source.abcdef0"),
        ):
            self.assertEqual(aha_version(), "source.abcdef0")
