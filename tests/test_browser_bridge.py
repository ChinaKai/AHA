from __future__ import annotations

import asyncio
from collections import defaultdict
from pathlib import Path
import socket
import tempfile
import unittest
from unittest import mock

from aha_cli.cli_parser import build_parser
from aha_cli.services.browser_actions import browser_page_accepts_text_input
from aha_cli.services.browser_bridge import (
    BrowserBridgeDaemon,
    BrowserBridgeError,
    browser_artifacts_dir,
    browser_runtime_dir,
)
from aha_cli.services.browser_runtime import (
    _browser_bridge_socket_accepting,
    _wait_for_browser_bridge_ready,
    acquire_browser_profile,
    browser_bridge_manual_stop_path,
    browser_context_launch_options,
    browser_display_status,
    browser_doctor,
    browser_frame_size,
    browser_initial_viewport,
    browser_launch_signature,
    browser_named_profile_dir,
    browser_native_display_environment,
    browser_proxy_launch_options,
    browser_session_lifecycle,
    ensure_browser_bridge,
    list_named_browser_profiles,
)
from aha_cli.services.browser_io import (
    append_browser_io_record,
    browser_io_page,
    redact_browser_url,
)
from aha_cli.store.filesystem import create_plan, update_task_browser_control_config


class BrowserPolicyTests(unittest.TestCase):
    def test_browser_upload_parser_accepts_ref_and_path(self) -> None:
        handler = lambda _args: 0
        parser = build_parser(defaultdict(lambda: handler))

        args = parser.parse_args([
            "browser",
            "upload",
            "run-001",
            "task-001",
            "4:b12",
            "artifact.zip",
        ])

        self.assertEqual(args.browser_action, "upload")
        self.assertEqual(args.ref, "4:b12")
        self.assertEqual(args.path, "artifact.zip")

    def test_page_text_focus_is_reported_without_reading_field_content(self) -> None:
        ignored = mock.Mock()
        ignored.evaluate = mock.AsyncMock(side_effect=RuntimeError("cross-origin frame"))
        editable = mock.Mock()
        editable.evaluate = mock.AsyncMock(return_value=True)
        page = mock.Mock(frames=[ignored, editable])

        self.assertTrue(asyncio.run(browser_page_accepts_text_input(page)))
        script = editable.evaluate.await_args.args[0]
        self.assertIn("document.activeElement", script)
        self.assertIn("element?.shadowRoot?.activeElement", script)
        self.assertNotIn(".value", script)

        editable.evaluate = mock.AsyncMock(return_value=False)
        self.assertFalse(asyncio.run(browser_page_accepts_text_input(page)))

    def test_url_policy_supports_exact_and_wildcard_hosts(self) -> None:
        config = {"allowed_hosts": ["example.com", "*.example.org"]}

        self.assertTrue(BrowserBridgeDaemon._url_allowed("https://example.com/path", config))
        self.assertTrue(BrowserBridgeDaemon._url_allowed("https://app.example.org/", config))
        self.assertFalse(BrowserBridgeDaemon._url_allowed("https://example.org/", config))
        self.assertFalse(BrowserBridgeDaemon._url_allowed("https://badexample.com/", config))
        self.assertFalse(BrowserBridgeDaemon._url_allowed("file:///etc/passwd", config))

    def test_runtime_and_artifact_paths_are_task_scoped(self) -> None:
        root = Path("/tmp/aha-browser-test")

        runtime = browser_runtime_dir(root, "../run", "../../task")
        artifacts = browser_artifacts_dir(root, "run-001", "task-001")

        self.assertNotIn("..", runtime.parts)
        self.assertTrue(str(runtime).endswith("runtime/browser/run/task"))
        self.assertTrue(str(artifacts).endswith("runs/run-001/tasks/task-001/browser_artifacts"))

    def test_device_mode_has_explicit_initial_viewports(self) -> None:
        self.assertEqual(browser_initial_viewport({"device_mode": "desktop"}), (1280, 720, False))
        self.assertEqual(browser_initial_viewport({"device_mode": "mobile"}), (360, 640, True))
        self.assertEqual(browser_initial_viewport({}), (1280, 720, False))

    def test_internal_new_tab_pages_are_replaced_by_the_configured_start_url(self) -> None:
        needs_start_url = BrowserBridgeDaemon._should_open_start_url

        for url in (
            "",
            "about:blank",
            "chrome://newtab/",
            "chrome://newtab/?source=aha",
            "chrome://new-tab-page/",
            "chrome-search://local-ntp/local-ntp.html",
        ):
            with self.subTest(url=url):
                self.assertTrue(needs_start_url(url))
        self.assertFalse(needs_start_url("chrome://settings/"))
        self.assertFalse(needs_start_url("https://www.google.com/"))

    def test_manual_close_blocks_lazy_start_until_current_task_is_started(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            plan = create_plan(root, "Browser lifecycle", 1, "research", ["Browser"], [])
            run_id = plan["id"]
            task_id = plan["tasks"][0]["id"]
            update_task_browser_control_config(root, run_id, task_id, mode="managed")

            with mock.patch(
                "aha_cli.services.browser_runtime._stop_browser_bridge",
                return_value={"status": "closed"},
            ) as stop:
                closed = browser_session_lifecycle(root, run_id, task_id, "close")

            marker = browser_bridge_manual_stop_path(root, run_id, task_id)
            self.assertTrue(marker.is_file())
            self.assertEqual(closed["bridge"]["status"], "closed")
            stop.assert_called_once_with(root, run_id, task_id)
            with self.assertRaises(BrowserBridgeError) as blocked:
                ensure_browser_bridge(root, run_id, task_id)
            self.assertEqual(blocked.exception.code, "browser_closed")

            with mock.patch(
                "aha_cli.services.browser_runtime.ensure_browser_bridge",
                return_value={"status": "running", "alive": True},
            ) as ensure:
                started = browser_session_lifecycle(root, run_id, task_id, "start")

            self.assertFalse(marker.exists())
            self.assertEqual(started["action"], "start")
            ensure.assert_called_once_with(root, run_id, task_id)

    def test_ensure_replaces_running_pid_when_ipc_socket_refuses_connections(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            plan = create_plan(root, "Browser stale IPC", 1, "research", ["Browser"], [])
            run_id = plan["id"]
            task_id = plan["tasks"][0]["id"]
            update_task_browser_control_config(root, run_id, task_id, mode="managed")
            state_path = browser_runtime_dir(root, run_id, task_id) / "bridge.json"
            state_path.parent.mkdir(parents=True, exist_ok=True)
            state_path.write_text(
                '{"pid": 1234, "status": "running"}',
                encoding="utf-8",
            )
            process = mock.Mock(pid=5678)

            with (
                mock.patch("aha_cli.services.browser_runtime.pid_alive", return_value=True),
                mock.patch(
                    "aha_cli.services.browser_runtime._browser_bridge_socket_accepting",
                    return_value=False,
                ),
                mock.patch(
                    "aha_cli.services.browser_runtime._stop_browser_bridge",
                    return_value={"status": "stopped", "alive": False},
                ) as stop,
                mock.patch("aha_cli.services.browser_runtime.subprocess.Popen", return_value=process),
                mock.patch(
                    "aha_cli.services.browser_runtime.browser_bridge_launcher",
                    return_value=["python", "-m", "aha_cli"],
                ),
                mock.patch(
                    "aha_cli.services.browser_runtime.process_control.assign_parent_death",
                ) as assign_parent,
            ):
                status = ensure_browser_bridge(root, run_id, task_id)

            stop.assert_called_once_with(root, run_id, task_id, timeout=2.0)
            assign_parent.assert_not_called()
            self.assertEqual(status["pid"], 5678)
        self.assertEqual(status["status"], "starting")

    def test_parent_bound_browser_bridge_joins_parent_job(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            plan = create_plan(root, "Browser parent binding", 1, "research", ["Browser"], [])
            run_id = plan["id"]
            task_id = plan["tasks"][0]["id"]
            update_task_browser_control_config(root, run_id, task_id, mode="managed")
            process = mock.Mock(pid=5678)

            with (
                mock.patch("aha_cli.services.browser_runtime.subprocess.Popen", return_value=process) as popen,
                mock.patch(
                    "aha_cli.services.browser_runtime.browser_bridge_launcher",
                    return_value=["python", "-m", "aha_cli"],
                ),
                mock.patch(
                    "aha_cli.services.browser_runtime.process_control.assign_parent_death",
                ) as assign_parent,
            ):
                status = ensure_browser_bridge(root, run_id, task_id, parent_bound=True)

            assign_parent.assert_called_once_with(process)
            self.assertFalse(popen.call_args.kwargs["start_new_session"])
            self.assertEqual(status["pid"], 5678)

    def test_browser_bridge_launcher_requests_playwright_capable_python(self) -> None:
        with mock.patch(
            "aha_cli.services.browser_runtime.aha_cli_invocation",
            return_value=[r"C:\Users\me\.venvs\aha\Scripts\python.exe", "aha"],
        ) as invocation:
            command = __import__(
                "aha_cli.services.browser_runtime",
                fromlist=["browser_bridge_launcher"],
            ).browser_bridge_launcher()

        self.assertEqual(command[0], r"C:\Users\me\.venvs\aha\Scripts\python.exe")
        invocation.assert_called_once_with(required_module="playwright")

    def test_browser_doctor_uses_detected_playwright_environment(self) -> None:
        detected = r"C:\Users\me\.venvs\aha\Scripts\python.exe"
        with (
            mock.patch(
                "aha_cli.services.browser_runtime.resolve_aha_python",
                return_value=detected,
            ),
            mock.patch(
                "aha_cli.services.browser_runtime._browser_doctor_with_python",
                return_value={
                    "ok": True,
                    "playwright_installed": True,
                    "python_executable": detected,
                    "python_fallback": True,
                },
            ) as inspect,
        ):
            result = asyncio.run(browser_doctor())

        self.assertTrue(result["ok"])
        self.assertTrue(result["python_fallback"])
        inspect.assert_called_once_with(detected)

    def test_lifecycle_ready_waits_for_running_socket(self) -> None:
        with (
            mock.patch(
                "aha_cli.services.browser_runtime.browser_bridge_status",
                side_effect=[
                    {"status": "starting", "alive": True},
                    {"status": "running", "alive": True, "instance_id": "bridge-ready"},
                ],
            ),
            mock.patch(
                "aha_cli.services.browser_runtime._browser_bridge_socket_accepting",
                return_value=True,
            ) as socket_ready,
            mock.patch("aha_cli.services.browser_runtime.time.sleep"),
        ):
            ready = _wait_for_browser_bridge_ready(
                Path("/tmp/aha-browser-ready"),
                "run-001",
                "task-001",
                timeout=1.0,
            )

        self.assertEqual(ready["instance_id"], "bridge-ready")
        socket_ready.assert_called_once()

    def test_socket_health_rejects_stale_unix_socket_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            run_id = "run-001"
            task_id = "task-001"
            socket_path = browser_runtime_dir(root, run_id, task_id) / "browser.sock"
            socket_path.parent.mkdir(parents=True, exist_ok=True)
            listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            listener.bind(str(socket_path))
            listener.close()

            self.assertFalse(_browser_bridge_socket_accepting(root, run_id, task_id))

    def test_agent_write_requires_permission_and_user_preempts(self) -> None:
        daemon = BrowserBridgeDaemon(Path("/tmp/aha-browser-test"), "run-001", "task-001")
        task = {"status": "running"}
        config = {"mode": "managed", "agent_access": "read_only"}
        daemon._config = lambda: (task, config)  # type: ignore[method-assign]
        daemon._active_page = lambda: None  # type: ignore[method-assign]
        daemon._execute_locked = mock.AsyncMock(return_value={"ok": True})  # type: ignore[method-assign]

        with self.assertRaises(BrowserBridgeError) as denied:
            asyncio.run(daemon._execute("navigate", {"url": "https://example.com"}, source="agent", agent_id="main"))
        self.assertEqual(denied.exception.code, "read_only")

        config["agent_access"] = "read_write"
        with mock.patch("aha_cli.services.browser_bridge.append_browser_io_record"):
            asyncio.run(daemon._execute("reload", {}, source="user", agent_id="browser"))
            with self.assertRaises(BrowserBridgeError) as preempted:
                asyncio.run(daemon._execute("reload", {}, source="agent", agent_id="main"))
        self.assertEqual(preempted.exception.code, "control_preempted")

    def test_upload_requires_policy_and_sets_file_input(self) -> None:
        daemon = BrowserBridgeDaemon(Path("/tmp/aha-browser-test"), "run-001", "task-001")
        page = mock.Mock()
        page.url = "https://example.com/compose"
        page.title = mock.AsyncMock(return_value="Compose")
        locator = mock.Mock()
        locator.first = locator
        locator.set_input_files = mock.AsyncMock()
        page.locator.return_value = locator
        daemon.pages = {"page-001": page}
        daemon.page_ids = {id(page): "page-001"}
        daemon.active_page_id = "page-001"
        daemon._write_state = mock.Mock()  # type: ignore[method-assign]
        daemon._broadcast_state = mock.AsyncMock()  # type: ignore[method-assign]

        with tempfile.TemporaryDirectory() as tmp:
            upload = Path(tmp) / "artifact.zip"
            upload.write_bytes(b"artifact")
            with self.assertRaises(BrowserBridgeError) as denied:
                asyncio.run(
                    daemon._execute_locked(
                        "upload",
                        {"ref": "0:b12", "path": str(upload.resolve())},
                        {"uploads": "deny"},
                    )
                )
            self.assertEqual(denied.exception.code, "upload_denied")

            result = asyncio.run(
                daemon._execute_locked(
                    "upload",
                    {"ref": "0:b12", "path": str(upload.resolve())},
                    {"uploads": "allow"},
                )
            )

        page.locator.assert_called_once_with('[data-aha-browser-ref="b12"]')
        locator.set_input_files.assert_awaited_once_with(str(upload.resolve()), timeout=30000)
        self.assertEqual(result["upload"], {"filename": "artifact.zip", "size": 8})
        self.assertEqual(result["revision"], 1)

    def test_upload_normalizes_windows_style_path_in_wsl(self) -> None:
        # On WSL a Windows-style path (C:\\...) is not absolute and would resolve
        # to garbage; the upload handler must normalize it via host_native_path
        # to a /mnt/... path that the WSL-side bridge can actually open.
        import sys

        if not sys.platform.startswith("linux"):
            self.skipTest("WSL-style path normalization only applies on Linux")
        daemon = BrowserBridgeDaemon(Path("/tmp/aha-browser-test"), "run-001", "task-001")
        page = mock.Mock()
        page.url = "https://example.com/compose"
        page.title = mock.AsyncMock(return_value="Compose")
        locator = mock.Mock()
        locator.first = locator
        locator.set_input_files = mock.AsyncMock()
        page.locator.return_value = locator
        daemon.pages = {"page-001": page}
        daemon.page_ids = {id(page): "page-001"}
        daemon.active_page_id = "page-001"
        daemon._write_state = mock.Mock()  # type: ignore[method-assign]
        daemon._broadcast_state = mock.AsyncMock()  # type: ignore[method-assign]

        with tempfile.TemporaryDirectory() as tmp:
            upload = Path(tmp) / "artifact.zip"
            upload.write_bytes(b"artifact")
            # Represent the same file with a Windows drive path. Under a real
            # /mnt mount this normalizes back to the accessible /mnt path.
            drive_path = "C:\\Users\\toope\\AppData\\Local\\Temp\\artifact.zip"
            with mock.patch(
                "aha_cli.services.browser_bridge.host_native_path",
                return_value=str(upload.resolve()),
            ) as normalize:
                result = asyncio.run(
                    daemon._execute_locked(
                        "upload",
                        {"ref": "0:b12", "path": drive_path},
                        {"uploads": "allow"},
                    )
                )
            normalize.assert_called_once_with(drive_path, aha_home="/tmp/aha-browser-test")

        locator.set_input_files.assert_awaited_once_with(str(upload.resolve()), timeout=30000)
        self.assertEqual(result["upload"], {"filename": "artifact.zip", "size": 8})

    def test_frame_loop_streams_1080p_without_waiting_for_action_lock(self) -> None:
        daemon = BrowserBridgeDaemon(Path("/tmp/aha-browser-test"), "run-001", "task-001")
        page = mock.Mock()
        page.url = "https://example.com/"
        page.screenshot = mock.AsyncMock(return_value=b"jpeg-frame")
        daemon.active_page_id = "page-001"
        daemon._active_page = lambda: page  # type: ignore[method-assign]
        daemon.subscribers.add(mock.Mock())
        frames: list[dict] = []

        async def broadcast(payload: dict) -> None:
            frames.append(payload)
            daemon.stop_event.set()

        daemon._broadcast = broadcast  # type: ignore[method-assign]

        async def run() -> None:
            await daemon.action_lock.acquire()
            try:
                await asyncio.wait_for(daemon._frame_loop(), timeout=1.0)
            finally:
                daemon.action_lock.release()

        asyncio.run(run())
        page.screenshot.assert_awaited_once_with(type="jpeg", quality=70, full_page=False)
        self.assertEqual(frames[0]["event"], "frame")
        self.assertEqual(frames[0]["instance_id"], daemon.instance_id)
        self.assertEqual((frames[0]["width"], frames[0]["height"]), (1280, 720))
        self.assertEqual((frames[0]["image_width"], frames[0]["image_height"]), (1920, 1080))

    def test_frame_size_keeps_logical_viewports_and_uses_1080_class_pixels(self) -> None:
        self.assertEqual(browser_frame_size(1280, 720, mobile=False), (1920, 1080))
        self.assertEqual(browser_frame_size(360, 640, mobile=True), (1080, 1920))

    def test_frame_loop_resends_unchanged_frame_to_new_subscriber(self) -> None:
        daemon = BrowserBridgeDaemon(Path("/tmp/aha-browser-test"), "run-001", "task-001")
        page = mock.Mock()
        page.url = "https://example.com/"
        page.screenshot = mock.AsyncMock(return_value=b"same-jpeg-frame")
        daemon.active_page_id = "page-001"
        daemon._active_page = lambda: page  # type: ignore[method-assign]
        first = mock.Mock()
        second = mock.Mock()
        daemon.subscribers.add(first)
        daemon.frame_subscriber_epoch = 1
        frames: list[dict] = []

        async def broadcast(payload: dict) -> None:
            frames.append(payload)
            if len(frames) == 1:
                daemon.subscribers.add(second)
                daemon.frame_subscriber_epoch += 1
                daemon.frame_wake_event.set()
            else:
                daemon.stop_event.set()

        daemon._broadcast = broadcast  # type: ignore[method-assign]

        asyncio.run(asyncio.wait_for(daemon._frame_loop(), timeout=1.0))

        self.assertEqual(page.screenshot.await_count, 2)
        self.assertEqual([frame["event"] for frame in frames], ["frame", "frame"])
        self.assertEqual(frames[0]["data"], frames[1]["data"])

    def test_runtime_viewport_cannot_be_hot_switched(self) -> None:
        daemon = BrowserBridgeDaemon(Path("/tmp/aha-browser-test"), "run-001", "task-001")
        daemon._config = lambda: (  # type: ignore[method-assign]
            {"status": "running"},
            {"mode": "managed", "agent_access": "read_write"},
        )

        with self.assertRaises(BrowserBridgeError) as denied:
            asyncio.run(
                daemon._execute(
                    "set_viewport",
                    {"width": 400, "height": 800, "mobile": True},
                    source="user",
                    agent_id="browser",
                )
            )

        self.assertEqual(denied.exception.code, "unknown_action")
        self.assertEqual((daemon.viewport_width, daemon.viewport_height), (1280, 720))

    def test_navigation_reapplies_current_mobile_viewport(self) -> None:
        daemon = BrowserBridgeDaemon(Path("/tmp/aha-browser-test"), "run-001", "task-001")
        page = mock.Mock()
        daemon.pages = {"page-001": page}
        daemon.viewport_width = 360
        daemon.viewport_height = 640
        daemon.mobile_emulation = True
        session = mock.Mock()
        session.prepare_page = mock.AsyncMock()
        daemon.browser_session = session
        daemon._write_state = mock.Mock()  # type: ignore[method-assign]
        daemon._broadcast_state = mock.AsyncMock()  # type: ignore[method-assign]

        asyncio.run(daemon._page_navigated("page-001"))

        session.prepare_page.assert_awaited_once_with(
            page, width=360, height=640, mobile=True,
        )
        self.assertEqual(daemon.active_page_id, "page-001")

    def test_custom_browser_proxy_builds_playwright_options(self) -> None:
        options = browser_proxy_launch_options(
            Path("/tmp/aha-browser-test"),
            "run-001",
            {"preferred_proxy_enabled": False},
            {
                "proxy_mode": "custom",
                "proxy_server": "socks5://proxy.example:1080",
                "proxy_bypass": "localhost,*.internal",
                "proxy_username": "alice",
                "proxy_password": "secret",
            },
        )

        self.assertEqual(
            options,
            {
                "server": "socks5://proxy.example:1080",
                "bypass": "localhost,*.internal",
                "username": "alice",
                "password": "secret",
            },
        )

    def test_native_display_uses_desktop_or_falls_back_to_embedded(self) -> None:
        unavailable = browser_display_status(
            {"display": "native"},
            environ={},
            platform_name="linux",
            wslg_root=Path("/nonexistent-aha-wslg"),
        )
        available = browser_display_status(
            {"display": "native"},
            environ={"DISPLAY": ":7"},
            platform_name="linux",
        )
        embedded = browser_display_status(
            {"display": "embedded"},
            environ={"DISPLAY": ":7"},
            platform_name="linux",
        )

        self.assertEqual(unavailable["active"], "embedded")
        self.assertTrue(unavailable["fallback"])
        self.assertEqual(unavailable["fallback_reason"], "native_display_unavailable")
        self.assertEqual(available["active"], "native")
        self.assertFalse(available["fallback"])
        self.assertEqual(embedded["active"], "embedded")
        self.assertFalse(embedded["fallback"])

    def test_wslg_socket_enables_native_display_without_inherited_display_env(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            socket_dir = root / ".X11-unix"
            socket_dir.mkdir()
            display_socket = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            try:
                display_socket.bind(str(socket_dir / "X0"))
                environment = browser_native_display_environment(
                    environ={},
                    platform_name="linux",
                    wslg_root=root,
                )
                status = browser_display_status(
                    {"display": "native"},
                    environ={},
                    platform_name="linux",
                    wslg_root=root,
                )
            finally:
                display_socket.close()

        self.assertEqual(environment, {"DISPLAY": ":0"})
        self.assertEqual(status["active"], "native")
        self.assertTrue(status["native_available"])

    def test_browser_launch_signature_tracks_profile_display_device_downloads_and_proxy(self) -> None:
        base = {
            "profile": "ephemeral",
            "display": "embedded",
            "downloads": "deny",
        }
        display = browser_display_status(base, environ={}, platform_name="linux")
        signature = browser_launch_signature(base, None, display_status=display)

        for update in (
            {"runtime": "user_chrome"},
            {"profile": "task"},
            {"display": "native"},
            {"device_mode": "mobile"},
            {"downloads": "allow"},
        ):
            changed = {**base, **update}
            changed_display = browser_display_status(
                changed,
                environ={"DISPLAY": ":7"},
                platform_name="linux",
            )
            self.assertNotEqual(
                browser_launch_signature(changed, None, display_status=changed_display),
                signature,
            )
        self.assertNotEqual(
            browser_launch_signature(
                base,
                {"server": "http://proxy.example:7890"},
                display_status=display,
            ),
            signature,
        )
        self.assertNotEqual(
            browser_launch_signature(
                {**base, "profile": "named", "profile_name": "Work"},
                None,
                display_status=display,
            ),
            browser_launch_signature(
                {**base, "profile": "named", "profile_name": "Personal"},
                None,
                display_status=display,
            ),
        )

    def test_named_browser_profile_is_reusable_and_exclusive(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = {"profile": "named", "profile_name": "工作"}
            first = acquire_browser_profile(root, "run-001", "task-001", config)
            try:
                self.assertEqual(first.path, browser_named_profile_dir(root, "工作"))
                self.assertTrue(first.path.is_dir())
                self.assertEqual(
                    [item["name"] for item in list_named_browser_profiles(root)],
                    ["工作"],
                )
                with self.assertRaises(BrowserBridgeError) as occupied:
                    acquire_browser_profile(root, "run-002", "task-002", config)
                self.assertEqual(occupied.exception.code, "browser_profile_in_use")
            finally:
                first.close()

            second = acquire_browser_profile(root, "run-002", "task-002", config)
            second.close()

    def test_bridge_close_preserves_startup_error_state(self) -> None:
        daemon = BrowserBridgeDaemon(Path("/tmp/aha-browser-test"), "run-001", "task-001")
        daemon.profile_lease = mock.Mock()
        daemon.profile_lease.close = mock.Mock()
        lease = daemon.profile_lease
        daemon._write_state = mock.Mock()  # type: ignore[method-assign]

        with mock.patch(
            "aha_cli.services.browser_bridge.read_browser_bridge_state",
            return_value={"status": "error", "error_code": "browser_profile_in_use"},
        ):
            asyncio.run(daemon._close())

        lease.close.assert_called_once_with()
        self.assertIsNone(daemon.profile_lease)
        daemon._write_state.assert_not_called()

    def test_native_context_launches_headed_and_embedded_context_launches_headless(self) -> None:
        proxy = {"server": "http://proxy.example:7890"}
        native = browser_context_launch_options(
            {"downloads": "allow"},
            proxy,
            display_status={"active": "native"},
            viewport_width=1280,
            viewport_height=720,
        )
        embedded = browser_context_launch_options(
            {"downloads": "deny"},
            None,
            display_status={"active": "embedded"},
            viewport_width=1280,
            viewport_height=720,
        )

        self.assertFalse(native["headless"])
        self.assertEqual(native["device_scale_factor"], 1.5)
        self.assertTrue(native["accept_downloads"])
        self.assertIn("--window-size=1280,820", native["args"])
        self.assertEqual(native["proxy"], proxy)
        self.assertTrue(embedded["headless"])
        self.assertEqual(embedded["device_scale_factor"], 1.5)
        self.assertFalse(embedded["accept_downloads"])
        self.assertNotIn("--window-size=1280,820", embedded["args"])
        self.assertNotIn("proxy", embedded)

    def test_reap_loop_restarts_bridge_when_profile_changes(self) -> None:
        daemon = BrowserBridgeDaemon(
            Path("/tmp/aha-browser-test"),
            "run-001",
            "task-001",
            reap_interval=1,
        )
        initial = {
            "mode": "managed",
            "profile": "ephemeral",
            "display": "embedded",
            "downloads": "deny",
            "proxy_mode": "direct",
        }
        changed = {**initial, "profile": "task"}
        daemon.launch_signature = browser_launch_signature(
            initial,
            None,
            display_status=browser_display_status(initial),
        )
        daemon._config = lambda: ({"status": "running"}, changed)  # type: ignore[method-assign]

        with (
            mock.patch("aha_cli.services.browser_bridge.asyncio.sleep", new=mock.AsyncMock()),
            mock.patch(
                "aha_cli.services.browser_bridge.browser_client.browser_proxy_launch_options",
                return_value=None,
            ),
        ):
            asyncio.run(daemon._reap_loop())

        self.assertTrue(daemon.stop_event.is_set())

    def test_inherited_browser_proxy_uses_config_independent_of_task_toggle(self) -> None:
        config = {"proxy_mode": "inherit"}
        with (
            mock.patch("aha_cli.services.browser_runtime.require_plan", return_value={"id": "run-001"}),
            mock.patch(
                "aha_cli.services.browser_runtime.load_config",
                return_value={
                    "backend": "codex",
                    "codex": {
                        "proxy": {
                            "enabled": False,
                            "http_proxy": "http://proxy.example:7890",
                            "https_proxy": "http://proxy.example:7890",
                            "no_proxy": "localhost",
                        }
                    },
                },
            ),
        ):
            task_proxy_off = browser_proxy_launch_options(
                Path("/tmp/aha-browser-test"),
                "run-001",
                {"preferred_backend": "codex", "preferred_proxy_enabled": False},
                config,
            )
            task_proxy_on = browser_proxy_launch_options(
                Path("/tmp/aha-browser-test"),
                "run-001",
                {"preferred_backend": "codex", "preferred_proxy_enabled": True},
                config,
            )

        expected = {"server": "http://proxy.example:7890", "bypass": "localhost"}
        self.assertEqual(task_proxy_off, expected)
        self.assertEqual(task_proxy_on, expected)

    def test_closing_last_tab_opens_configured_start_url(self) -> None:
        daemon = BrowserBridgeDaemon(Path("/tmp/aha-browser-test"), "run-001", "task-001")
        selected = mock.Mock()
        selected.url = "https://example.com/"
        selected.close = mock.AsyncMock()
        replacement = mock.Mock()
        replacement.url = "about:blank"
        replacement.title = mock.AsyncMock(return_value="Google")

        async def navigate(url: str, **_kwargs) -> None:
            replacement.url = url

        replacement.goto = mock.AsyncMock(side_effect=navigate)
        daemon.pages = {"page-001": selected}
        daemon.page_ids = {id(selected): "page-001"}
        daemon.active_page_id = "page-001"
        daemon.context = mock.Mock()
        daemon.context.new_page = mock.AsyncMock(return_value=replacement)

        async def register(page) -> str:
            daemon.pages["page-002"] = page
            daemon.page_ids[id(page)] = "page-002"
            daemon.active_page_id = "page-002"
            return "page-002"

        daemon._register_page = register  # type: ignore[method-assign]

        result = asyncio.run(
            daemon._execute_locked(
                "close_tab",
                {},
                {"start_url": "https://www.google.com/", "allowed_hosts": []},
            )
        )

        selected.close.assert_awaited_once_with()
        daemon.context.new_page.assert_awaited_once_with()
        replacement.goto.assert_awaited_once_with(
            "https://www.google.com/",
            wait_until="domcontentloaded",
            timeout=15000,
        )
        self.assertEqual(result["page_id"], "page-002")
        self.assertEqual(result["url"], "https://www.google.com/")
        self.assertEqual(set(daemon.pages), {"page-002"})

    def test_start_url_timeout_does_not_kill_browser_session(self) -> None:
        daemon = BrowserBridgeDaemon(Path("/tmp/aha-browser-test"), "run-001", "task-001")
        page = mock.Mock()
        page.goto = mock.AsyncMock(side_effect=TimeoutError("proxy timeout"))
        daemon._broadcast = mock.AsyncMock()  # type: ignore[method-assign]

        asyncio.run(
            daemon._navigate_to_start_url(
                page,
                {"start_url": "https://www.google.com/", "allowed_hosts": []},
            )
        )

        daemon._broadcast.assert_awaited_once()
        payload = daemon._broadcast.await_args.args[0]
        self.assertEqual(payload["event"], "navigation_error")

    def test_new_tab_without_url_opens_configured_start_url(self) -> None:
        daemon = BrowserBridgeDaemon(Path("/tmp/aha-browser-test"), "run-001", "task-001")
        existing = mock.Mock()
        existing.url = "https://example.com/"
        replacement = mock.Mock()
        replacement.url = "about:blank"

        async def navigate(url: str, **_kwargs) -> None:
            replacement.url = url

        replacement.goto = mock.AsyncMock(side_effect=navigate)
        daemon.pages = {"page-001": existing}
        daemon.page_ids = {id(existing): "page-001"}
        daemon.active_page_id = "page-001"
        daemon.context = mock.Mock()
        daemon.context.new_page = mock.AsyncMock(return_value=replacement)

        async def register(page) -> str:
            daemon.pages["page-002"] = page
            daemon.page_ids[id(page)] = "page-002"
            daemon.active_page_id = "page-002"
            return "page-002"

        daemon._register_page = register  # type: ignore[method-assign]

        result = asyncio.run(
            daemon._execute_locked(
                "new_tab",
                {},
                {"start_url": "https://www.google.com/", "allowed_hosts": []},
            )
        )

        replacement.goto.assert_awaited_once_with(
            "https://www.google.com/",
            wait_until="domcontentloaded",
            timeout=30000,
        )
        self.assertEqual(result["page_id"], "page-002")
        self.assertEqual(result["url"], "https://www.google.com/")

    def test_new_tab_cli_omits_url_so_bridge_can_use_task_default(self) -> None:
        handlers = defaultdict(lambda: lambda _args: 0)
        args = build_parser(handlers).parse_args(
            ["browser", "new-tab", "run-001", "task-001"]
        )

        self.assertEqual(args.url, "")

    def test_focus_window_brings_active_native_page_to_front(self) -> None:
        daemon = BrowserBridgeDaemon(Path("/tmp/aha-browser-test"), "run-001", "task-001")
        page = mock.Mock()
        page.url = "https://example.com/"
        page.title = mock.AsyncMock(return_value="Example")
        page.bring_to_front = mock.AsyncMock()
        daemon.pages = {"page-001": page}
        daemon.page_ids = {id(page): "page-001"}
        daemon.active_page_id = "page-001"

        first = asyncio.run(
            daemon._execute_locked(
                "focus_window",
                {},
                {"allowed_hosts": []},
            )
        )
        second = asyncio.run(
            daemon._execute_locked(
                "focus_window",
                {},
                {"allowed_hosts": []},
            )
        )
        daemon.last_focus_window_at -= 3.0
        third = asyncio.run(
            daemon._execute_locked(
                "focus_window",
                {},
                {"allowed_hosts": []},
            )
        )

        self.assertEqual(page.bring_to_front.await_count, 2)
        self.assertEqual(first["page_id"], "page-001")
        self.assertEqual(first["title"], "Example")
        self.assertEqual(second["revision"], 0)
        self.assertEqual(third["revision"], 0)

    def test_focus_window_does_not_wake_embedded_frame_capture(self) -> None:
        daemon = BrowserBridgeDaemon(Path("/tmp/aha-browser-test"), "run-001", "task-001")
        page = mock.Mock()
        page.url = "https://example.com/"
        page.title = mock.AsyncMock(return_value="Example")
        page.bring_to_front = mock.AsyncMock()
        daemon.pages = {"page-001": page}
        daemon.page_ids = {id(page): "page-001"}
        daemon.active_page_id = "page-001"
        daemon._config = lambda: (  # type: ignore[method-assign]
            {"status": "running"},
            {"mode": "managed", "agent_access": "read_write"},
        )
        daemon._write_state = mock.Mock()  # type: ignore[method-assign]
        daemon._broadcast_state = mock.AsyncMock()  # type: ignore[method-assign]

        with (
            mock.patch("aha_cli.services.browser_bridge.append_browser_io_record"),
            mock.patch("aha_cli.services.browser_bridge.time.monotonic", return_value=100.0),
        ):
            result = asyncio.run(
                daemon._execute(
                    "focus_window",
                    {},
                    source="user",
                    agent_id="browser",
                )
            )

        self.assertEqual(result["revision"], 0)
        self.assertFalse(daemon.frame_wake_event.is_set())
        daemon._broadcast_state.assert_awaited_once_with()

    def test_native_user_activity_preempts_agent_without_recording_input(self) -> None:
        daemon = BrowserBridgeDaemon(Path("/tmp/aha-browser-test"), "run-001", "task-001")
        daemon.display_status = {
            "requested": "native",
            "active": "native",
            "native_available": True,
            "fallback": False,
            "fallback_reason": "",
        }
        daemon._write_state = mock.Mock()  # type: ignore[method-assign]
        daemon._broadcast_state = mock.AsyncMock()  # type: ignore[method-assign]

        asyncio.run(daemon._native_user_activity({}, "keydown"))
        first_epoch = daemon.control_epoch
        daemon.bridge_mutation_depth = 1
        asyncio.run(daemon._native_user_activity({}, "pointerdown"))

        self.assertEqual(first_epoch, 1)
        self.assertEqual(daemon.control_epoch, first_epoch)
        self.assertGreater(daemon.user_active_until, 0)
        daemon._write_state.assert_called_once_with("running")


class BrowserAuditTests(unittest.TestCase):
    def test_audit_redacts_url_secrets_and_never_records_typed_text(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            plan = create_plan(root, "Browser audit", 1, "research", ["Shared browser"], [])
            run_id = plan["id"]
            task_id = plan["tasks"][0]["id"]
            update_task_browser_control_config(root, run_id, task_id, mode="managed")

            append_browser_io_record(
                root,
                run_id,
                task_id,
                {
                    "source": "user",
                    "action": "text",
                    "text": "top-secret-password",
                    "url_before": "https://alice:pw@example.com/private/token?access_token=secret#frag",
                    "url_after": "https://example.com/callback?code=secret",
                    "revision": 3,
                },
            )
            event = browser_io_page(root, run_id, task_id)["events"][0]

        self.assertEqual(event["url_before"], "https://example.com/…")
        self.assertEqual(event["url_after"], "https://example.com/…")
        self.assertNotIn("text", event)
        self.assertNotIn("secret", str(event))
        self.assertNotIn("alice", str(event))

    def test_redact_browser_url_preserves_only_safe_origin(self) -> None:
        self.assertEqual(redact_browser_url("about:blank"), "about:blank")
        self.assertEqual(redact_browser_url("http://localhost:8080/"), "http://localhost:8080/")
        self.assertEqual(redact_browser_url("javascript:alert(1)"), "")
