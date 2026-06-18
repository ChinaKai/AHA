"""Machine-level serial bridge: one long-lived process per physical device.

A UART is exclusive, stateful and streams continuously, so it cannot be opened
per-action. The bridge is the single owner of a device (`/dev/ttyUSB0`); every
task / agent / web client reads and writes the board *through* it. Because the
port is physical it is keyed by **device**, not task, and lives at the machine
level (under the AHA home) so tasks across different runs share one bridge and
one stream.

Lifecycle (decided with the user):

* **Owner**: the AHA runtime (the long-lived ``aha ui`` server) spawns the bridge
  as a managed child with ``PR_SET_PDEATHSIG`` — if the server dies (even on
  SIGKILL) the kernel tears the bridge down, so the port is never held by an
  orphan.
* **Birth**: lazy. :func:`ensure_bridge` is called when a non-terminal task that
  references the device actually uses it (opens the Hardware tab or sends).
* **Death**: when no non-terminal task references the device any more (reaped by
  the server on task-state changes), or the runtime exits.
* **Pause**: a manual escape hatch releases the port (so an operator can use their
  own minicom) without killing the bridge; resume re-acquires it.

This module owns the bridge process, its control inbox, and the device stream.
The matching :class:`~aha_cli.services.hardware_session.ArmedRuleEngine` and serial
transport are reused from ``hardware_session``.
"""

from __future__ import annotations

import codecs
import ctypes
import json
import os
import re
import select
import signal
import subprocess
import sys
import time
from pathlib import Path

from aha_cli.constants import PLAN_FILE, RUNS_DIR
from aha_cli.domain.models import normalize_task_hardware_debug, utc_now
from aha_cli.services.hardware_session import (
    ArmedRuleEngine,
    decode_escapes,
    open_uart_transport,
)
from aha_cli.store.io import append_jsonl, iter_jsonl_records_from, iter_jsonl_reverse
from aha_cli.store.paths import aha_home_path

HARDWARE_BRIDGE_CONTROL_COMMANDS = {"send", "arm", "disarm", "pause", "resume", "stop"}
_STREAM_INLINE_LIMIT = 12000
_PR_SET_PDEATHSIG = 1
_TERMINAL_TASK_STATUSES = {"completed", "failed", "blocked"}


def task_devices(task: dict) -> list[tuple[str, int]]:
    """Serial devices a task references through its UART hardware channels."""

    hardware = normalize_task_hardware_debug(task.get("hardware_debug"))
    out: list[tuple[str, int]] = []
    for channel in hardware.get("channels") or []:
        if str(channel.get("type")) != "uart":
            continue
        settings = channel.get("settings") if isinstance(channel.get("settings"), dict) else {}
        port = str(settings.get("port") or "").strip()
        if port:
            out.append((port, int(settings.get("baudrate") or 115200)))
    return out


