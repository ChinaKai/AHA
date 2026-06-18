from __future__ import annotations

import os
import select
import threading
import time
import tty
import unittest
from pathlib import Path
import tempfile

from aha_cli.cli import main as cli_main
from aha_cli.services.hardware_io import hardware_io_path
from aha_cli.services.hardware_session import (
    ArmedRuleEngine,
    FdTransport,
    HardwareSessionDaemon,
    append_session_control,
    decode_escapes,
    hardware_session_control_path,
    read_session_state,
)
from aha_cli.store.filesystem import create_plan
from aha_cli.store.io import iter_jsonl_from


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class DecodeEscapesTests(unittest.TestCase):
    def test_common_escapes(self) -> None:
        self.assertEqual(decode_escapes("reset\\r"), "reset\r")
        self.assertEqual(decode_escapes("a\\tb\\n"), "a\tb\n")
        self.assertEqual(decode_escapes("\\x03"), "\x03")  # Ctrl-C
        self.assertEqual(decode_escapes("plain"), "plain")
        self.assertEqual(decode_escapes("back\\\\slash"), "back\\slash")


class ArmedRuleEngineTests(unittest.TestCase):
    def test_match_rule_fires_once_and_autodisarms(self) -> None:
        clock = FakeClock()
        engine = ArmedRuleEngine(clock=clock)
        engine.arm({"pattern": "stop autoboot", "send": "\\r", "max_fires": 1})

        fired, _ = engine.on_text("U-Boot 2020.04\nHit any key to ")
        self.assertEqual(fired, [])
        fired, _ = engine.on_text("stop autoboot:  2 ")
        self.assertEqual(len(fired), 1)
        self.assertEqual(fired[0]["send"], "\r")
        self.assertEqual(engine.rules, [])  # auto-disarmed after the single fire

        # A later occurrence must not re-fire a disarmed rule.
        fired, _ = engine.on_text("stop autoboot again")
        self.assertEqual(fired, [])

    def test_regex_rule(self) -> None:
        engine = ArmedRuleEngine(clock=FakeClock())
        engine.arm({"pattern": r"login:\s*$", "regex": True, "send": "root\\r", "max_fires": 0})
        fired, _ = engine.on_text("Starting kernel ...\n")
        self.assertEqual(fired, [])
        fired, _ = engine.on_text("buildroot login: ")
        self.assertEqual(len(fired), 1)
        self.assertEqual(fired[0]["send"], "root\r")

    def test_timer_rule_repeats_then_expires_on_duration(self) -> None:
        clock = FakeClock()
        engine = ArmedRuleEngine(clock=clock)
        # "spam \r every 100ms for 300ms right after reset"
        engine.arm({"interval_seconds": 0.1, "duration_seconds": 0.3, "send": "\\r", "max_fires": 0})

        total = 0
        for _ in range(6):
            clock.advance(0.1)
            fired, expired = engine.on_tick()
            total += len(fired)
        self.assertGreaterEqual(total, 2)
        self.assertEqual(engine.rules, [])  # duration elapsed -> disarmed

    def test_ttl_expiry(self) -> None:
        clock = FakeClock()
        engine = ArmedRuleEngine(clock=clock)
        engine.arm({"pattern": "never", "send": "x", "ttl_seconds": 1.0})
        clock.advance(1.5)
        fired, expired = engine.on_text("nothing relevant")
        self.assertEqual(fired, [])
        self.assertEqual(len(expired), 1)
        self.assertEqual(expired[0][1], "ttl")
        self.assertEqual(engine.rules, [])

    def test_disarm_by_id(self) -> None:
        engine = ArmedRuleEngine(clock=FakeClock())
        rule = engine.arm({"id": "interrupt", "pattern": "x", "send": "y"})
        self.assertEqual(rule["id"], "interrupt")
        self.assertEqual(engine.disarm("interrupt"), 1)
        self.assertEqual(engine.disarm("interrupt"), 0)


