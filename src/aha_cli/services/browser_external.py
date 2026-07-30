"""Launch a user-visible Chrome process and attach the Browser Bridge over CDP."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping
import os
from pathlib import Path
import re
import shutil
import socket
import subprocess
import sys

from aha_cli.services.browser_runtime import (
    BrowserBridgeError,
    browser_capture_scale,
    browser_context_launch_options,
    browser_native_display_available,
    browser_native_display_environment,
)
from aha_cli.services.hardware_bridge import set_parent_death_signal

_CONNECT_TIMEOUT_SECONDS = 12.0
_CDP_DETACH_TIMEOUT_SECONDS = 2.0
_BROWSER_CLOSE_TIMEOUT_SECONDS = 3.0


def _mobile_user_agent(value: str) -> str:
    user_agent = re.sub(
        r"\([^)]*\)",
        "(Linux; Android 13; Pixel 7)",
        str(value or ""),
        count=1,
    )
    if " Mobile " not in user_agent:
        user_agent = user_agent.replace(" Safari/", " Mobile Safari/")
    return user_agent


def _mobile_user_agent_metadata(value: str, product: str) -> dict:
    match = re.search(r"(?:Chrome|Chromium|Edg)/([0-9.]+)", str(value or ""))
    version = match.group(1) if match else "1.0.0.0"
    major = version.split(".", 1)[0]
    brand = (
        "Google Chrome"
        if product == "Google Chrome"
        else "Microsoft Edge"
        if product == "Microsoft Edge"
        else "Chromium"
    )
    brands = [{"brand": "Chromium", "version": major}]
    full_versions = [{"brand": "Chromium", "version": version}]
    if brand != "Chromium":
        brands.append({"brand": brand, "version": major})
        full_versions.append({"brand": brand, "version": version})
    return {
        "brands": brands,
        "fullVersionList": full_versions,
        "platform": "Android",
        "platformVersion": "13.0.0",
        "architecture": "",
        "model": "Pixel 7",
        "mobile": True,
    }


def _known_user_browser_candidates(
    *,
    platform_name: str | None = None,
    environ: Mapping[str, str] | None = None,
) -> list[tuple[str, str]]:
    platform_value = str(platform_name or sys.platform)
    environment = environ if environ is not None else os.environ
    candidates: list[tuple[str, str]] = []
    for command, product in (
        ("google-chrome-stable", "Google Chrome"),
        ("google-chrome", "Google Chrome"),
        ("chromium", "Chromium"),
        ("chromium-browser", "Chromium"),
        ("microsoft-edge-stable", "Microsoft Edge"),
        ("microsoft-edge", "Microsoft Edge"),
    ):
        resolved = shutil.which(command)
        if resolved:
            candidates.append((resolved, product))
    if platform_value == "darwin":
        candidates.extend(
            [
                ("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome", "Google Chrome"),
                ("/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge", "Microsoft Edge"),
            ]
        )
    if platform_value.startswith("win"):
        for base_key in ("PROGRAMFILES", "PROGRAMFILES(X86)", "LOCALAPPDATA"):
            base = str(environment.get(base_key) or "").strip()
            if not base:
                continue
            candidates.extend(
                [
                    (str(Path(base) / "Google/Chrome/Application/chrome.exe"), "Google Chrome"),
                    (str(Path(base) / "Microsoft/Edge/Application/msedge.exe"), "Microsoft Edge"),
                ]
            )
    return candidates


def resolve_user_browser_executable(
    playwright_chromium_path: object = "",
    *,
    platform_name: str | None = None,
    environ: Mapping[str, str] | None = None,
) -> tuple[Path, str]:
    for candidate, product in _known_user_browser_candidates(
        platform_name=platform_name,
        environ=environ,
    ):
        path = Path(candidate).expanduser()
        if path.is_file() and os.access(path, os.X_OK):
            return path.resolve(), product
    fallback = Path(str(playwright_chromium_path or "")).expanduser()
    if fallback.is_file() and os.access(fallback, os.X_OK):
        return fallback.resolve(), "Chromium"
    raise BrowserBridgeError(
        "user_browser_missing",
        "No local Chrome/Chromium executable is available. Install Google Chrome or Playwright Chromium.",
    )


def _reserve_loopback_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def _user_browser_proxy_args(proxy_options: dict | None) -> list[str]:
    if not proxy_options:
        return []
    if proxy_options.get("username") or proxy_options.get("password"):
        raise BrowserBridgeError(
            "user_browser_proxy_auth_unsupported",
            "User Chrome mode does not support proxy credentials in launch arguments.",
        )
    args = [f'--proxy-server={str(proxy_options.get("server") or "")}']
    bypass = str(proxy_options.get("bypass") or "").strip()
    if bypass:
        args.append(f"--proxy-bypass-list={bypass}")
    return args


async def _stop_process(process: subprocess.Popen) -> None:
    if process.poll() is not None:
        return
    process.terminate()
    try:
        await asyncio.to_thread(process.wait, 3)
    except subprocess.TimeoutExpired:
        process.kill()
        await asyncio.to_thread(process.wait, 3)


class BrowserLaunchSession:
    def __init__(
        self,
        context,
        *,
        runtime: str,
        product: str,
        browser=None,
        process: subprocess.Popen | None = None,
    ) -> None:
        self.context = context
        self.runtime = runtime
        self.product = product
        self.browser = browser
        self.process = process
        self.cdp_sessions: dict[int, tuple[object, object]] = {}
        self.desktop_user_agent = ""

    @property
    def user_visible(self) -> bool:
        return self.runtime == "user_chrome"

    def public_state(self) -> dict:
        return {"runtime": self.runtime, "browser_product": self.product}

    def on_close(self, callback: Callable[[], object]) -> None:
        if self.browser is not None:
            self.browser.on("disconnected", lambda *_args: callback())
        else:
            self.context.on("close", lambda _context: callback())

    async def prepare_page(
        self,
        page,
        *,
        width: int,
        height: int,
        mobile: bool | None = None,
    ) -> None:
        if self.runtime != "user_chrome":
            await page.set_viewport_size({"width": width, "height": height})
        identity = id(page)
        stored = self.cdp_sessions.get(identity)
        if stored is None or stored[0] is not page:
            stored = (page, await self.context.new_cdp_session(page))
            self.cdp_sessions[identity] = stored
        session = stored[1]
        mobile = width <= 640 if mobile is None else bool(mobile)
        if not self.desktop_user_agent:
            self.desktop_user_agent = str(await page.evaluate("navigator.userAgent"))
        await session.send(
            "Emulation.setUserAgentOverride",
            (
                {
                    "userAgent": _mobile_user_agent(self.desktop_user_agent),
                    "platform": "Android",
                    "userAgentMetadata": _mobile_user_agent_metadata(
                        self.desktop_user_agent,
                        self.product,
                    ),
                }
                if mobile
                else {"userAgent": ""}
            ),
        )
        if self.runtime == "user_chrome":
            device_metrics = {
                # A raw CDP mobile viewport falls back to a 980px layout on pages
                # without a viewport meta tag. Mobile identity and touch are applied
                # separately, while fixed CSS metrics keep the shared frame exact.
                "mobile": False,
                "width": width,
                "height": height,
                "deviceScaleFactor": browser_capture_scale(mobile),
                "screenWidth": width,
                "screenHeight": height,
            }
            await session.send("Emulation.setDeviceMetricsOverride", device_metrics)
        await session.send(
            "Emulation.setTouchEmulationEnabled",
            {"enabled": mobile, "maxTouchPoints": 5 if mobile else 1},
        )

    async def close(self) -> None:
        sessions = [session for _page, session in self.cdp_sessions.values()]
        if sessions:
            try:
                await asyncio.wait_for(
                    asyncio.gather(
                        *(session.detach() for session in sessions),
                        return_exceptions=True,
                    ),
                    timeout=_CDP_DETACH_TIMEOUT_SECONDS,
                )
            except asyncio.TimeoutError:
                pass
        self.cdp_sessions.clear()
        if self.browser is None:
            await asyncio.wait_for(
                self.context.close(),
                timeout=_BROWSER_CLOSE_TIMEOUT_SECONDS,
            )
            return
        try:
            await asyncio.wait_for(
                self.browser.close(),
                timeout=_BROWSER_CLOSE_TIMEOUT_SECONDS,
            )
        except asyncio.TimeoutError:
            pass
        finally:
            if self.process is not None:
                await _stop_process(self.process)
                self.process = None


async def _launch_user_chrome(
    playwright,
    profile_path: Path,
    browser_config: dict,
    proxy_options: dict | None,
    *,
    viewport_width: int,
    viewport_height: int,
) -> BrowserLaunchSession:
    if not browser_native_display_available():
        raise BrowserBridgeError(
            "user_browser_display_unavailable",
            "User Chrome mode requires a desktop display on the AHA host.",
        )
    executable, product = resolve_user_browser_executable(
        playwright.chromium.executable_path,
    )
    port = _reserve_loopback_port()
    environment = dict(os.environ)
    environment.update(browser_native_display_environment(environ=environment))
    args = [
        str(executable),
        f"--user-data-dir={profile_path}",
        f"--remote-debugging-port={port}",
        "--remote-debugging-address=127.0.0.1",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-dev-shm-usage",
        "--disable-session-crashed-bubble",
        f"--window-size={viewport_width},{viewport_height + 150}",
        *_user_browser_proxy_args(proxy_options),
    ]
    process = subprocess.Popen(
        args,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        env=environment,
        preexec_fn=set_parent_death_signal if not sys.platform.startswith("win") else None,
    )
    browser = None
    deadline = asyncio.get_running_loop().time() + _CONNECT_TIMEOUT_SECONDS
    try:
        while asyncio.get_running_loop().time() < deadline:
            if process.poll() is not None:
                raise BrowserBridgeError(
                    "user_browser_start_failed",
                    f"{product} exited before its local debugging endpoint became ready.",
                )
            try:
                browser = await playwright.chromium.connect_over_cdp(
                    f"http://127.0.0.1:{port}",
                    timeout=500,
                )
                break
            except Exception:
                await asyncio.sleep(0.1)
        if browser is None:
            raise BrowserBridgeError(
                "user_browser_connect_timeout",
                f"Timed out connecting to the local {product} debugging endpoint.",
            )
        if not browser.contexts:
            raise BrowserBridgeError(
                "user_browser_context_missing",
                f"{product} did not expose its default browser context.",
            )
        return BrowserLaunchSession(
            browser.contexts[0],
            runtime="user_chrome",
            product=product,
            browser=browser,
            process=process,
        )
    except Exception:
        if browser is not None:
            try:
                await browser.close()
            except Exception:
                pass
        await _stop_process(process)
        raise


async def launch_browser_session(
    playwright,
    profile_path: Path,
    browser_config: dict,
    proxy_options: dict | None,
    display_status: dict,
    *,
    viewport_width: int,
    viewport_height: int,
) -> BrowserLaunchSession:
    if browser_config.get("runtime") == "user_chrome":
        return await _launch_user_chrome(
            playwright,
            profile_path,
            browser_config,
            proxy_options,
            viewport_width=viewport_width,
            viewport_height=viewport_height,
        )
    options = browser_context_launch_options(
        browser_config,
        proxy_options,
        display_status=display_status,
        viewport_width=viewport_width,
        viewport_height=viewport_height,
    )
    context = await playwright.chromium.launch_persistent_context(
        str(profile_path),
        **options,
    )
    return BrowserLaunchSession(
        context,
        runtime="playwright",
        product="Playwright Chromium",
    )


__all__ = [
    "BrowserLaunchSession",
    "launch_browser_session",
    "resolve_user_browser_executable",
]
