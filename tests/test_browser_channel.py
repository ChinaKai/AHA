from __future__ import annotations

from pathlib import Path
import tempfile
import unittest
from unittest import mock

from aha_cli.services.browser_external import (
    _chromium_executable_available,
    detect_installed_browser_channel,
    resolve_browser_channel,
)


class BrowserChannelTests(unittest.TestCase):
    def test_bundled_chromium_detection_reads_playwright_registry_without_starting_driver(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            package_root = root / "playwright"
            manifest = package_root / "driver" / "package" / "browsers.json"
            manifest.parent.mkdir(parents=True)
            manifest.write_text(
                '{"browsers":[{"name":"chromium","revision":"1234"}]}',
                encoding="utf-8",
            )
            executable = root / "browsers" / "chromium-1234" / "chrome-linux64" / "chrome"
            executable.parent.mkdir(parents=True)
            executable.write_bytes(b"browser")
            spec = mock.Mock(submodule_search_locations=[str(package_root)])
            with (
                mock.patch("aha_cli.services.browser_external.importlib.util.find_spec", return_value=spec),
                mock.patch("aha_cli.services.browser_external.sys.platform", "linux"),
                mock.patch.dict("os.environ", {"PLAYWRIGHT_BROWSERS_PATH": str(root / "browsers")}, clear=False),
            ):
                self.assertTrue(_chromium_executable_available())

    def test_detect_prefers_chrome_over_edge(self) -> None:
        with mock.patch(
            "aha_cli.services.browser_external._known_user_browser_candidates",
            return_value=[("/p/chrome", "Google Chrome"), ("/p/edge", "Microsoft Edge")],
        ):
            self.assertEqual(detect_installed_browser_channel(), "chrome")

    def test_detect_edge_when_no_chrome(self) -> None:
        with mock.patch(
            "aha_cli.services.browser_external._known_user_browser_candidates",
            return_value=[("/p/edge", "Microsoft Edge")],
        ):
            self.assertEqual(detect_installed_browser_channel(), "msedge")

    def test_detect_none_when_nothing_installed(self) -> None:
        with mock.patch(
            "aha_cli.services.browser_external._known_user_browser_candidates",
            return_value=[],
        ):
            self.assertIsNone(detect_installed_browser_channel())

    def test_resolve_explicit_pins(self) -> None:
        self.assertEqual(resolve_browser_channel({"channel": "chrome"}), "chrome")
        self.assertEqual(resolve_browser_channel({"channel": "msedge"}), "msedge")
        self.assertIsNone(resolve_browser_channel({"channel": "chromium"}))

    def test_resolve_auto_detects(self) -> None:
        with mock.patch(
            "aha_cli.services.browser_external.detect_installed_browser_channel",
            return_value="msedge",
        ):
            self.assertEqual(resolve_browser_channel({}), "msedge")
            self.assertEqual(resolve_browser_channel({"channel": "auto"}), "msedge")

    def test_resolve_auto_falls_back_to_bundled(self) -> None:
        with mock.patch(
            "aha_cli.services.browser_external.detect_installed_browser_channel",
            return_value=None,
        ):
            self.assertIsNone(resolve_browser_channel({}))


if __name__ == "__main__":
    unittest.main()