class HardwareSessionDaemonTests(unittest.TestCase):
    def _bootstrap(self, root: Path) -> str:
        plan = create_plan(root, "Hardware session", 1, "research", ["Board bringup"], [])
        return str(plan["id"])

    def _read_rows(self, root: Path, run_id: str, task_id: str) -> list[dict]:
        rows, _ = iter_jsonl_from(hardware_io_path(root, run_id, task_id), 0)
        return rows

    def test_daemon_streams_rx_and_fires_armed_rule_over_pty(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            run_id = self._bootstrap(root)
            task_id = "task-001"

            master, slave = os.openpty()
            tty.setraw(slave)  # disable echo so board output is not bounced back
            transport = FdTransport(slave, close_fd=True)
            daemon = HardwareSessionDaemon(
                root, run_id, task_id, "uart", transport, endpoint="pty", poll_interval=0.005
            )

            # Agent arms the interrupt BEFORE the board reaches the countdown window.
            append_session_control(
                root, run_id, task_id, "uart",
                {"cmd": "arm", "pattern": "stop autoboot", "send": "\\r", "max_fires": 1},
            )

            thread = threading.Thread(target=daemon.run, daemon=True)
            thread.start()

            try:
                # Board powers up and streams its log; the countdown appears mid-stream.
                os.write(master, b"U-Boot 2020.04\r\n")
                time.sleep(0.05)
                os.write(master, b"Hit any key to stop autoboot:  2 \r\n")

                injected = self._await_bytes(master, b"\r", timeout=2.0)
                self.assertIn(b"\r", injected)
            finally:
                append_session_control(root, run_id, task_id, "uart", {"cmd": "stop"})
                thread.join(timeout=2.0)
                os.close(master)

            self.assertFalse(thread.is_alive())
            rows = self._read_rows(root, run_id, task_id)
            rx = [r for r in rows if r["direction"] == "rx"]
            tx = [r for r in rows if r["direction"] == "tx"]
            self.assertTrue(any("stop autoboot" in r["data"] for r in rx))
            self.assertTrue(any(r.get("source", "").startswith("rule:") and r["data"] == "\r" for r in tx))

            state = read_session_state(root, run_id, task_id, "uart")
            self.assertIsNotNone(state)
            self.assertEqual(state["status"], "stopped")

    def test_daemon_interactive_send(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            run_id = self._bootstrap(root)
            task_id = "task-001"

            master, slave = os.openpty()
            tty.setraw(slave)
            transport = FdTransport(slave, close_fd=True)
            daemon = HardwareSessionDaemon(
                root, run_id, task_id, "uart", transport, endpoint="pty", poll_interval=0.005
            )
            thread = threading.Thread(target=daemon.run, daemon=True)
            thread.start()
            try:
                append_session_control(
                    root, run_id, task_id, "uart", {"cmd": "send", "data": "printenv\\r"}
                )
                injected = self._await_bytes(master, b"printenv\r", timeout=2.0)
                self.assertIn(b"printenv\r", injected)
            finally:
                append_session_control(root, run_id, task_id, "uart", {"cmd": "stop"})
                thread.join(timeout=2.0)
                os.close(master)

            rows = self._read_rows(root, run_id, task_id)
            tx = [r for r in rows if r["direction"] == "tx"]
            self.assertTrue(any(r["data"] == "printenv\r" and r.get("source") == "interactive" for r in tx))

    def test_cli_commands_queue_control_records(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            run_id = self._bootstrap(root)
            task_id = "task-001"
            home = str(root / ".aha")

            self.assertEqual(
                cli_main(["--home", home, "hardware-arm", run_id, task_id, "--channel", "uart",
                          "--pattern", "stop autoboot", "--send", "\\r", "--max-fires", "1"]),
                0,
            )
            self.assertEqual(
                cli_main(["--home", home, "hardware-send", run_id, task_id, "--channel", "uart",
                          "--data", "printenv\\r"]),
                0,
            )
            self.assertEqual(
                cli_main(["--home", home, "hardware-stop", run_id, task_id, "--channel", "uart"]),
                0,
            )

            rows, _ = iter_jsonl_from(hardware_session_control_path(root, run_id, task_id, "uart"), 0)
            cmds = [row["cmd"] for row in rows]
            self.assertEqual(cmds, ["arm", "send", "stop"])
            arm = rows[0]
            self.assertEqual(arm["pattern"], "stop autoboot")
            self.assertEqual(arm["send"], "\\r")
            self.assertEqual(arm["max_fires"], 1)

    def _await_bytes(self, fd: int, needle: bytes, *, timeout: float) -> bytes:
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


if __name__ == "__main__":
    unittest.main()
