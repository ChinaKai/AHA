from __future__ import annotations

import json
import os
import select
import signal
import socket
import threading
import time
import unittest
from unittest import mock
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
    device_terminal_socket_path,
    ensure_bridge,
    pid_alive,
    read_bridge_state,
)
from aha_cli.store.io import append_jsonl, iter_jsonl_from
from aha_cli.services.serial_lock import SerialDeviceBusyError


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

    def test_pid_alive_rejects_zombie_process(self) -> None:
        pid = os.fork()
        if pid == 0:
            os._exit(0)
        try:
            self.assertTrue(_wait_until(
                lambda: Path(f"/proc/{pid}/stat").read_text(encoding="utf-8").split(") ", 1)[1].startswith("Z"),
                timeout=2.0,
            ))
            self.assertFalse(pid_alive(pid))
        finally:
            os.waitpid(pid, 0)


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

    def test_live_external_lock_keeps_bridge_blocked_without_opening_device(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            device = "/dev/ttyUSB-occupied"
            owner = {
                "path": "/run/lock/LCK..ttyUSB-occupied",
                "pid": 321,
                "alive": True,
                "process": "minicom",
            }
            daemon = DeviceBridgeDaemon(root, device, 115200, poll_interval=0.005, self_reap=False)
            with mock.patch(
                "aha_cli.services.hardware_bridge.open_uart_transport",
                side_effect=SerialDeviceBusyError(device, owner),
            ) as opener:
                thread = threading.Thread(target=daemon.run, daemon=True)
                thread.start()
                try:
                    self.assertTrue(_wait_until(
                        lambda: (read_bridge_state(root, device) or {}).get("status") == "blocked",
                        timeout=2.0,
                    ))
                    status = bridge_status(root, device)
                    self.assertTrue(status["alive"])
                    self.assertEqual(status["status"], "blocked")
                    self.assertEqual(status["device_owner"]["pid"], 321)
                    self.assertIn("minicom", status["error"])
                finally:
                    append_bridge_control(root, device, {"cmd": "stop"})
                    thread.join(timeout=2.0)
            self.assertGreaterEqual(opener.call_count, 1)

    def test_realtime_ipc_pushes_rx_and_sends_input_without_jsonl_polling(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            master, device = self._pty()
            daemon = DeviceBridgeDaemon(root, device, 115200, poll_interval=0.02, self_reap=False)
            thread = threading.Thread(target=daemon.run, daemon=True)
            thread.start()
            client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            try:
                ipc_path = device_terminal_socket_path(root, device)
                self.assertTrue(_wait_until(ipc_path.exists, timeout=2.0))
                client.settimeout(2.0)
                client.connect(str(ipc_path))
                stream = client.makefile("rb")
                hello = json.loads(stream.readline())
                self.assertEqual(hello["type"], "ready")

                client.sendall(b'{"type":"input","data":"help\\r"}\n')
                self.assertIn(b"help\r", _await_bytes(master, b"help\r", timeout=1.0))

                burst = b"x" * 2048
                client.sendall(b'{"type":"input","data":"x"}\n' * len(burst))
                self.assertEqual(_await_bytes(master, burst, timeout=4.0), burst)

                os.write(master, b"\x1b[32mREADY\x1b[0m\r\n")
                messages = []
                while not any(item.get("type") == "output" for item in messages):
                    messages.append(json.loads(stream.readline()))
                output = next(item for item in messages if item.get("type") == "output")
                self.assertIn("\x1b[32mREADY", output["data"])
                self.assertGreater(output["offset"], hello["after_offset"])
            finally:
                client.close()
                append_bridge_control(root, device, {"cmd": "stop"})
                thread.join(timeout=2.0)
                os.close(master)

    def test_tx_queue_retries_partial_and_temporarily_blocked_writes(self) -> None:
        class PartialTransport:
            def __init__(self) -> None:
                self.received = bytearray()
                self.results = iter((2, 0, 2, 2))

            def write(self, data: bytes) -> int:
                accepted = min(next(self.results), len(data))
                self.received.extend(data[:accepted])
                return accepted

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            daemon = DeviceBridgeDaemon(root, "/dev/test-partial", tx_byte_interval=0, self_reap=False)
            transport = PartialTransport()
            daemon.transport = transport

            daemon._send("abcdef", source="test")
            self.assertEqual(bytes(transport.received), b"ab")
            self.assertEqual(daemon._tx_pending_bytes, 4)
            daemon._flush_tx()

            self.assertEqual(bytes(transport.received), b"abcdef")
            self.assertEqual(daemon._tx_pending_bytes, 0)
            tx = [row for row in self._rows(root, daemon.device) if row.get("direction") == "tx"]
            self.assertEqual([(row["data"], row["source"]) for row in tx], [("abcdef", "test")])

    def test_tx_queue_paces_serial_bytes_without_delaying_first_byte(self) -> None:
        class Clock:
            now = 10.0

            def __call__(self) -> float:
                return self.now

        class RecordingTransport:
            def __init__(self) -> None:
                self.writes: list[bytes] = []

            def write(self, data: bytes) -> int:
                self.writes.append(data)
                return len(data)

        with tempfile.TemporaryDirectory() as tmp:
            clock = Clock()
            daemon = DeviceBridgeDaemon(
                Path(tmp),
                "/dev/test-paced",
                clock=clock,
                tx_byte_interval=0.001,
                self_reap=False,
            )
            transport = RecordingTransport()
            daemon.transport = transport

            daemon._send("abc", source="test")
            self.assertEqual(transport.writes, [b"a"])
            daemon._flush_tx()
            self.assertEqual(transport.writes, [b"a"])

            clock.now += 0.001
            daemon._flush_tx()
            self.assertEqual(transport.writes, [b"a", b"b"])
            clock.now += 0.001
            daemon._flush_tx()
            self.assertEqual(transport.writes, [b"a", b"b", b"c"])
            self.assertEqual(daemon._tx_pending_bytes, 0)

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
            offsets = []
            for i in range(50):
                offsets.append(append_jsonl(stream, {"ts": f"t{i:03d}", "direction": "rx", "data": f"line{i}"}))

            page = device_stream_page(root, device, after=None, limit=10)
            self.assertEqual(len(page["events"]), 10)
            self.assertFalse(page["has_more"])
            self.assertEqual(page["events"][0]["ts"], "t040")
            self.assertEqual(page["events"][-1]["ts"], "t049")  # newest record is shown

            # Incremental follow from an offset still returns forward records.
            follow = device_stream_page(root, device, after=0, limit=5)
            self.assertEqual([e["ts"] for e in follow["events"]], ["t000", "t001", "t002", "t003", "t004"])
            self.assertTrue(follow["has_more"])

            replay = device_stream_page(root, device, before=offsets[44], limit=5)
            self.assertEqual([e["ts"] for e in replay["events"]], ["t040", "t041", "t042", "t043", "t044"])
            self.assertEqual(replay["after_offset"], offsets[44])


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
