"""Task-scoped shared Playwright browser runtime over a local Unix socket."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
from pathlib import Path
import signal
import time
import uuid

from aha_cli.domain.models import utc_now
from aha_cli.services import browser_external
from aha_cli.services import browser_runtime as browser_client
from aha_cli.services.browser_actions import (
    NATIVE_USER_ACTIVITY_SCRIPT,
    browser_mouse_action,
    browser_page_accepts_text_input,
    browser_tabs,
    browser_url_allowed,
    validated_browser_ref,
)
from aha_cli.services.browser_io import append_browser_io_record
from aha_cli.services.browser_snapshot import (
    SNAPSHOT_ELEMENT_LIMIT,
    SNAPSHOT_SCRIPT,
    SNAPSHOT_TEXT_LIMIT,
)

_READ_ACTIONS = {"status", "subscribe", "tabs", "snapshot", "screenshot"}
_MAX_FRAME_BYTES = browser_client.MAX_BROWSER_FRAME_BYTES
_MAX_WRITE_BUFFER_BYTES = 2 * 1024 * 1024
_FRAME_INTERVAL_SECONDS = 0.15
_FRAME_IDLE_INTERVAL_SECONDS = 0.75
_DEFAULT_VIEWPORT = (1280, 720)
_FRAME_JPEG_QUALITY = 70
_USER_PREEMPT_SECONDS = 2.0
_FOCUS_WINDOW_DEBOUNCE_SECONDS = 2.5
_BRIDGE_START_TIMEOUT_SECONDS = 12.0
_BRIDGE_IDLE_TIMEOUT_SECONDS = 30 * 60
_INTERNAL_NEW_TAB_URLS = {
    "about:blank",
    "chrome://newtab",
    "chrome://new-tab-page",
    "chrome-search://local-ntp/local-ntp.html",
}
BrowserBridgeError = browser_client.BrowserBridgeError
browser_artifacts_dir = browser_client.browser_artifacts_dir
browser_bridge_socket_path = browser_client.browser_bridge_socket_path
browser_bridge_state_path = browser_client.browser_bridge_state_path
browser_bridge_status = browser_client.browser_bridge_status
browser_runtime_dir = browser_client.browser_runtime_dir
read_browser_bridge_state = browser_client.read_browser_bridge_state
_task_browser_config = browser_client.task_browser_config
_task_browser_active = browser_client.task_browser_active

def ensure_browser_bridge(
    root: Path,
    run_id: str,
    task_id: str,
    *,
    launcher: list[str] | None = None,
    parent_bound: bool = False,
) -> dict:
    return browser_client.ensure_browser_bridge(
        root,
        run_id,
        task_id,
        launcher=launcher,
        parent_bound=parent_bound,
    )

async def open_browser_bridge_ipc(
    root: Path,
    run_id: str,
    task_id: str,
    *,
    ensure: bool = True,
    parent_bound: bool = False,
    timeout: float = _BRIDGE_START_TIMEOUT_SECONDS,
) -> tuple[asyncio.StreamReader, asyncio.StreamWriter, dict]:
    return await browser_client.open_browser_bridge_ipc(
        root,
        run_id,
        task_id,
        ensure=ensure,
        parent_bound=parent_bound,
        timeout=timeout,
    )


async def browser_bridge_request(
    root: Path,
    run_id: str,
    task_id: str,
    action: str,
    *,
    args: dict | None = None,
    source: str = "agent",
    agent_id: str = "main",
    timeout: float = 30.0,
) -> dict:
    return await browser_client.browser_bridge_request(
        root,
        run_id,
        task_id,
        action,
        args=args,
        source=source,
        agent_id=agent_id,
        timeout=timeout,
    )


async def browser_doctor() -> dict:
    return await browser_client.browser_doctor()


async def _read_frame(reader: asyncio.StreamReader) -> dict | None:
    return await browser_client.read_browser_frame(reader)


async def _write_frame(writer: asyncio.StreamWriter, payload: dict) -> None:
    await browser_client.write_browser_frame(writer, payload)

class BrowserBridgeDaemon:
    def __init__(
        self,
        root: Path,
        run_id: str,
        task_id: str,
        *,
        frame_interval: float = _FRAME_INTERVAL_SECONDS,
        reap_interval: float = 8.0,
    ) -> None:
        self.root = root
        self.run_id = run_id
        self.task_id = task_id
        self.instance_id = uuid.uuid4().hex
        self.frame_interval = max(0.15, float(frame_interval))
        self.reap_interval = max(1.0, float(reap_interval))
        self.stop_event = asyncio.Event()
        self.context = None
        self.playwright = None
        self.server: asyncio.AbstractServer | None = None
        self.pages: dict[str, object] = {}
        self.page_ids: dict[int, str] = {}
        self.active_page_id = ""
        self.page_sequence = 0
        self.revision = 0
        self.control_epoch = 0
        self.viewport_width, self.viewport_height = _DEFAULT_VIEWPORT
        self.mobile_emulation = False
        self.user_active_until = self.last_native_activity = self.last_focus_window_at = 0.0
        self.bridge_mutation_depth = 0
        self.action_lock = asyncio.Lock()
        self.frame_wake_event = asyncio.Event()
        self.subscribers: set[asyncio.StreamWriter] = set()
        self.frame_subscriber_epoch = 0
        self.profile_lease = None
        self.browser_session = None
        self.proxy_signature = ""
        self.proxy_active = False
        self.launch_signature = ""
        self.display_status = {
            "requested": "native",
            "active": "embedded",
            "native_available": False,
            "fallback": True,
            "fallback_reason": "native_display_unavailable",
        }
        self._tasks: set[asyncio.Task] = set()
        self.last_activity = time.monotonic()
    def _spawn(self, coroutine) -> None:
        task = asyncio.create_task(coroutine)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
    def _config(self) -> tuple[dict, dict]:
        return _task_browser_config(self.root, self.run_id, self.task_id)

    def _write_state(self, status: str, *, error: str = "", error_code: str = "") -> None:
        path = browser_bridge_state_path(self.root, self.run_id, self.task_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        config = self._config()[1]
        state = {
            "run_id": self.run_id,
            "task_id": self.task_id,
            "instance_id": self.instance_id,
            "pid": os.getpid(),
            "status": status,
            "updated_at": utc_now(),
            "profile": self.profile_lease.mode if self.profile_lease else config.get("profile"),
            "profile_name": self.profile_lease.name if self.profile_lease else config.get("profile_name"),
            "profile_configured": config.get("profile"),
            "profile_name_configured": config.get("profile_name"),
            **(self.browser_session.public_state() if self.browser_session else {"runtime": config.get("runtime")}),
            "display": dict(self.display_status),
            "page_count": len(self.pages),
            "active_page_id": self.active_page_id,
            "revision": self.revision,
            "control_epoch": self.control_epoch,
        }
        if error:
            state["error"] = error
            state["error_code"] = error_code or "browser_error"
        path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")

    async def run(self) -> int:
        os.environ.update(browser_client.browser_native_display_environment())
        runtime_dir = browser_runtime_dir(self.root, self.run_id, self.task_id)
        runtime_dir.mkdir(parents=True, exist_ok=True)
        socket_path = browser_bridge_socket_path(self.root, self.run_id, self.task_id)
        try:
            if socket_path.exists():
                socket_path.unlink()
        except OSError:
            pass
        try:
            initial_config = self._config()[1]
            self.display_status = browser_client.browser_display_status(initial_config)
        except Exception:
            pass
        self._write_state("starting")
        loop = asyncio.get_running_loop()
        for signum in (signal.SIGTERM, signal.SIGINT):
            try:
                loop.add_signal_handler(signum, self.stop_event.set)
            except (NotImplementedError, RuntimeError):
                pass
        try:
            from playwright.async_api import async_playwright
        except ImportError:
            message = "Python Playwright is not installed. Install it and run `python3 -m playwright install chromium`."
            self._write_state("error", error=message, error_code="playwright_missing")
            print(message, flush=True)
            return 2
        try:
            task, config = self._config()
            self.viewport_width, self.viewport_height, self.mobile_emulation = browser_client.browser_initial_viewport(config)
            if not _task_browser_active(task, config):
                raise BrowserBridgeError("browser_disabled", "Browser control is not active for this task.")
            self.playwright = await async_playwright().start()
            self.profile_lease = browser_client.acquire_browser_profile(
                self.root,
                self.run_id,
                self.task_id,
                config,
            )
            proxy_options = browser_client.browser_proxy_launch_options(
                self.root,
                self.run_id,
                task,
                config,
            )
            self.display_status = browser_client.browser_display_status(config)
            self.proxy_signature = browser_client.browser_proxy_signature(proxy_options)
            self.proxy_active = bool(proxy_options)
            self.launch_signature = browser_client.browser_launch_signature(
                config,
                proxy_options,
                display_status=self.display_status,
            )
            self.browser_session = await browser_external.launch_browser_session(
                self.playwright,
                self.profile_lease.path,
                config,
                proxy_options,
                self.display_status,
                viewport_width=self.viewport_width,
                viewport_height=self.viewport_height,
            )
            self.context = self.browser_session.context
            self.browser_session.on_close(self.stop_event.set)
            if self.display_status.get("active") == "native" or self.browser_session.user_visible:
                await self.context.expose_binding(
                    "__ahaNativeUserActivity",
                    self._native_user_activity,
                )
                await self.context.add_init_script(NATIVE_USER_ACTIVITY_SCRIPT)
            await self.context.route("**/*", self._route_request)
            self.context.on("page", lambda page: self._spawn(self._register_page(page)))
            for page in list(self.context.pages):
                await self._register_page(page)
            if not self.pages:
                await self._register_page(await self.context.new_page())
            self.server = await asyncio.start_unix_server(
                self._handle_client,
                path=str(socket_path),
                limit=_MAX_FRAME_BYTES,
            )
            os.chmod(socket_path, 0o600)
            self._write_state("running")
            self._spawn(self._frame_loop())
            self._spawn(self._reap_loop())
            for page in list(self.pages.values()):
                if self._should_open_start_url(str(page.url or "")):
                    self._spawn(self._navigate_to_start_url(page, config))
            await self._broadcast_state()
            await self.stop_event.wait()
            return 0
        except BrowserBridgeError as exc:
            self._write_state("error", error=str(exc), error_code=exc.code)
            print(str(exc), flush=True)
            return 2
        except Exception as exc:
            self._write_state("error", error=str(exc), error_code="browser_start_failed")
            print(f"Browser bridge failed: {exc}", flush=True)
            return 2
        finally:
            await self._close()

    async def _route_request(self, route, request) -> None:
        try:
            is_navigation = bool(request.is_navigation_request())
        except Exception:
            is_navigation = False
        if is_navigation and not self._url_allowed(str(request.url or ""), self._config()[1]):
            await route.abort("blockedbyclient")
            return
        await route.continue_()

    @staticmethod
    def _url_allowed(url: str, config: dict) -> bool:
        return browser_url_allowed(url, config)

    @staticmethod
    def _should_open_start_url(url: str) -> bool:
        raw = str(url or "").strip().lower()
        if not raw:
            return True
        base = raw.split("#", 1)[0].split("?", 1)[0].rstrip("/")
        return base in _INTERNAL_NEW_TAB_URLS

    async def _register_page(self, page) -> str:
        if self.browser_session is not None:
            await self.browser_session.prepare_page(
                page, width=self.viewport_width, height=self.viewport_height, mobile=self.mobile_emulation)
        identity = id(page)
        existing = self.page_ids.get(identity)
        if existing:
            self.active_page_id = existing
            return existing
        self.page_sequence += 1
        page_id = f"page-{self.page_sequence:03d}"
        self.pages[page_id] = page
        self.page_ids[identity] = page_id
        self.active_page_id = page_id
        page.on("close", lambda: self._spawn(self._page_closed(page_id, identity)))
        page.on(
            "framenavigated",
            lambda frame: self._spawn(self._page_navigated(page_id))
            if frame == page.main_frame
            else None,
        )
        page.on("dialog", lambda dialog: self._spawn(self._dismiss_dialog(dialog)))
        page.on("download", lambda download: self._spawn(self._handle_download(download)))
        self.revision += 1
        self.frame_wake_event.set()
        await self._broadcast_state()
        return page_id

    async def _page_closed(self, page_id: str, identity: int) -> None:
        if not self._remove_page(page_id, identity):
            return
        self.revision += 1
        self.frame_wake_event.set()
        self._write_state("running")
        await self._broadcast_state()

    def _remove_page(self, page_id: str, identity: int) -> bool:
        removed_page = self.pages.pop(page_id, None)
        removed_id = self.page_ids.pop(identity, None)
        if removed_page is None and removed_id is None:
            return False
        if self.active_page_id == page_id:
            self.active_page_id = next(iter(self.pages), "")
        return True

    async def _navigate_to_start_url(self, page, config: dict) -> None:
        start_url = str(config.get("start_url") or "").strip()
        if not start_url or start_url == "about:blank":
            return
        if not self._url_allowed(start_url, config):
            raise BrowserBridgeError(
                "navigation_blocked",
                f"Navigation is not allowed: {start_url}",
            )
        try:
            await page.goto(start_url, wait_until="domcontentloaded", timeout=15000)
        except Exception as exc:
            await self._broadcast(
                {
                    "type": "event",
                    "event": "navigation_error",
                    "message": f"Unable to open the browser start URL: {str(exc)[:500]}",
                }
            )

    async def _page_navigated(self, page_id: str) -> None:
        page = self.pages.get(page_id)
        if page is not None and self.browser_session is not None:
            await self.browser_session.prepare_page(
                page, width=self.viewport_width, height=self.viewport_height, mobile=self.mobile_emulation)
        if page_id in self.pages:
            self.active_page_id = page_id
        self.revision += 1
        self.frame_wake_event.set()
        self._write_state("running")
        await self._broadcast_state()
    async def _native_user_activity(self, _source, _event_type: object = "") -> None:
        if (
            self.display_status.get("active") != "native" and not self.browser_session.user_visible
            or self.bridge_mutation_depth
        ):
            return
        now = time.monotonic()
        self.user_active_until = now + _USER_PREEMPT_SECONDS
        self.last_activity = now
        if now - self.last_native_activity < 0.2:
            return
        self.last_native_activity = now
        self.control_epoch += 1
        self._write_state("running")
        await self._broadcast_state()
    async def _dismiss_dialog(self, dialog) -> None:
        try:
            await dialog.dismiss()
        except Exception:
            pass
        await self._broadcast(
            {
                "type": "event",
                "event": "dialog_dismissed",
                "message": "A page dialog was dismissed by the shared browser runtime.",
            }
        )
    async def _handle_download(self, download) -> None:
        config = self._config()[1]
        if config.get("downloads") != "allow":
            try:
                await download.cancel()
            except Exception:
                pass
            await self._broadcast(
                {
                    "type": "event",
                    "event": "download_blocked",
                    "message": "Download blocked by task browser policy.",
                }
            )
    def _active_page(self):
        page = self.pages.get(self.active_page_id)
        if page is not None:
            return page
        if self.pages:
            self.active_page_id = next(iter(self.pages))
            return self.pages[self.active_page_id]
        return None

    async def _tabs(self) -> list[dict]:
        return await browser_tabs(self.pages, self.active_page_id)

    async def _status_payload(self) -> dict:
        _task, config = self._config()
        page = self._active_page()
        title = ""
        if page is not None:
            try:
                title = await page.title()
            except Exception:
                pass
        frame_width, frame_height = browser_client.browser_frame_size(
            self.viewport_width,
            self.viewport_height,
            mobile=self.mobile_emulation,
        )
        return {
            "run_id": self.run_id,
            "task_id": self.task_id,
            "instance_id": self.instance_id,
            "status": "running",
            "mode": config.get("mode"),
            "agent_access": config.get("agent_access"),
            "profile": self.profile_lease.mode if self.profile_lease else config.get("profile"),
            "profile_name": self.profile_lease.name if self.profile_lease else config.get("profile_name"),
            "profile_configured": config.get("profile"),
            "profile_name_configured": config.get("profile_name"),
            **self.browser_session.public_state(),
            "display": dict(self.display_status),
            "downloads": config.get("downloads"),
            "uploads": config.get("uploads"),
            "allowed_hosts": list(config.get("allowed_hosts") or []),
            "page_id": self.active_page_id,
            "url": str(page.url or "")[:2048] if page is not None else "",
            "title": title[:500],
            "revision": self.revision,
            "control_epoch": self.control_epoch,
            "user_active": time.monotonic() < self.user_active_until,
            "viewport": {"width": self.viewport_width, "height": self.viewport_height},
            "device_mode": "mobile" if self.mobile_emulation else "desktop",
            "mobile_emulation": self.mobile_emulation,
            "frame_size": {"width": frame_width, "height": frame_height},
            "proxy": {
                "mode": config.get("proxy_mode") or "direct",
                "active": self.proxy_active,
                "configured": bool(config.get("proxy_server"))
                or (config.get("proxy_mode") == "inherit" and self.proxy_active),
            },
            "tabs": await self._tabs(),
        }

    async def _broadcast_state(self) -> None:
        if not self.subscribers:
            return
        await self._broadcast({"type": "event", "event": "state", "state": await self._status_payload()})

    async def _broadcast(self, payload: dict) -> None:
        if not self.subscribers:
            return
        encoded = (json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")
        stale: list[asyncio.StreamWriter] = []
        for writer in list(self.subscribers):
            transport = writer.transport
            if transport is None or transport.is_closing():
                stale.append(writer)
                continue
            if transport.get_write_buffer_size() + len(encoded) > _MAX_WRITE_BUFFER_BYTES:
                stale.append(writer)
                continue
            writer.write(encoded)
        for writer in stale:
            self.subscribers.discard(writer)
            writer.close()

    async def _frame_loop(self) -> None:
        last_digest = ""
        last_page_id = ""
        last_subscriber_epoch = -1
        unchanged_frames = 0
        while not self.stop_event.is_set():
            if not self.subscribers:
                self.frame_wake_event.clear()
                if self.subscribers:
                    continue
                try:
                    await asyncio.wait_for(
                        self.frame_wake_event.wait(),
                        timeout=_FRAME_IDLE_INTERVAL_SECONDS,
                    )
                except asyncio.TimeoutError:
                    pass
                continue
            self.frame_wake_event.clear()
            try:
                page = self._active_page()
                page_id = self.active_page_id
                if page is None:
                    unchanged_frames += 1
                else:
                    image_width, image_height = browser_client.browser_frame_size(
                        self.viewport_width,
                        self.viewport_height,
                        mobile=self.mobile_emulation,
                    )
                    data = await page.screenshot(
                        type="jpeg",
                        quality=_FRAME_JPEG_QUALITY,
                        full_page=False,
                    )
                    if page is not self._active_page() or page_id != self.active_page_id:
                        self.frame_wake_event.set()
                        continue
                    digest = hashlib.sha256(data).hexdigest()
                    if (
                        page_id != last_page_id
                        or digest != last_digest
                        or self.frame_subscriber_epoch != last_subscriber_epoch
                    ):
                        last_digest = digest
                        last_page_id = page_id
                        last_subscriber_epoch = self.frame_subscriber_epoch
                        unchanged_frames = 0
                        await self._broadcast(
                            {
                                "type": "event",
                                "event": "frame",
                                "instance_id": self.instance_id,
                                "page_id": page_id,
                                "url": str(page.url or "")[:2048],
                                "revision": self.revision,
                                "mime": "image/jpeg",
                                "width": self.viewport_width,
                                "height": self.viewport_height,
                                "image_width": image_width,
                                "image_height": image_height,
                                "data": base64.b64encode(data).decode("ascii"),
                            }
                        )
                    else:
                        unchanged_frames += 1
            except Exception as exc:
                unchanged_frames += 1
                await self._broadcast({"type": "event", "event": "frame_error", "message": str(exc)[:1000]})
            delay = min(
                _FRAME_IDLE_INTERVAL_SECONDS,
                self.frame_interval * (2 ** min(unchanged_frames // 4, 3)),
            )
            if not self.frame_wake_event.is_set():
                try:
                    await asyncio.wait_for(self.frame_wake_event.wait(), timeout=delay)
                except asyncio.TimeoutError:
                    pass

    async def _reap_loop(self) -> None:
        while not self.stop_event.is_set():
            await asyncio.sleep(self.reap_interval)
            try:
                task, config = self._config()
                proxy_options = browser_client.browser_proxy_launch_options(
                    self.root,
                    self.run_id,
                    task,
                    config,
                )
            except Exception:
                self.stop_event.set()
                return
            if not _task_browser_active(task, config):
                self.stop_event.set()
                return
            display_status = browser_client.browser_display_status(config)
            launch_signature = browser_client.browser_launch_signature(
                config,
                proxy_options,
                display_status=display_status,
            )
            if launch_signature != self.launch_signature:
                self.stop_event.set()
                return
            if not self.subscribers and time.monotonic() - self.last_activity >= _BRIDGE_IDLE_TIMEOUT_SECONDS:
                self.stop_event.set()
                return

    async def _handle_client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            await _write_frame(writer, {"type": "ready", "protocol": 1, "state": await self._status_payload()})
            while not self.stop_event.is_set():
                payload = await _read_frame(reader)
                if payload is None:
                    break
                if payload.get("type") != "command":
                    await self._send_error(writer, str(payload.get("id") or ""), "invalid_frame", "Expected a command frame.")
                    continue
                await self._handle_command(writer, payload)
        except (ConnectionError, BrokenPipeError, asyncio.IncompleteReadError):
            pass
        except BrowserBridgeError as exc:
            try:
                await self._send_error(writer, "", exc.code, str(exc))
            except Exception:
                pass
        finally:
            self.subscribers.discard(writer)
            writer.close()
            try:
                await writer.wait_closed()
            except (ConnectionError, BrokenPipeError):
                pass

    async def _handle_command(self, writer: asyncio.StreamWriter, payload: dict) -> None:
        request_id = str(payload.get("id") or "")
        action = str(payload.get("action") or "").strip().lower().replace("-", "_")
        args = payload.get("args") if isinstance(payload.get("args"), dict) else {}
        source = "user" if str(payload.get("source") or "").strip().lower() == "user" else "agent"
        agent_id = str(payload.get("agent_id") or "main")
        self.last_activity = time.monotonic()
        if action == "subscribe":
            if writer not in self.subscribers:
                self.subscribers.add(writer)
                self.frame_subscriber_epoch += 1
            self.frame_wake_event.set()
        try:
            result = await self._execute(action, args, source=source, agent_id=agent_id)
        except BrowserBridgeError as exc:
            await self._send_error(writer, request_id, exc.code, str(exc))
            return
        except Exception as exc:
            await self._send_error(writer, request_id, "browser_action_failed", str(exc))
            return
        await _write_frame(writer, {"type": "result", "id": request_id, "ok": True, "result": result})

    @staticmethod
    async def _send_error(
        writer: asyncio.StreamWriter,
        request_id: str,
        code: str,
        message: str,
    ) -> None:
        await _write_frame(
            writer,
            {
                "type": "result",
                "id": request_id,
                "ok": False,
                "error": {"code": code, "message": message},
            },
        )

    async def _execute(self, action: str, args: dict, *, source: str, agent_id: str) -> dict:
        if action not in _READ_ACTIONS and action not in {
            "navigate",
            "click",
            "fill",
            "press",
            "back",
            "forward",
            "reload",
            "new_tab",
            "close_tab",
            "select_tab",
            "focus_window",
            "mouse",
            "text",
        }:
            raise BrowserBridgeError("unknown_action", f"Unknown browser action: {action or '(empty)'}")
        task, config = self._config()
        if not _task_browser_active(task, config):
            raise BrowserBridgeError("browser_disabled", "Browser control is not active for this task.")
        mutating = action not in _READ_ACTIONS
        agent_control_epoch = self.control_epoch
        page_before = self._active_page()
        url_before = str(page_before.url or "") if page_before is not None else ""
        page_id_before = self.active_page_id
        if mutating and source == "agent" and config.get("agent_access") != "read_write":
            self._append_action_audit(
                action,
                source=source,
                agent_id=agent_id,
                status="error",
                error="read_only",
                url_before=url_before,
                page_id_before=page_id_before,
            )
            raise BrowserBridgeError("read_only", "Task browser permission is read-only for agents.")
        now = time.monotonic()
        if mutating and source == "agent" and now < self.user_active_until:
            self._append_action_audit(
                action,
                source=source,
                agent_id=agent_id,
                status="error",
                error="control_preempted",
                url_before=url_before,
                page_id_before=page_id_before,
            )
            raise BrowserBridgeError("control_preempted", "The user is actively controlling the shared browser.")
        if mutating and source == "user":
            self.control_epoch += 1
            self.user_active_until = now + _USER_PREEMPT_SECONDS
        try:
            async with self.action_lock:
                if (
                    mutating
                    and source == "agent"
                    and (
                        self.control_epoch != agent_control_epoch
                        or time.monotonic() < self.user_active_until
                    )
                ):
                    raise BrowserBridgeError(
                        "control_preempted",
                        "The user took control before the browser action could run.",
                    )
                if mutating:
                    self.bridge_mutation_depth += 1
                try:
                    result = await self._execute_locked(action, args, config)
                finally:
                    if mutating:
                        self.bridge_mutation_depth -= 1
            status = "ok"
            error = ""
        except Exception as exc:
            status = "error"
            error = exc.code if isinstance(exc, BrowserBridgeError) else type(exc).__name__
            if isinstance(exc, BrowserBridgeError):
                raise
            raise BrowserBridgeError("browser_action_failed", str(exc)) from exc
        finally:
            if mutating:
                self._append_action_audit(
                    action,
                    source=source,
                    agent_id=agent_id,
                    status=locals().get("status", "error"),
                    error=locals().get("error", ""),
                    url_before=url_before,
                    page_id_before=page_id_before,
                )
        if mutating:
            if action != "focus_window":
                self.frame_wake_event.set()
            self._write_state("running")
            await self._broadcast_state()
        return result

    def _append_action_audit(
        self,
        action: str,
        *,
        source: str,
        agent_id: str,
        status: str,
        error: str,
        url_before: str,
        page_id_before: str,
    ) -> None:
        page = self._active_page()
        try:
            append_browser_io_record(
                self.root,
                self.run_id,
                self.task_id,
                {
                    "agent_id": agent_id,
                    "source": source,
                    "action": action,
                    "status": status,
                    "error": error,
                    "page_id": self.active_page_id or page_id_before,
                    "url_before": url_before,
                    "url_after": str(page.url or "") if page is not None else "",
                    "revision": self.revision,
                },
            )
        except (Exception, SystemExit):
            # Auditing must not change the browser action result or leak page
            # details through a secondary storage error.
            pass

    async def _execute_locked(self, action: str, args: dict, config: dict) -> dict:
        if action in {"status", "subscribe"}:
            return await self._status_payload()
        if action == "tabs":
            return {"tabs": await self._tabs(), "revision": self.revision}
        page = self._active_page()
        if action == "new_tab":
            url = str(args.get("url") or config.get("start_url") or "about:blank").strip()
            if not self._url_allowed(url, config):
                raise BrowserBridgeError("navigation_blocked", f"Navigation is not allowed: {url}")
            page = await self.context.new_page()
            page_id = await self._register_page(page)
            if url != "about:blank":
                await page.goto(url, wait_until="domcontentloaded", timeout=30000)
            self.revision += 1
            return {"page_id": page_id, "url": str(page.url or ""), "revision": self.revision}
        if page is None:
            raise BrowserBridgeError("no_page", "The browser has no open page.")
        if action == "snapshot":
            snapshot = await page.evaluate(
                SNAPSHOT_SCRIPT,
                {"textLimit": SNAPSHOT_TEXT_LIMIT, "elementLimit": SNAPSHOT_ELEMENT_LIMIT},
            )
            elements = snapshot.get("elements") if isinstance(snapshot, dict) else []
            for element in elements or []:
                element["ref"] = f"{self.revision}:{element.get('ref')}"
            return {
                "page_id": self.active_page_id,
                "url": str(page.url or "")[:2048],
                "title": (await page.title())[:500],
                "revision": self.revision,
                "text": str((snapshot or {}).get("text") or ""),
                "elements": elements or [],
            }
        if action == "screenshot":
            image_type = "jpeg" if str(args.get("type") or "png").lower() in {"jpg", "jpeg"} else "png"
            kwargs = {"type": image_type, "full_page": bool(args.get("full_page"))}
            if image_type == "jpeg":
                kwargs["quality"] = max(1, min(int(args.get("quality") or 80), 100))
            data = await page.screenshot(**kwargs)
            if len(data) > (_MAX_FRAME_BYTES * 3 // 4) - 4096:
                raise BrowserBridgeError(
                    "screenshot_too_large",
                    "Screenshot exceeds the browser IPC limit; retry without --full-page or use JPEG.",
                )
            return {
                "page_id": self.active_page_id,
                "url": str(page.url or "")[:2048],
                "revision": self.revision,
                "instance_id": self.instance_id,
                "mime": f"image/{image_type}",
                "data": base64.b64encode(data).decode("ascii"),
            }
        accepts_text_input: bool | None = None
        if action == "navigate":
            url = str(args.get("url") or "").strip()
            if not self._url_allowed(url, config):
                raise BrowserBridgeError("navigation_blocked", f"Navigation is not allowed: {url or '(empty)'}")
            await page.goto(url, wait_until="domcontentloaded", timeout=30000)
        elif action == "click":
            ref = validated_browser_ref(args.get("ref"), self.revision)
            await page.locator(f'[data-aha-browser-ref="{ref}"]').first.click(timeout=15000)
        elif action == "fill":
            ref = validated_browser_ref(args.get("ref"), self.revision)
            await page.locator(f'[data-aha-browser-ref="{ref}"]').first.fill(str(args.get("text") or ""), timeout=15000)
        elif action == "press":
            await page.keyboard.press(str(args.get("key") or ""))
        elif action == "back":
            await page.go_back(wait_until="domcontentloaded", timeout=30000)
        elif action == "forward":
            await page.go_forward(wait_until="domcontentloaded", timeout=30000)
        elif action == "reload":
            await page.reload(wait_until="domcontentloaded", timeout=30000)
        elif action == "select_tab":
            page_id = str(args.get("page_id") or "")
            selected = self.pages.get(page_id)
            if selected is None:
                raise BrowserBridgeError("page_not_found", f"Browser page not found: {page_id}")
            self.active_page_id = page_id
            page = selected
            await page.bring_to_front()
        elif action == "focus_window":
            now = time.monotonic()
            if now - self.last_focus_window_at >= _FOCUS_WINDOW_DEBOUNCE_SECONDS:
                await page.bring_to_front()
                self.last_focus_window_at = now
        elif action == "close_tab":
            page_id = str(args.get("page_id") or self.active_page_id)
            selected = self.pages.get(page_id)
            if selected is None:
                raise BrowserBridgeError("page_not_found", f"Browser page not found: {page_id}")
            await selected.close()
            self._remove_page(page_id, id(selected))
            if not self.pages:
                replacement = await self.context.new_page()
                await self._register_page(replacement)
                await self._navigate_to_start_url(replacement, config)
        elif action == "mouse":
            await browser_mouse_action(
                page,
                args,
                viewport_width=self.viewport_width,
                viewport_height=self.viewport_height,
            )
            if str(args.get("event") or "click").strip().lower() not in {"move", "down", "wheel"}:
                accepts_text_input = await browser_page_accepts_text_input(page)
        elif action == "text":
            text = str(args.get("text") or "")
            if len(text) > 65536:
                raise BrowserBridgeError("input_too_large", "Browser text input is limited to 65536 characters.")
            await page.keyboard.insert_text(text)
        if action != "focus_window":
            self.revision += 1
        active = self._active_page()
        result = {
            "page_id": self.active_page_id,
            "url": str(active.url or "")[:2048] if active is not None else "",
            "title": (await active.title())[:500] if active is not None else "",
            "revision": self.revision,
            "control_epoch": self.control_epoch,
        }
        if accepts_text_input is not None:
            result["accepts_text_input"] = accepts_text_input
        return result

    async def _close(self) -> None:
        self.stop_event.set()
        if self.server is not None:
            self.server.close()
            await self.server.wait_closed()
        for writer in list(self.subscribers):
            writer.close()
        self.subscribers.clear()
        for task in list(self._tasks):
            if task is asyncio.current_task():
                continue
            task.cancel()
        for task in list(self._tasks):
            if task is asyncio.current_task():
                continue
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
        if self.browser_session is not None:
            try:
                await self.browser_session.close()
            except Exception:
                pass
            self.browser_session = None
        if self.playwright is not None:
            try:
                await asyncio.wait_for(self.playwright.stop(), timeout=2.0)
            except Exception:
                pass
        try:
            socket_path = browser_bridge_socket_path(self.root, self.run_id, self.task_id)
            if socket_path.exists():
                socket_path.unlink()
        except OSError:
            pass
        if self.profile_lease is not None:
            self.profile_lease.close()
            self.profile_lease = None
        try:
            state = read_browser_bridge_state(self.root, self.run_id, self.task_id) or {}
            if state.get("status") != "error":
                self._write_state("stopped")
        except Exception:
            pass

async def run_browser_bridge_daemon(root: Path, run_id: str, task_id: str) -> int:
    return await BrowserBridgeDaemon(root, run_id, task_id).run()

save_browser_screenshot = browser_client.save_browser_screenshot

__all__ = [
    "BrowserBridgeDaemon",
    "BrowserBridgeError",
    "browser_artifacts_dir",
    "browser_bridge_request",
    "browser_bridge_socket_path",
    "browser_bridge_state_path",
    "browser_bridge_status",
    "browser_doctor",
    "browser_runtime_dir",
    "ensure_browser_bridge",
    "open_browser_bridge_ipc",
    "read_browser_bridge_state",
    "run_browser_bridge_daemon",
    "save_browser_screenshot",
]
