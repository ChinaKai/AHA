from __future__ import annotations

import asyncio
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from aha_cli.services import browser_external
from aha_cli.services.browser_external import (
    BrowserLaunchSession,
    launch_browser_session,
    resolve_user_browser_executable,
)
from aha_cli.services.browser_runtime import BrowserBridgeError


class BrowserExternalTests(unittest.TestCase):
    def test_resolver_prefers_installed_user_browser_and_falls_back(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            chrome = Path(tmp) / "google-chrome"
            chrome.touch()
            chrome.chmod(0o755)
            with mock.patch(
                "aha_cli.services.browser_external.shutil.which",
                side_effect=lambda command: str(chrome) if command == "google-chrome-stable" else None,
            ):
                resolved, product = resolve_user_browser_executable("/missing/chromium")
            self.assertEqual(resolved, chrome.resolve())
            self.assertEqual(product, "Google Chrome")

            with mock.patch("aha_cli.services.browser_external.shutil.which", return_value=None):
                resolved, product = resolve_user_browser_executable(chrome)
            self.assertEqual(resolved, chrome.resolve())
            self.assertEqual(product, "Chromium")

    def test_resolver_reports_missing_browser(self) -> None:
        with mock.patch("aha_cli.services.browser_external.shutil.which", return_value=None):
            with self.assertRaises(BrowserBridgeError) as raised:
                resolve_user_browser_executable("/missing/chromium", platform_name="linux")
        self.assertEqual(raised.exception.code, "user_browser_missing")

    def test_user_browser_proxy_rejects_credentials(self) -> None:
        with self.assertRaises(BrowserBridgeError) as raised:
            browser_external._user_browser_proxy_args(
                {"server": "http://proxy.example:7890", "username": "alice"}
            )
        self.assertEqual(raised.exception.code, "user_browser_proxy_auth_unsupported")
        self.assertEqual(
            browser_external._user_browser_proxy_args(
                {"server": "http://proxy.example:7890", "bypass": "localhost"}
            ),
            [
                "--proxy-server=http://proxy.example:7890",
                "--proxy-bypass-list=localhost",
            ],
        )

    def test_managed_session_uses_playwright_launch_options(self) -> None:
        context = mock.Mock()
        chromium = mock.Mock()
        chromium.launch_persistent_context = mock.AsyncMock(return_value=context)
        playwright = mock.Mock(chromium=chromium)

        session = asyncio.run(
            launch_browser_session(
                playwright,
                Path("/tmp/profile"),
                {"runtime": "playwright", "downloads": "deny"},
                None,
                {"active": "embedded"},
                viewport_width=1280,
                viewport_height=720,
            )
        )

        self.assertEqual(session.runtime, "playwright")
        self.assertIs(session.context, context)
        args, kwargs = chromium.launch_persistent_context.await_args
        self.assertEqual(args, ("/tmp/profile",))
        self.assertTrue(kwargs["headless"])

    def test_managed_session_applies_explicit_mobile_identity_and_touch(self) -> None:
        page = mock.Mock()
        page.set_viewport_size = mock.AsyncMock()
        page.evaluate = mock.AsyncMock(return_value=(
            "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/140.0.7339.82 Safari/537.36"
        ))
        cdp = mock.Mock()
        cdp.send = mock.AsyncMock()
        context = mock.Mock()
        context.new_cdp_session = mock.AsyncMock(return_value=cdp)
        session = BrowserLaunchSession(
            context,
            runtime="playwright",
            product="Playwright Chromium",
        )

        asyncio.run(session.prepare_page(page, width=400, height=720, mobile=True))

        page.set_viewport_size.assert_awaited_once_with({"width": 400, "height": 720})
        context.new_cdp_session.assert_awaited_once_with(page)
        commands = [call.args[0] for call in cdp.send.await_args_list]
        self.assertEqual(commands, [
            "Emulation.setUserAgentOverride",
            "Emulation.setTouchEmulationEnabled",
        ])
        self.assertTrue(cdp.send.await_args_list[0].args[1]["userAgentMetadata"]["mobile"])
        self.assertEqual(
            cdp.send.await_args_list[1].args,
            ("Emulation.setTouchEmulationEnabled", {"enabled": True, "maxTouchPoints": 5}),
        )

    def test_user_chrome_uses_explicit_loopback_cdp_without_automation_flag(self) -> None:
        context = mock.Mock()
        context.pages = []
        browser = mock.Mock()
        browser.contexts = [context]
        browser.on = mock.Mock()
        browser.close = mock.AsyncMock()
        chromium = mock.Mock()
        chromium.executable_path = "/fallback/chromium"
        chromium.connect_over_cdp = mock.AsyncMock(return_value=browser)
        playwright = mock.Mock(chromium=chromium)
        process = mock.Mock()
        process.poll.return_value = None

        with (
            mock.patch(
                "aha_cli.services.browser_external.resolve_user_browser_executable",
                return_value=(Path("/opt/google/chrome"), "Google Chrome"),
            ),
            mock.patch(
                "aha_cli.services.browser_external.browser_native_display_available",
                return_value=True,
            ),
            mock.patch(
                "aha_cli.services.browser_external.browser_native_display_environment",
                return_value={"DISPLAY": ":0"},
            ),
            mock.patch(
                "aha_cli.services.browser_external._reserve_loopback_port",
                return_value=43117,
            ),
            mock.patch(
                "aha_cli.services.browser_external.subprocess.Popen",
                return_value=process,
            ) as popen,
        ):
            session = asyncio.run(
                launch_browser_session(
                    playwright,
                    Path("/tmp/profile"),
                    {"runtime": "user_chrome"},
                    {"server": "http://127.0.0.1:7890"},
                    {"active": "embedded"},
                    viewport_width=1280,
                    viewport_height=720,
                )
            )

        command = popen.call_args.args[0]
        self.assertIn("--remote-debugging-address=127.0.0.1", command)
        self.assertIn("--remote-debugging-port=43117", command)
        self.assertIn("--user-data-dir=/tmp/profile", command)
        self.assertIn("--proxy-server=http://127.0.0.1:7890", command)
        self.assertNotIn("about:blank", command)
        self.assertFalse(any("enable-automation" in item for item in command))
        self.assertFalse(any(item == "--headless" for item in command))
        self.assertEqual(
            chromium.connect_over_cdp.await_args.args,
            ("http://127.0.0.1:43117",),
        )
        self.assertEqual(session.public_state(), {
            "runtime": "user_chrome",
            "browser_product": "Google Chrome",
        })
        process.poll.return_value = 0
        asyncio.run(session.close())

    def test_user_chrome_requires_native_display(self) -> None:
        playwright = mock.Mock()
        with mock.patch(
            "aha_cli.services.browser_external.browser_native_display_available",
            return_value=False,
        ):
            with self.assertRaises(BrowserBridgeError) as raised:
                asyncio.run(
                    launch_browser_session(
                        playwright,
                        Path("/tmp/profile"),
                        {"runtime": "user_chrome"},
                        None,
                        {"active": "embedded"},
                        viewport_width=1280,
                        viewport_height=720,
                    )
                )
        self.assertEqual(raised.exception.code, "user_browser_display_unavailable")

    def test_user_session_resizes_attached_pages(self) -> None:
        page = mock.Mock()
        page.evaluate = mock.AsyncMock(return_value=(
            "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/140.0.7339.82 Safari/537.36"
        ))
        cdp = mock.Mock()
        cdp.send = mock.AsyncMock()
        cdp.detach = mock.AsyncMock()
        context = mock.Mock()
        context.new_cdp_session = mock.AsyncMock(return_value=cdp)
        context.close = mock.AsyncMock()
        session = BrowserLaunchSession(
            context,
            runtime="user_chrome",
            product="Chromium",
        )

        asyncio.run(session.prepare_page(page, width=390, height=640, mobile=True))
        asyncio.run(session.prepare_page(page, width=400, height=700, mobile=True))

        context.new_cdp_session.assert_awaited_once_with(page)
        self.assertEqual(
            cdp.send.await_args_list[0].args,
            (
                "Emulation.setUserAgentOverride",
                {
                    "userAgent": (
                        "Mozilla/5.0 (Linux; Android 13; Pixel 7) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/140.0.7339.82 Mobile Safari/537.36"
                    ),
                    "platform": "Android",
                    "userAgentMetadata": {
                        "brands": [{"brand": "Chromium", "version": "140"}],
                        "fullVersionList": [{
                            "brand": "Chromium",
                            "version": "140.0.7339.82",
                        }],
                        "platform": "Android",
                        "platformVersion": "13.0.0",
                        "architecture": "",
                        "model": "Pixel 7",
                        "mobile": True,
                    },
                },
            ),
        )
        self.assertEqual(
            cdp.send.await_args_list[1].args,
            (
                "Emulation.setDeviceMetricsOverride",
                {
                    "mobile": False,
                    "width": 390,
                    "height": 640,
                    "deviceScaleFactor": 3.0,
                    "screenWidth": 390,
                    "screenHeight": 640,
                },
            ),
        )
        self.assertEqual(
            cdp.send.await_args_list[2].args,
            (
                "Emulation.setTouchEmulationEnabled",
                {"enabled": True, "maxTouchPoints": 5},
            ),
        )
        self.assertEqual(
            cdp.send.await_args_list[4].args,
            (
                "Emulation.setDeviceMetricsOverride",
                {
                    "mobile": False,
                    "width": 400,
                    "height": 700,
                    "deviceScaleFactor": 3.0,
                    "screenWidth": 400,
                    "screenHeight": 700,
                },
            ),
        )
        cdp.detach.assert_not_awaited()
        asyncio.run(session.close())
        cdp.detach.assert_awaited_once_with()
        context.close.assert_awaited_once_with()


if __name__ == "__main__":
    unittest.main()
