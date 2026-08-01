"""Launch a user-visible Chrome process and attach the Browser Bridge over CDP."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import socket
import subprocess
import sys
import time

from aha_cli.services.browser_runtime import (
    BrowserBridgeError,
    browser_capture_scale,
    browser_context_launch_options,
    browser_native_display_available,
    browser_native_display_environment,
)
from aha_cli import process_control

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
    channel: str | None = None,
    platform_name: str | None = None,
    environ: Mapping[str, str] | None = None,
) -> tuple[Path, str]:
    candidates = _known_user_browser_candidates(
        platform_name=platform_name,
        environ=environ,
    )
    wanted = str(channel or "auto").strip().lower()
    wanted_product = {"chrome": "Google Chrome", "msedge": "Microsoft Edge"}.get(wanted)
    # Honor an explicit browser choice first: "daily" mode must launch the browser
    # the user selected (Edge daily -> real Edge), not always the Chrome-first default.
    if wanted_product:
        for candidate, product in candidates:
            if product != wanted_product:
                continue
            path = Path(candidate).expanduser()
            if path.is_file() and os.access(path, os.X_OK):
                return path.resolve(), product
    for candidate, product in candidates:
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


def detect_installed_browser_channel(
    *,
    platform_name: str | None = None,
    environ: Mapping[str, str] | None = None,
) -> str | None:
    """Return a Playwright channel for an installed Chrome/Edge, else ``None``.

    ``None`` means "use Playwright's bundled Chromium". Prefers Chrome, then Edge,
    so on a typical Windows machine (Edge always present, Chrome common) it picks
    Chrome when available and otherwise Edge — no ``playwright install chromium``
    needed.
    """
    for _candidate, product in _known_user_browser_candidates(
        platform_name=platform_name,
        environ=environ,
    ):
        if product == "Google Chrome":
            return "chrome"
        if product == "Microsoft Edge":
            return "msedge"
    return None


def resolve_browser_channel(browser_config: dict | None) -> str | None:
    """Resolve the Playwright channel: explicit config override, else auto-detect.

    ``channel`` values: ``chrome`` / ``msedge`` (pin to that browser);
    ``chromium`` / ``bundled`` (force Playwright's bundled Chromium);
    ``auto`` or unset (detect an installed Chrome/Edge, fall back to bundled).
    """
    explicit = str((browser_config or {}).get("channel") or "auto").strip().lower()
    if explicit in {"chrome", "msedge"}:
        return explicit
    if explicit in {"chromium", "bundled"}:
        return None
    return detect_installed_browser_channel()


_AVAILABLE_CHANNELS_CACHE: dict = {"value": None, "fetched_at": 0.0}
_AVAILABLE_CHANNELS_TTL_SECONDS = 60.0


def _chromium_executable_available() -> bool:
    spec = importlib.util.find_spec("playwright")
    locations = list(spec.submodule_search_locations or []) if spec is not None else []
    if not locations:
        return False
    try:
        package_root = Path(locations[0]).resolve()
        browser_manifest = json.loads(
            (package_root / "driver" / "package" / "browsers.json").read_text(encoding="utf-8")
        )
        chromium = next(
            item
            for item in browser_manifest.get("browsers", [])
            if item.get("name") == "chromium"
        )
        revision = str(chromium.get("revision") or "").strip()
        if not revision:
            return False
        configured_root = str(os.environ.get("PLAYWRIGHT_BROWSERS_PATH") or "").strip()
        if configured_root == "0":
            registry_root = package_root / "driver" / "package" / ".local-browsers"
        elif configured_root:
            registry_root = Path(configured_root).expanduser()
            if not registry_root.is_absolute():
                registry_root = Path(os.environ.get("INIT_CWD") or Path.cwd()) / registry_root
        elif sys.platform == "win32":
            registry_root = Path(os.environ.get("LOCALAPPDATA") or (Path.home() / "AppData" / "Local")) / "ms-playwright"
        elif sys.platform == "darwin":
            registry_root = Path.home() / "Library" / "Caches" / "ms-playwright"
        else:
            registry_root = Path(os.environ.get("XDG_CACHE_HOME") or (Path.home() / ".cache")) / "ms-playwright"
        browser_dirs = [registry_root / f"chromium-{revision}"]
        browser_dirs.extend(registry_root.glob(f"chromium_*-{revision}"))
        if sys.platform == "win32":
            suffixes = (("chrome-win64", "chrome.exe"), ("chrome-win", "chrome.exe"))
        elif sys.platform == "darwin":
            suffixes = (
                ("chrome-mac-x64", "Google Chrome for Testing.app", "Contents", "MacOS", "Google Chrome for Testing"),
                ("chrome-mac-arm64", "Google Chrome for Testing.app", "Contents", "MacOS", "Google Chrome for Testing"),
            )
        else:
            suffixes = (("chrome-linux64", "chrome"), ("chrome-linux", "chrome"))
        return any((browser_dir.joinpath(*suffix)).is_file() for browser_dir in browser_dirs for suffix in suffixes)
    except (OSError, ValueError, StopIteration, TypeError):
        return False


def available_browser_channels() -> dict:
    """Detect which shared-browser channels are usable on this host.

    Returns ``{chrome, msedge, chromium}`` booleans so the browser picker can be
    populated from what is actually installed instead of a fixed list.
    """
    now = time.monotonic()
    cached = _AVAILABLE_CHANNELS_CACHE["value"]
    if cached is not None and now - _AVAILABLE_CHANNELS_CACHE["fetched_at"] < _AVAILABLE_CHANNELS_TTL_SECONDS:
        return cached
    chrome = False
    msedge = False
    for candidate, product in _known_user_browser_candidates():
        try:
            if Path(candidate).expanduser().is_file():
                if product == "Google Chrome":
                    chrome = True
                elif product == "Microsoft Edge":
                    msedge = True
        except Exception:
            continue
    value = {
        "chrome": chrome,
        "msedge": msedge,
        "chromium": _chromium_executable_available(),
    }
    _AVAILABLE_CHANNELS_CACHE["value"] = value
    _AVAILABLE_CHANNELS_CACHE["fetched_at"] = now
    return value


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
    ) -> tuple[int, int] | None:
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
            if mobile:
                # Mobile keeps emulated metrics (rare path; avoids the 980px layout
                # fallback on pages without a viewport meta tag).
                device_metrics = {
                    "mobile": False,
                    "width": width,
                    "height": height,
                    "deviceScaleFactor": browser_capture_scale(mobile),
                    "screenWidth": width,
                    "screenHeight": height,
                }
                await session.send("Emulation.setDeviceMetricsOverride", device_metrics)
                measured = None
            else:
                # Desktop: Emulation.setDeviceMetricsOverride freezes page.screenshot()
                # on a CDP-attached real browser (the mirror stays on the first frame).
                # Skip it and use the window's real viewport; return the measured CSS
                # size so the reported frame and mouse coordinates stay accurate.
                try:
                    measured = await page.evaluate("() => [window.innerWidth, window.innerHeight]")
                    measured = (max(1, int(measured[0] or 1)), max(1, int(measured[1] or 1)))
                except Exception:
                    measured = None
        else:
            measured = None
        await session.send(
            "Emulation.setTouchEmulationEnabled",
            {"enabled": mobile, "maxTouchPoints": 5 if mobile else 1},
        )
        return measured

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
        channel=browser_config.get("channel"),
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
        # Keep the shared window painting even when occluded/non-foreground so
        # CDP page.screenshot() stays live (otherwise Windows stops compositing
        # the occluded window and the mirror freezes on the first frame).
        "--disable-backgrounding-occluded-windows",
        "--disable-features=CalculateNativeWinOcclusion",
        f"--window-size={viewport_width},{viewport_height + 150}",
        *_user_browser_proxy_args(proxy_options),
    ]
    process = subprocess.Popen(
        args,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        env=environment,
        preexec_fn=process_control.parent_death_preexec(),
    )
    process_control.assign_parent_death(process)
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
    channel = resolve_browser_channel(browser_config)
    if channel:
        options["channel"] = channel
    context = await playwright.chromium.launch_persistent_context(
        str(profile_path),
        **options,
    )
    product = {"chrome": "Google Chrome", "msedge": "Microsoft Edge"}.get(channel, "Playwright Chromium")
    return BrowserLaunchSession(
        context,
        runtime="playwright",
        product=product,
    )


__all__ = [
    "BrowserLaunchSession",
    "detect_installed_browser_channel",
    "launch_browser_session",
    "resolve_browser_channel",
    "resolve_user_browser_executable",
]
