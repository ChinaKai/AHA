from __future__ import annotations

import json
import os
import select
import signal
import threading
import time
import unittest
from pathlib import Path
import tempfile

from aha_cli.services.hardware_bridge import (
    DeviceBridgeDaemon,
    append_bridge_control,
    bridge_status,
    device_bridge_state_path,
    device_key,
    device_stream_page,
    device_stream_path,
    ensure_bridge,
    pid_alive,
    read_bridge_state,
)
from aha_cli.store.io import append_jsonl, iter_jsonl_from


def _await_bytes(fd: int, needle: bytes, *, timeout: float) -> bytes:
    deadline = time.time() + timeout
    buffer = b""
    while time.time() < deadline:
        readable, _, _ = select.select([fd], [], [], 0.05)
        if readable:
            try:
                buffer += os.read(fd, 1024)
            except OSError:
                break
            if needle in buffer:
                break
    return buffer


def _wait_until(predicate, *, timeout: float) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return False


class DeviceKeyTests(unittest.TestCase):
    def test_device_key_is_filesystem_safe(self) -> None:
        self.assertEqual(device_key("/dev/ttyUSB0"), "dev-ttyUSB0")
        self.assertEqual(device_key("/dev/serial/by-id/usb-X"), "dev-serial-by-id-usb-X")
        self.assertEqual(device_key(""), "device")

    def test_pid_alive(self) -> None:
        self.assertTrue(pid_alive(os.getpid()))
        self.assertFalse(pid_alive(0))
        self.assertFalse(pid_alive(2_000_000_000))


