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
    bridge_status,
    device_bridge_state_path,
    device_control_path,
    device_control_offset_path,
    device_lock_path,
    ensure_bridge,
    read_bridge_state,
)
from aha_cli.services.network_terminal import (
    NetworkTerminalDaemon,
    append_network_control,
    ensure_network_terminal,
    network_alive,
    network_control_path,
    network_state_path,
    network_status,
    network_terminal_dir,
)
from aha_cli.services.terminal_ipc import (
    current_pid_platform,
    read_control_records,
    rotate_control_file,
    stamp_control_generation,
    state_has_fresh_heartbeat,
    state_liveness_source,
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
            self.assertEqual(bridge_status(root, "COM6")["liveness_source"], "heartbeat")

    def test_bridge_alive_false_without_fresh_heartbeat(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_state(root, "COM6")  # no heartbeat_at, dead pid
            self.assertFalse(bridge_alive(root, "COM6"))

    def test_bridge_alive_false_when_stopped_even_with_fresh_heartbeat(self) -> None:
        # The stop path writes a fresh heartbeat in its final state write; a
        # stopped bridge must never be reported alive or `attach` would refuse to
        # spawn a replacement.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_state(root, "COM6", status="stopped", heartbeat_at=time.time())
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
            self.assertEqual(network_status(root, "10.0.0.5", 23)["liveness_source"], "heartbeat")

    def test_foreign_pid_platform_uses_heartbeat_without_local_pid_probe(self) -> None:
        foreign = "windows" if current_pid_platform() == "posix" else "posix"
        pid_checker = mock.Mock(return_value=True)
        source = state_liveness_source(
            {"status": "running", "pid": os.getpid(), "pid_platform": foreign, "heartbeat_at": time.time()},
            pid_checker,
        )
        self.assertEqual(source, "heartbeat")
        pid_checker.assert_not_called()

    def test_stale_provisional_starting_state_expires(self) -> None:
        source = state_liveness_source(
            {"status": "starting", "pid": 0, "heartbeat_at": time.time() - 9.0},
            lambda _pid: False,
        )
        self.assertEqual(source, "")

    def test_network_alive_false_when_stopped_even_with_fresh_heartbeat(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = network_state_path(root, "10.0.0.5", 23)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                json.dumps({"host": "10.0.0.5", "port": 23, "pid": 2_000_000_000, "status": "stopped", "heartbeat_at": time.time()}),
                encoding="utf-8",
            )
            self.assertFalse(network_alive(root, "10.0.0.5", 23))


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

    def test_read_control_records_resets_offset_after_rotation(self) -> None:
        """After the control inbox is rotated (archived + reset), the consumer's
        offset into the old large file must not pin reads to the new file's end;
        the read restarts from 0 so records appended after rotation are consumed."""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "control.jsonl"
            # Simulate the old large file: the consumer offset points deep inside.
            old_size = 6 * 1024 * 1024
            with path.open("ab") as f:
                f.write(b"\n" * old_size)  # sparse-ish old file
            records, offset = read_control_records(path, old_size - 100, limit=200)
            # Old file was the same size -> no reset, offset stays at end.
            self.assertEqual(offset, old_size)

            # Now the file is rotated: replaced by a small new file.
            with path.open("wb") as f:
                f.write((json.dumps({"cmd": "pause", "ts": "t1"}) + "\n").encode("utf-8"))
            records, offset = read_control_records(path, old_size, limit=200)
            # The consumer had a large offset; the file shrank -> restart from 0
            # and read the freshly-appended record.
            self.assertEqual(len(records), 1)
            self.assertEqual(records[0][0]["cmd"], "pause")
            self.assertGreater(offset, 0)

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
        record = stamp_control_generation(
            {"cmd": "stop", "ts": "t"},
            {"generation": 7, "instance_uuid": "instance-7"},
        )
        self.assertEqual(record["generation"], 7)
        self.assertEqual(record["instance_uuid"], "instance-7")
        untagged = stamp_control_generation({"cmd": "stop", "ts": "t"}, None)
        self.assertNotIn("generation", untagged)

    def test_append_bridge_control_stamps_every_command_for_current_instance(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_state(root, "COM6", generation=5, instance_uuid="instance-5")
            record = append_bridge_control(root, "COM6", {"cmd": "pause"})
            self.assertEqual(record["generation"], 5)
            self.assertEqual(record["instance_uuid"], "instance-5")
            stored, _ = iter_jsonl_from(device_control_path(root, "COM6"), 0)
            self.assertEqual(stored[0]["generation"], 5)


class DaemonControlTests(unittest.TestCase):
    def _daemon(self, root: Path, device: str, generation: int = 1) -> DeviceBridgeDaemon:
        daemon = DeviceBridgeDaemon(root, device, 115200)
        daemon._generation = generation
        daemon._instance_uuid = f"instance-{generation}"
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

    def test_stale_pause_and_arm_from_previous_instance_are_ignored(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            device = "/dev/ttyUSB-instance"
            daemon = self._daemon(root, device, generation=2)
            append_jsonl(
                device_control_path(root, device),
                {"cmd": "pause", "generation": 1, "instance_uuid": "instance-1"},
            )
            append_jsonl(
                device_control_path(root, device),
                {
                    "cmd": "arm",
                    "id": "stale-rule",
                    "trigger": "match",
                    "pattern": "login:",
                    "send": "root\\r",
                    "generation": 1,
                    "instance_uuid": "instance-1",
                },
            )
            daemon._apply_control()
            self.assertFalse(daemon._paused)
            self.assertEqual(daemon.engine.snapshot(), [])
            self._assert_logged(root, device, "ignored stale pause control record")
            self._assert_logged(root, device, "ignored stale arm control record")

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

    def test_spawn_anchor_ignores_old_offset_and_consumes_only_new_records(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            device = "/dev/ttyUSB-offset"
            control = device_control_path(root, device)
            control.parent.mkdir(parents=True, exist_ok=True)
            append_jsonl(control, {"cmd": "pause", "ts": "t1"})
            append_jsonl(control, {"cmd": "pause", "ts": "t2"})
            anchor = control.stat().st_size
            device_control_offset_path(root, device).write_text("0", encoding="utf-8")
            _write_state(
                root,
                device,
                generation=3,
                instance_uuid="instance-3",
                control_start_offset=anchor,
            )
            append_jsonl(
                control,
                {
                    "cmd": "arm",
                    "id": "new-rule",
                    "trigger": "match",
                    "pattern": "login:",
                    "send": "root\\r",
                    "generation": 3,
                    "instance_uuid": "instance-3",
                },
            )
            daemon = DeviceBridgeDaemon(root, device, 115200, self_reap=False)
            observed_offsets: list[int] = []
            original_apply = daemon._apply_control

            def apply_once() -> None:
                observed_offsets.append(daemon._control_offset)
                original_apply()
                daemon._running = False

            with mock.patch.object(daemon, "_open_port", return_value=False), mock.patch.object(
                daemon, "_apply_control", side_effect=apply_once
            ):
                daemon.run()

            self.assertEqual(observed_offsets, [anchor])
            self.assertEqual([rule["id"] for rule in daemon.engine.snapshot()], ["new-rule"])

    def test_control_read_failure_does_not_stop_daemon_and_recovers(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            device = "/dev/ttyUSB-retry"
            daemon = self._daemon(root, device, generation=4)
            daemon._control_offset = 17
            arm = {
                "cmd": "arm",
                "id": "recovered-rule",
                "trigger": "match",
                "pattern": "login:",
                "send": "root\\r",
                "generation": 4,
                "instance_uuid": "instance-4",
            }
            with mock.patch(
                "aha_cli.services.hardware_bridge.read_control_records",
                side_effect=[OSError(22, "Invalid argument"), ([(arm, 42)], 42)],
            ):
                daemon._apply_control()
                self.assertTrue(daemon._running)
                self.assertEqual(daemon._control_offset, 17)
                daemon._apply_control()

            self.assertEqual(daemon._control_offset, 42)
            self.assertEqual([rule["id"] for rule in daemon.engine.snapshot()], ["recovered-rule"])
            self._assert_logged(root, device, "control inbox read failed; retrying")
            self._assert_logged(root, device, "control inbox read recovered")

    def test_ensure_bridge_writes_instance_anchor_before_spawn_and_recovers_stale_lock(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            device = "COM7"
            control = device_control_path(root, device)
            append_jsonl(control, {"cmd": "pause", "ts": "old"})
            old_size = control.stat().st_size
            lock_path = device_lock_path(root, device)
            lock_path.write_text("stale-owner\n", encoding="ascii")
            old_time = time.time() - 30.0
            os.utime(lock_path, (old_time, old_time))
            captured: dict[str, object] = {}

            def spawn(*_args: object, **_kwargs: object) -> mock.Mock:
                state = read_bridge_state(root, device)
                captured["state"] = dict(state or {})
                captured["record"] = append_bridge_control(root, device, {"cmd": "pause"})
                return mock.Mock(pid=4321)

            with mock.patch("aha_cli.services.hardware_bridge.subprocess.Popen", side_effect=spawn):
                result = ensure_bridge(root, device, launcher=["aha"], detach=True)

            state = captured["state"]
            record = captured["record"]
            self.assertEqual(state["pid"], 0)
            self.assertEqual(state["control_start_offset"], old_size)
            self.assertEqual(record["generation"], state["generation"])
            self.assertEqual(record["instance_uuid"], state["instance_uuid"])
            self.assertEqual(result["pid"], 4321)
            self.assertFalse(lock_path.exists())

    def test_superseded_serial_daemon_does_not_overwrite_new_instance_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            device = "/dev/ttyUSB-superseded"
            _write_state(root, device, instance_uuid="new-instance", generation=2)
            daemon = self._daemon(root, device, generation=1)
            daemon._instance_uuid = "old-instance"
            daemon._write_state("running")
            state = read_bridge_state(root, device)
            self.assertEqual(state["instance_uuid"], "new-instance")
            self.assertFalse(daemon._running)

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


class NetworkControlHygieneTests(unittest.TestCase):
    def test_network_commands_are_stamped_and_old_instance_records_are_ignored(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            host, port = "192.0.2.20", 23
            state_path = network_state_path(root, host, port)
            state_path.parent.mkdir(parents=True, exist_ok=True)
            state_path.write_text(
                json.dumps({"generation": 6, "instance_uuid": "network-6"}),
                encoding="utf-8",
            )
            record = append_network_control(root, host, port, {"cmd": "pause"})
            self.assertEqual(record["generation"], 6)
            self.assertEqual(record["instance_uuid"], "network-6")

            daemon = NetworkTerminalDaemon(root, host, port, self_reap=False)
            daemon._generation = 7
            daemon._instance_uuid = "network-7"
            daemon._apply_control()
            self.assertFalse(daemon._paused)

    def test_network_spawn_anchor_precedes_process_creation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            host, port = "192.0.2.21", 23
            control = network_control_path(root, host, port)
            append_jsonl(control, {"cmd": "pause", "ts": "old"})
            old_size = control.stat().st_size
            lock_path = network_terminal_dir(root, host, port) / "bridge.lock"
            lock_path.parent.mkdir(parents=True, exist_ok=True)
            lock_path.write_text("stale-owner\n", encoding="ascii")
            old_time = time.time() - 30.0
            os.utime(lock_path, (old_time, old_time))
            captured: dict[str, object] = {}

            def spawn(*_args: object, **_kwargs: object) -> mock.Mock:
                state = json.loads(network_state_path(root, host, port).read_text(encoding="utf-8"))
                captured["state"] = state
                captured["record"] = append_network_control(root, host, port, {"cmd": "pause"})
                return mock.Mock(pid=5432)

            with mock.patch("aha_cli.services.network_terminal.subprocess.Popen", side_effect=spawn):
                result = ensure_network_terminal(root, host, port, launcher=["aha"], detach=True)

            state = captured["state"]
            record = captured["record"]
            self.assertEqual(state["pid"], 0)
            self.assertEqual(state["control_start_offset"], old_size)
            self.assertEqual(record["generation"], state["generation"])
            self.assertEqual(record["instance_uuid"], state["instance_uuid"])
            self.assertEqual(result["pid"], 5432)
            self.assertFalse(lock_path.exists())

    def test_network_run_uses_spawn_anchor_instead_of_persisted_offset(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            host, port = "192.0.2.22", 23
            control = network_control_path(root, host, port)
            append_jsonl(control, {"cmd": "pause", "ts": "old"})
            anchor = control.stat().st_size
            state_path = network_state_path(root, host, port)
            state_path.write_text(
                json.dumps(
                    {
                        "host": host,
                        "port": port,
                        "generation": 8,
                        "instance_uuid": "network-8",
                        "control_start_offset": anchor,
                    }
                ),
                encoding="utf-8",
            )
            append_jsonl(
                control,
                {
                    "cmd": "arm",
                    "id": "network-new-rule",
                    "trigger": "match",
                    "pattern": "login:",
                    "send": "root\\r",
                    "generation": 8,
                    "instance_uuid": "network-8",
                },
            )
            daemon = NetworkTerminalDaemon(root, host, port, self_reap=False)
            observed_offsets: list[int] = []
            original_apply = daemon._apply_control

            def apply_once() -> None:
                observed_offsets.append(daemon._control_offset)
                original_apply()
                daemon._running = False

            with mock.patch.object(daemon, "_apply_control", side_effect=apply_once):
                daemon.run()

            self.assertEqual(observed_offsets, [anchor])
            self.assertEqual([rule["id"] for rule in daemon.engine.snapshot()], ["network-new-rule"])

    def test_network_control_read_failure_recovers_without_exit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            host, port = "192.0.2.23", 23
            daemon = NetworkTerminalDaemon(root, host, port, self_reap=False)
            daemon._generation = 9
            daemon._instance_uuid = "network-9"
            daemon._control_offset = 23
            arm = {
                "cmd": "arm",
                "id": "network-recovered-rule",
                "trigger": "match",
                "pattern": "login:",
                "send": "root\\r",
                "generation": 9,
                "instance_uuid": "network-9",
            }
            with mock.patch(
                "aha_cli.services.network_terminal.read_control_records",
                side_effect=[OSError(22, "Invalid argument"), ([(arm, 47)], 47)],
            ):
                daemon._apply_control()
                self.assertTrue(daemon._running)
                self.assertEqual(daemon._control_offset, 23)
                daemon._apply_control()

            self.assertEqual(daemon._control_offset, 47)
            self.assertEqual([rule["id"] for rule in daemon.engine.snapshot()], ["network-recovered-rule"])

    def test_superseded_network_daemon_does_not_overwrite_new_instance_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            host, port = "192.0.2.24", 23
            state_path = network_state_path(root, host, port)
            state_path.parent.mkdir(parents=True, exist_ok=True)
            state_path.write_text(
                json.dumps({"instance_uuid": "new-network", "generation": 2}),
                encoding="utf-8",
            )
            daemon = NetworkTerminalDaemon(root, host, port, self_reap=False)
            daemon._instance_uuid = "old-network"
            daemon._write_state("running")
            state = json.loads(state_path.read_text(encoding="utf-8"))
            self.assertEqual(state["instance_uuid"], "new-network")
            self.assertFalse(daemon._running)

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
