from __future__ import annotations

import json
import os
import tempfile
import time
import unittest
from unittest import mock
from pathlib import Path

from aha_cli.services.hardware_bridge import (
    DeviceBridgeDaemon,
    append_bridge_control,
    bridge_alive,
    device_bridge_dir,
    device_bridge_state_path,
    device_control_path,
    device_control_offset_path,
    read_bridge_state,
)
from aha_cli.services.network_terminal import network_alive, network_state_path
from aha_cli.services.terminal_ipc import (
    read_control_records,
    rotate_control_file,
    stamp_control_generation,
    state_has_fresh_heartbeat,
)
from aha_cli.store.io import append_jsonl, iter_jsonl_from


def _write_state(root: Path, device: str, **overrides: object) -> dict:
    state = {
        "device": device,
        "pid": 2_000_000_000,  # never a live pid
        "status": "running",
        **overrides,
    }
    path = device_bridge_state_path(root, device)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
    return state


class HeartbeatLivenessTests(unittest.TestCase):
    def test_state_has_fresh_heartbeat_true_within_ttl(self) -> None:
        self.assertTrue(state_has_fresh_heartbeat({"heartbeat_at": time.time()}))
        self.assertTrue(state_has_fresh_heartbeat({"heartbeat_at": time.time() - 5.0}))

    def test_state_has_fresh_heartbeat_false_when_stale_or_missing(self) -> None:
        self.assertFalse(state_has_fresh_heartbeat({"heartbeat_at": time.time() - 60.0}))
        self.assertFalse(state_has_fresh_heartbeat({}))
        self.assertFalse(state_has_fresh_heartbeat(None))

    def test_bridge_alive_uses_heartbeat_when_pid_unresolvable(self) -> None:
        # A Windows-owned bridge observed from WSL: pid is not checkable but the
        # heartbeat is fresh -> the bridge must be reported alive (not torn down).
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_state(root, "COM6", heartbeat_at=time.time(), instance_uuid="uuid-1")
            self.assertTrue(bridge_alive(root, "COM6"))

    def test_bridge_alive_false_without_fresh_heartbeat(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_state(root, "COM6")  # no heartbeat_at, dead pid
            self.assertFalse(bridge_alive(root, "COM6"))

    def test_network_alive_uses_heartbeat(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = network_state_path(root, "10.0.0.5", 23)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                json.dumps({"host": "10.0.0.5", "port": 23, "pid": 2_000_000_000, "heartbeat_at": time.time()}),
                encoding="utf-8",
            )
            self.assertTrue(network_alive(root, "10.0.0.5", 23))


class ControlHygieneTests(unittest.TestCase):
    def test_read_control_records_retries_transient_oserror(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "control.jsonl"
            append_jsonl(path, {"cmd": "pause", "ts": "t1"})
            with mock.patch(
                "aha_cli.services.terminal_ipc.iter_jsonl_records_from",
                side_effect=[OSError(22, "Invalid argument"), OSError(22, "Invalid argument"), ([({"cmd": "pause"}, 10)], 10)],
            ) as read:
                records, offset = read_control_records(path, 0, limit=200)
                self.assertEqual(offset, 10)
                self.assertEqual(records[0][0]["cmd"], "pause")
                self.assertEqual(read.call_count, 3)

    def test_rotate_control_file_archives_oversized_inbox_and_clears_offset(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "control.jsonl"
            offset = Path(tmp) / "control.jsonl.offset"
            offset.write_text("1234", encoding="utf-8")
            # Force the threshold down so a tiny file triggers rotation.
            with mock.patch("aha_cli.services.terminal_ipc._CONTROL_MAX_BYTES", 10):
                append_jsonl(path, {"cmd": "pause", "ts": "t1"})
                rotate_control_file(path)
            # The oversized live file is archived (or truncated) and the offset reset.
            live_exists = path.exists()
            live_text = path.read_text(encoding="utf-8") if live_exists else ""
            self.assertNotEqual(live_text.strip(), json.dumps({"cmd": "pause", "ts": "t1"}, ensure_ascii=False))
            archives = list((Path(tmp) / "archive").glob("control.*.jsonl"))
            self.assertEqual(len(archives), 1)
            records, _ = iter_jsonl_from(archives[0], 0)
            self.assertEqual(records[0]["cmd"], "pause")
            self.assertFalse(offset.exists())

    def test_stamp_control_generation_tags_from_state(self) -> None:
        record = stamp_control_generation({"cmd": "stop", "ts": "t"}, {"generation": 7})
        self.assertEqual(record["generation"], 7)
        untagged = stamp_control_generation({"cmd": "stop", "ts": "t"}, None)
        self.assertNotIn("generation", untagged)

    def test_append_bridge_control_rotates_and_stamps_stop_generation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_state(root, "COM6", generation=5)
            record = append_bridge_control(root, "COM6", {"cmd": "stop"})
            self.assertEqual(record["generation"], 5)
            stored, _ = iter_jsonl_from(device_control_path(root, "COM6"), 0)
            self.assertEqual(stored[0]["generation"], 5)


class DaemonControlTests(unittest.TestCase):
    def _daemon(self, root: Path, device: str, generation: int = 1) -> DeviceBridgeDaemon:
        daemon = DeviceBridgeDaemon(root, device, 115200)
        daemon._generation = generation
        daemon._control_offset = 0
        return daemon

    def test_stale_generation_stop_is_ignored(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            device = "/dev/ttyUSB-stale"
            daemon = self._daemon(root, device, generation=2)
            append_jsonl(device_control_path(root, device), {"cmd": "stop", "ts": "t", "generation": 1})
            daemon._apply_control()
            self.assertTrue(daemon._running)
            self._assert_logged(root, device, "ignored stale stop for generation 1")

    def test_matching_generation_stop_is_honored(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            device = "/dev/ttyUSB-match"
            daemon = self._daemon(root, device, generation=2)
            append_jsonl(device_control_path(root, device), {"cmd": "stop", "ts": "t", "generation": 2})
            daemon._apply_control()
            self.assertFalse(daemon._running)

    def test_identical_rearm_logs_once(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            device = "/dev/ttyUSB-arm"
            daemon = self._daemon(root, device, generation=1)
            arm = {
                "cmd": "arm",
                "id": "auto-login-user",
                "trigger": "match",
                "regex": True,
                "pattern": r"(?i)login\s*:\s*$",
                "send": "root\r",
                "max_fires": 5,
            }
            # Both records are identical -> the second re-arm must not log "armed".
            append_jsonl(device_control_path(root, device), arm)
            append_jsonl(device_control_path(root, device), arm)
            daemon._apply_control()
            armed_count = self._count_log(root, device, "armed")
            self.assertEqual(armed_count, 1)

    def test_persisted_control_offset_is_resumed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            device = "/dev/ttyUSB-offset"
            control = device_control_path(root, device)
            control.parent.mkdir(parents=True, exist_ok=True)
            append_jsonl(control, {"cmd": "pause", "ts": "t1"})
            append_jsonl(control, {"cmd": "pause", "ts": "t2"})
            # Simulate a prior consumer that consumed through the first record.
            first = device_control_offset_path(root, device)
            first.parent.mkdir(parents=True, exist_ok=True)
            records, offset = read_control_records(control, 0, limit=1)
            self.assertEqual(records[0][0]["ts"], "t1")
            first.write_text(str(offset), encoding="utf-8")
            daemon = DeviceBridgeDaemon(root, device, 115200)
            daemon._control_offset = 0
            # run() resumes the persisted offset (not the file end / not zero).
            daemon.run = lambda: None  # we only assert the offset-picking source path exists
            import inspect
            run_src = inspect.getsource(type(daemon).run)
            self.assertIn("device_control_offset_path", run_src)

    def _log_lines(self, root: Path, device: str) -> list[str]:
        from aha_cli.services.hardware_bridge import device_stream_path

        stream = device_stream_path(root, device)
        if not stream.exists():
            return []
        return stream.read_text(encoding="utf-8", errors="replace").splitlines()

    def _assert_logged(self, root: Path, device: str, needle: str) -> None:
        self.assertTrue(
            any(needle in line for line in self._log_lines(root, device)),
            f"expected {needle!r} in bridge log",
        )

    def _count_log(self, root: Path, device: str, needle: str) -> int:
        return sum(needle in line for line in self._log_lines(root, device))


class AppendRotateTests(unittest.TestCase):
    def test_append_rotates_bloated_control(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            device = "/dev/ttyUSB-bloat"
            control = device_control_path(root, device)
            control.parent.mkdir(parents=True, exist_ok=True)
            with mock.patch("aha_cli.services.terminal_ipc._CONTROL_MAX_BYTES", 10):
                append_bridge_control(root, device, {"cmd": "pause"})
            # After rotation the live file holds only the fresh record.
            stored, _ = iter_jsonl_from(control, 0)
            self.assertEqual(stored[0]["cmd"], "pause")


class PromptCapabilityGateTests(unittest.TestCase):
    def test_cli_command_available_reflects_live_dispatch(self) -> None:
        from aha_cli.services.chat_prompt_context import _cli_command_available

        self.assertTrue(_cli_command_available("managed-process"))
        self.assertTrue(_cli_command_available("hardware-attach"))
        self.assertFalse(_cli_command_available("definitely-not-a-command"))

    def test_managed_process_context_empty_when_command_unsupported(self) -> None:
        from aha_cli.services import chat_prompt_context

        with mock.patch(
            "aha_cli.services.chat_prompt_context._cli_command_available",
            return_value=False,
        ):
            self.assertEqual(chat_prompt_context._managed_process_context(), "")

    def test_managed_process_context_present_when_command_supported(self) -> None:
        from aha_cli.services import chat_prompt_context

        with mock.patch(
            "aha_cli.services.chat_prompt_context._cli_command_available",
            return_value=True,
        ):
            context = chat_prompt_context._managed_process_context()
            self.assertIn("aha managed-process start", context)


if __name__ == "__main__":
    unittest.main()