class DeviceBridgeDaemonTests(unittest.TestCase):
    def _pty(self):
        master, slave = os.openpty()
        path = os.ttyname(slave)
        os.close(slave)  # let the bridge open the device path itself (like a real port)
        return master, path

    def _rows(self, root: Path, device: str) -> list[dict]:
        rows, _ = iter_jsonl_from(device_stream_path(root, device), 0)
        return rows

    def test_streams_rx_and_interactive_send(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            master, device = self._pty()
            daemon = DeviceBridgeDaemon(root, device, 115200, poll_interval=0.005, self_reap=False)
            thread = threading.Thread(target=daemon.run, daemon=True)
            thread.start()
            try:
                self.assertTrue(_wait_until(lambda: (read_bridge_state(root, device) or {}).get("status") == "running", timeout=2.0))
                os.write(master, b"Linux boot log\r\n# ")
                self.assertTrue(_wait_until(lambda: any(r["direction"] == "rx" for r in self._rows(root, device)), timeout=2.0))
                append_bridge_control(root, device, {"cmd": "send", "data": "ps\\r"})
                injected = _await_bytes(master, b"ps\r", timeout=2.0)
                self.assertIn(b"ps\r", injected)
            finally:
                append_bridge_control(root, device, {"cmd": "stop"})
                thread.join(timeout=2.0)
                os.close(master)

            rows = self._rows(root, device)
            self.assertTrue(any(r["direction"] == "rx" and "boot log" in r["data"] for r in rows))
            tx = [r for r in rows if r["direction"] == "tx"]
            self.assertTrue(any(r["data"] == "ps\r" and r.get("source") == "interactive" for r in tx))
            self.assertEqual(read_bridge_state(root, device)["status"], "stopped")

    def test_rx_utf8_split_across_reads_decodes_cleanly(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            master, device = self._pty()
            daemon = DeviceBridgeDaemon(root, device, 115200, poll_interval=0.005, self_reap=False)
            thread = threading.Thread(target=daemon.run, daemon=True)
            thread.start()
            try:
                self.assertTrue(_wait_until(lambda: (read_bridge_state(root, device) or {}).get("status") == "running", timeout=2.0))
                # "中" is 3 UTF-8 bytes; split it so the bridge must carry the partial
                # bytes across two separate reads instead of emitting replacement chars.
                blob = "状态中文".encode("utf-8")
                os.write(master, blob[:5])
                time.sleep(0.1)
                os.write(master, blob[5:])
                self.assertTrue(_wait_until(
                    lambda: "".join(r["data"] for r in self._rows(root, device) if r["direction"] == "rx").find("状态中文") >= 0,
                    timeout=2.0,
                ))
            finally:
                append_bridge_control(root, device, {"cmd": "stop"})
                thread.join(timeout=2.0)
                os.close(master)
            rx_text = "".join(r["data"] for r in self._rows(root, device) if r["direction"] == "rx")
            self.assertIn("状态中文", rx_text)
            self.assertNotIn("�", rx_text)

    def test_pause_releases_port_then_resume(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            master, device = self._pty()
            daemon = DeviceBridgeDaemon(root, device, 115200, poll_interval=0.005, self_reap=False)
            thread = threading.Thread(target=daemon.run, daemon=True)
            thread.start()
            try:
                self.assertTrue(_wait_until(lambda: (read_bridge_state(root, device) or {}).get("status") == "running", timeout=2.0))

                append_bridge_control(root, device, {"cmd": "pause"})
                self.assertTrue(_wait_until(lambda: (read_bridge_state(root, device) or {}).get("status") == "paused", timeout=2.0))

                # A send while paused must be ignored (port is released).
                append_bridge_control(root, device, {"cmd": "send", "data": "ghost\\r"})
                time.sleep(0.2)
                leaked = _await_bytes(master, b"ghost", timeout=0.3)
                self.assertNotIn(b"ghost", leaked)

                append_bridge_control(root, device, {"cmd": "resume"})
                self.assertTrue(_wait_until(lambda: (read_bridge_state(root, device) or {}).get("status") == "running", timeout=2.0))
                append_bridge_control(root, device, {"cmd": "send", "data": "live\\r"})
                self.assertIn(b"live\r", _await_bytes(master, b"live\r", timeout=2.0))
            finally:
                append_bridge_control(root, device, {"cmd": "stop"})
                thread.join(timeout=2.0)
                os.close(master)

    def test_armed_rule_fires_locally(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            master, device = self._pty()
            daemon = DeviceBridgeDaemon(root, device, 115200, poll_interval=0.005, self_reap=False)
            thread = threading.Thread(target=daemon.run, daemon=True)
            thread.start()
            try:
                self.assertTrue(_wait_until(lambda: (read_bridge_state(root, device) or {}).get("status") == "running", timeout=2.0))
                append_bridge_control(root, device, {"cmd": "arm", "pattern": "stop autoboot", "send": "\\r", "max_fires": 1})
                self.assertTrue(_wait_until(lambda: any(r.get("source", "").startswith("rule:") for r in self._rows(root, device)), timeout=2.0))
                os.write(master, b"Hit any key to stop autoboot:  2 ")
                self.assertIn(b"\r", _await_bytes(master, b"\r", timeout=2.0))
            finally:
                append_bridge_control(root, device, {"cmd": "stop"})
                thread.join(timeout=2.0)
                os.close(master)
            tx = [r for r in self._rows(root, device) if r["direction"] == "tx"]
            self.assertTrue(any(r.get("source", "").startswith("rule:") for r in tx))


class SelfReapTests(unittest.TestCase):
    def test_bridge_reaps_when_no_active_task_references_device(self) -> None:
        from aha_cli.store.filesystem import (
            complete_task,
            create_plan,
            update_task_hardware_debug_config,
        )

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            master, slave = os.openpty()
            device = os.ttyname(slave)
            os.close(slave)
            plan = create_plan(root, "Bridge reap", 1, "research", ["Board"], [])
            run_id = str(plan["id"])
            update_task_hardware_debug_config(
                root, run_id, "task-001",
                channels=[{"type": "uart", "settings": {"port": device, "baudrate": 115200}}],
            )
            daemon = DeviceBridgeDaemon(root, device, 115200, poll_interval=0.005, reap_interval=0.2)
            thread = threading.Thread(target=daemon.run, daemon=True)
            thread.start()
            try:
                # Stays alive while the task is non-terminal.
                self.assertTrue(_wait_until(lambda: (read_bridge_state(root, device) or {}).get("status") == "running", timeout=2.0))
                time.sleep(0.5)
                self.assertEqual((read_bridge_state(root, device) or {}).get("status"), "running")
                # Task goes terminal -> bridge self-reaps and releases the port.
                complete_task(root, run_id, "task-001")
                self.assertTrue(_wait_until(lambda: (read_bridge_state(root, device) or {}).get("status") == "stopped", timeout=3.0))
            finally:
                daemon._running = False
                thread.join(timeout=2.0)
                os.close(master)


class DeviceStreamPageTests(unittest.TestCase):
    def test_initial_load_returns_newest_records_not_oldest(self) -> None:
        # Regression: a console with more records than the page limit must show the LATEST
        # window on the initial (after=None) load, otherwise the view freezes on the oldest
        # page and never displays new serial output.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            device = "/dev/ttyUSB-tail"
            stream = device_stream_path(root, device)
            stream.parent.mkdir(parents=True, exist_ok=True)
            for i in range(50):
                append_jsonl(stream, {"ts": f"t{i:03d}", "direction": "rx", "data": f"line{i}"})

            page = device_stream_page(root, device, after=None, limit=10)
            self.assertEqual(len(page["events"]), 10)
            self.assertFalse(page["has_more"])
            self.assertEqual(page["events"][0]["ts"], "t040")
            self.assertEqual(page["events"][-1]["ts"], "t049")  # newest record is shown

            # Incremental follow from an offset still returns forward records.
            follow = device_stream_page(root, device, after=0, limit=5)
            self.assertEqual([e["ts"] for e in follow["events"]], ["t000", "t001", "t002", "t003", "t004"])
            self.assertTrue(follow["has_more"])


class BridgeStatusTests(unittest.TestCase):
    def test_stale_pid_reports_stopped(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            device = "/dev/ttyUSB9"
            path = device_bridge_state_path(root, device)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps({"device": device, "pid": 2_000_000_000, "status": "running"}), encoding="utf-8")
            status = bridge_status(root, device)
            self.assertFalse(status["alive"])
            self.assertEqual(status["status"], "stopped")


class EnsureBridgeTests(unittest.TestCase):
    def test_ensure_spawns_managed_bridge_and_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            master, slave = os.openpty()
            device = os.ttyname(slave)
            os.close(slave)
            pid = None
            try:
                first = ensure_bridge(root, device, 115200)
                self.assertTrue(first["alive"])
                self.assertTrue(_wait_until(lambda: (read_bridge_state(root, device) or {}).get("status") == "running", timeout=4.0))
                pid = read_bridge_state(root, device)["pid"]
                self.assertTrue(pid_alive(pid))
                # Idempotent: a second ensure reuses the same live bridge.
                second = ensure_bridge(root, device, 115200)
                self.assertEqual(second["pid"], pid)
            finally:
                if pid:
                    try:
                        os.kill(int(pid), signal.SIGTERM)
                    except OSError:
                        pass
                os.close(master)


if __name__ == "__main__":
    unittest.main()