def device_referenced_by_active_task(root: Path, device: str) -> bool:
    """True if any non-terminal, non-deleted task (any run) references ``device``.

    Conservative on error: returns True so a transient read failure never reaps a
    live bridge.
    """

    try:
        runs_dir = aha_home_path(root) / RUNS_DIR
        if not runs_dir.is_dir():
            return False
        for run_path in runs_dir.iterdir():
            plan_file = run_path / PLAN_FILE
            if not plan_file.exists():
                continue
            try:
                plan = json.loads(plan_file.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                continue
            for task in plan.get("tasks") or []:
                if task.get("deleted_at") or str(task.get("status")) in _TERMINAL_TASK_STATUSES:
                    continue
                if any(dev == device for dev, _ in task_devices(task)):
                    return True
        return False
    except Exception:
        return True


def device_key(device: str) -> str:
    """Filesystem-safe key for a device path (``/dev/ttyUSB0`` -> ``dev-ttyUSB0``)."""

    text = str(device or "").strip()
    text = re.sub(r"^/+", "", text)
    return re.sub(r"[^A-Za-z0-9_.-]+", "-", text) or "device"


def hardware_devices_dir(root: Path) -> Path:
    return aha_home_path(root) / "hardware" / "devices"


def device_bridge_dir(root: Path, device: str) -> Path:
    return hardware_devices_dir(root) / device_key(device)


def device_stream_path(root: Path, device: str) -> Path:
    return device_bridge_dir(root, device) / "stream.jsonl"


def device_bridge_state_path(root: Path, device: str) -> Path:
    return device_bridge_dir(root, device) / "bridge.json"


def device_control_path(root: Path, device: str) -> Path:
    return device_bridge_dir(root, device) / "control.jsonl"


def device_lock_path(root: Path, device: str) -> Path:
    return device_bridge_dir(root, device) / "bridge.lock"


def _inline(value: object) -> tuple[str, bool]:
    data = str(value if value is not None else "")
    if len(data) <= _STREAM_INLINE_LIMIT:
        return data, False
    return data[:_STREAM_INLINE_LIMIT], True


def pid_alive(pid: object) -> bool:
    try:
        pid_int = int(pid)
    except (TypeError, ValueError):
        return False
    if pid_int <= 0:
        return False
    try:
        os.kill(pid_int, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def read_bridge_state(root: Path, device: str) -> dict | None:
    path = device_bridge_state_path(root, device)
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def bridge_status(root: Path, device: str) -> dict:
    """Liveness-checked view of a device bridge for callers (web / CLI)."""

    state = read_bridge_state(root, device)
    if not state or not pid_alive(state.get("pid")):
        return {"device": device, "status": "stopped", "alive": False, "paused": False}
    status = str(state.get("status") or "running")
    return {
        "device": device,
        "status": status,
        "alive": True,
        "paused": status == "paused",
        "pid": state.get("pid"),
        "baudrate": state.get("baudrate"),
        "rules": state.get("rules") or [],
    }


def append_bridge_control(root: Path, device: str, command: dict) -> dict:
    cmd = str(command.get("cmd") or "").strip().lower()
    if cmd not in HARDWARE_BRIDGE_CONTROL_COMMANDS:
        raise ValueError(f"Unknown hardware bridge command: {cmd or '(empty)'}")
    record = {**command, "cmd": cmd, "ts": str(command.get("ts") or utc_now())}
    append_jsonl(device_control_path(root, device), record)
    return record


def _set_pdeathsig() -> None:
    # Run in the child between fork and exec: ask the kernel to SIGTERM us when the
    # spawning runtime dies, so the port is never held by an orphaned bridge.
    try:
        libc = ctypes.CDLL("libc.so.6", use_errno=True)
        libc.prctl(_PR_SET_PDEATHSIG, signal.SIGTERM)
    except Exception:
        pass


def bridge_launcher() -> list[str]:
    """How to launch an ``aha`` subcommand using the *current* runtime's code.

    Deliberately not a ``PATH`` lookup: the installed ``aha`` may be a frozen build
    that predates the bridge. Using ``sys.executable -m aha_cli`` guarantees the
    spawned bridge runs the same code as the runtime spawning it.
    """

    return [sys.executable, "-m", "aha_cli"]


def ensure_bridge(root: Path, device: str, baudrate: int = 115200, *, launcher: list[str] | None = None) -> dict:
    """Idempotently make sure a live bridge owns ``device``; spawn one if absent.

    Concurrency-safe via a per-device file lock. Returns the (liveness-checked)
    bridge status. The spawned child is a managed runtime child (PDEATHSIG), not a
    detached daemon.
    """

    import fcntl

    bridge_dir = device_bridge_dir(root, device)
    bridge_dir.mkdir(parents=True, exist_ok=True)
    lock_fd = os.open(device_lock_path(root, device), os.O_CREAT | os.O_RDWR, 0o644)
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        state = read_bridge_state(root, device)
        if state and pid_alive(state.get("pid")):
            return bridge_status(root, device)
        # No live bridge: spawn one bound to this runtime process.
        cmd = [
            *(launcher or bridge_launcher()),
            "--home",
            str(aha_home_path(root)),
            "hardware-bridge",
            device,
            "--baudrate",
            str(int(baudrate)),
        ]
        # Hand the child the runtime's exact import path so `-m aha_cli` resolves to
        # the same code, even when the installed `aha` on PATH is a stale frozen build.
        child_env = dict(os.environ)
        child_env["PYTHONPATH"] = os.pathsep.join(p for p in sys.path if p) + (
            os.pathsep + child_env["PYTHONPATH"] if child_env.get("PYTHONPATH") else ""
        )
        proc = subprocess.Popen(
            cmd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            preexec_fn=_set_pdeathsig,
            start_new_session=False,
            env=child_env,
        )
        # The child writes its own authoritative bridge.json on boot; record a
        # provisional pid so concurrent callers see it immediately.
        device_bridge_state_path(root, device).write_text(
            json.dumps(
                {"device": device, "baudrate": int(baudrate), "pid": proc.pid, "status": "starting", "updated_at": utc_now()},
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        return {"device": device, "status": "starting", "alive": True, "paused": False, "pid": proc.pid}
    finally:
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
        finally:
            os.close(lock_fd)


class DeviceBridgeDaemon:
    """Hold one serial device open, stream RX, multiplex TX, run armed rules."""

    def __init__(
        self,
        root: Path,
        device: str,
        baudrate: int = 115200,
        *,
        clock=time.monotonic,
        poll_interval: float = 0.02,
        self_reap: bool = True,
        reap_interval: float = 8.0,
    ) -> None:
        self.root = root
        self.device = device
        self.baudrate = int(baudrate)
        self._clock = clock
        self._poll_interval = max(0.001, float(poll_interval))
        self.engine = ArmedRuleEngine(clock=clock)
        self.transport = None
        # Incremental decoder so a multi-byte UTF-8 char split across two reads is not
        # mangled into replacement chars; the trailing partial bytes carry to next chunk.
        self._rx_decoder = codecs.getincrementaldecoder("utf-8")("replace")
        self._control_offset = 0
        self._running = True
        self._paused = False
        self._self_reap = bool(self_reap)
        self._reap_interval = max(1.0, float(reap_interval))
        self._last_reap_check = 0.0

    def _maybe_self_reap(self) -> None:
        # Death condition: no non-terminal task references this device any more.
        if not self._self_reap:
            return
        now = self._clock()
        if now - self._last_reap_check < self._reap_interval:
            return
        self._last_reap_check = now
        if not device_referenced_by_active_task(self.root, self.device):
            self._log("system", "no active task references device; reaping bridge", source="bridge")
            self._running = False

    # -- stream + state -------------------------------------------------
    def _log(self, direction: str, data: str, *, source: str = "", encoding: str = "text") -> None:
        inline, truncated = _inline(data)
        append_jsonl(
            device_stream_path(self.root, self.device),
            {
                "ts": utc_now(),
                "device": self.device,
                "direction": direction,
                "encoding": encoding,
                "data": inline,
                "truncated": truncated,
                "source": source,
            },
        )

    def _write_state(self, status: str) -> None:
        path = device_bridge_state_path(self.root, self.device)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {
                    "device": self.device,
                    "baudrate": self.baudrate,
                    "pid": os.getpid(),
                    "status": status,
                    "paused": self._paused,
                    "updated_at": utc_now(),
                    "rules": self.engine.snapshot(),
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )

    # -- port -----------------------------------------------------------
    def _open_port(self) -> bool:
        try:
            self.transport = open_uart_transport(self.device, self.baudrate)
            return True
        except OSError as exc:
            self._log("system", f"failed to open {self.device}: {exc}", source="bridge")
            return False

    def _close_port(self) -> None:
        if self.transport is not None:
            self.transport.close()
            self.transport = None

    # -- TX + rules -----------------------------------------------------
    def _send(self, data_text: str, *, source: str) -> None:
        if not data_text or self.transport is None:
            return
        self.transport.write(data_text.encode("utf-8", "replace"))
        self._log("tx", data_text, source=source)

    def _fire(self, fired: list[dict]) -> None:
        for rule in fired:
            self._send(rule["send"], source=f"rule:{rule['id']}")
            self._log("system", f"rule {rule['id']} fired (fires={rule['fires']})", source=f"rule:{rule['id']}")

    def _note_expired(self, expired: list[tuple[dict, str]]) -> None:
        for rule, reason in expired:
            self._log("system", f"rule {rule['id']} disarmed ({reason})", source=f"rule:{rule['id']}")

    # -- control --------------------------------------------------------
    def _apply_control(self) -> None:
        path = device_control_path(self.root, self.device)
        records, self._control_offset = iter_jsonl_records_from(path, self._control_offset, limit=200)
        for record, _line_end in records:
            cmd = str(record.get("cmd") or "").strip().lower()
            if cmd == "stop":
                self._log("system", "stop requested", source="control")
                self._running = False
            elif cmd == "pause":
                if not self._paused:
                    self._paused = True
                    self._close_port()
                    self._log("system", "bridge paused (port released)", source="control")
                    self._write_state("paused")
            elif cmd == "resume":
                if self._paused:
                    self._paused = False
                    self._log("system", "bridge resume requested", source="control")
            elif self._paused:
                # While paused we accept no TX/rule changes against a released port.
                self._log("system", f"ignored {cmd} while paused", source="control")
            elif cmd == "send":
                self._send(decode_escapes(record.get("data", record.get("send", ""))), source=str(record.get("source") or "interactive"))
            elif cmd == "arm":
                try:
                    rule = self.engine.arm(record)
                except re.error as exc:
                    self._log("system", f"arm rejected: invalid regex ({exc})", source="control")
                    continue
                self._log("system", f"rule {rule['id']} armed (trigger={rule['trigger']}, send={rule['send_display']!r})", source=f"rule:{rule['id']}")
                self._write_state("running")
            elif cmd == "disarm":
                rule_id = str(record.get("id") or record.get("rule") or "").strip()
                removed = self.engine.disarm(rule_id)
                self._log("system", f"rule {rule_id} disarmed (manual)" if removed else f"disarm: no rule {rule_id}", source="control")
                self._write_state("running")

    # -- main loop ------------------------------------------------------
    def run(self) -> None:
        # Skip any control backlog so a fresh bridge never replays stale commands.
        path = device_control_path(self.root, self.device)
        self._control_offset = path.stat().st_size if path.exists() else 0
        if not self._open_port():
            self._write_state("stopped")
            return
        self._log("system", f"bridge started ({self.device}@{self.baudrate})", source="bridge")
        self._write_state("running")
        try:
            self._last_reap_check = self._clock()
            while self._running:
                self._apply_control()
                if not self._running:
                    break
                self._maybe_self_reap()
                if not self._running:
                    break
                if self._paused:
                    time.sleep(self._poll_interval)
                    continue
                if self.transport is None:
                    if not self._open_port():
                        time.sleep(0.5)
                        continue
                    self._log("system", "bridge resumed (port reacquired)", source="bridge")
                    self._write_state("running")
                fired, expired = self.engine.on_tick()
                self._fire(fired)
                self._note_expired(expired)
                if fired or expired:
                    self._write_state("running")
                try:
                    readable, _, _ = select.select([self.transport.fileno()], [], [], self._poll_interval)
                except (OSError, ValueError):
                    break
                if not readable:
                    continue
                chunk = self.transport.read(4096)
                if chunk is None:
                    continue
                if chunk == b"":
                    self._log("system", "transport closed (EOF)", source="bridge")
                    break
                text = self._rx_decoder.decode(chunk)
                if not text:
                    continue
                self._log("rx", text)
                fired, expired = self.engine.on_text(text)
                self._fire(fired)
                self._note_expired(expired)
                if fired or expired:
                    self._write_state("running")
        finally:
            self._log("system", "bridge stopped", source="bridge")
            self._paused = False
            self._write_state("stopped")
            self._close_port()


def device_stream_page(root: Path, device: str, *, after: int | None = None, limit: int = 1000) -> dict:
    """Read the device stream for the web Hardware tab.

    Two modes:
    * ``after`` given -> incremental: the records *after* that byte offset (live tail follow).
    * ``after`` is None -> initial load: the **last** ``limit`` records (chronological). This
      must read from the END, not the start; otherwise a console with more than ``limit``
      records would freeze on the oldest page and never show new output.
    """

    path = device_stream_path(root, device)
    file_size = path.stat().st_size if path.exists() else 0
    safe_limit = max(1, min(int(limit or 1000), 4000))
    if after is not None:
        start = max(0, int(after))
        records, next_offset = iter_jsonl_records_from(path, start, limit=safe_limit)
        return {
            "device": device,
            "events": [{**record, "offset": line_end} for record, line_end in records],
            "after_offset": next_offset,
            "has_more": next_offset < file_size,
            "limit": safe_limit,
        }
    tail: list[tuple[dict, int]] = []
    for line_start, record in iter_jsonl_reverse(path):
        tail.append((record, line_start))
        if len(tail) >= safe_limit:
            break
    tail.reverse()
    return {
        "device": device,
        "events": [{**record, "offset": line_start} for record, line_start in tail],
        "after_offset": file_size,
        "has_more": False,
        "limit": safe_limit,
    }


__all__ = [
    "DeviceBridgeDaemon",
    "HARDWARE_BRIDGE_CONTROL_COMMANDS",
    "append_bridge_control",
    "bridge_status",
    "device_bridge_dir",
    "device_control_path",
    "device_key",
    "device_stream_page",
    "device_stream_path",
    "ensure_bridge",
    "pid_alive",
    "read_bridge_state",
]
