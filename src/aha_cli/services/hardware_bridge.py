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
  references the device actually uses it (opens the Terminal tab or sends).
* **Death**: when no non-terminal task references the device any more (reaped by
  the server on task-state changes), or the runtime exits.
* **Pause**: a manual escape hatch releases the port (so an operator can use their
  own minicom) without killing the bridge; resume re-acquires it.

This module owns the bridge process, its control inbox, and the device stream.
The matching :class:`~aha_cli.services.hardware_session.ArmedRuleEngine` and serial
transport are reused from ``hardware_session``.
"""

from __future__ import annotations

import base64
import binascii
import codecs
from collections import deque
import ctypes
import json
import os
import re
import select
import signal
import subprocess
import sys
import time
import uuid
from pathlib import Path

from aha_cli import platform, process_control
from aha_cli.constants import AHA_WEB_INSTANCE_ENV, PLAN_FILE, RUNS_DIR
from aha_cli.services.onebin import aha_cli_invocation
from aha_cli.domain.models import normalize_task_hardware_debug, utc_now
from aha_cli.services.hardware_session import (
    ArmedRuleEngine,
    decode_escapes,
    open_uart_transport,
)
from aha_cli.services.serial_lock import SerialDeviceBusyError, process_alive
from aha_cli.services.terminal_ipc import (
    BridgeTerminalIpc,
    read_control_records,
    rotate_control_file,
    stamp_control_generation,
    state_has_fresh_heartbeat,
)
from aha_cli.store.io import append_jsonl, iter_jsonl_records_from, iter_jsonl_reverse
from aha_cli.store.paths import aha_home_path

HARDWARE_BRIDGE_CONTROL_COMMANDS = {
    "send",
    "send_raw",
    "transfer_begin",
    "transfer_send",
    "transfer_send_bytes",
    "transfer_end",
    "arm",
    "disarm",
    "pause",
    "resume",
    "stop",
}
_STREAM_INLINE_LIMIT = 12000
_MAX_SERIAL_TX_PENDING_BYTES = 256 * 1024
# Many bootloader and small-board UART receivers lose characters when a paste or
# key-repeat is handed to the tty driver as one host-side burst.  Keep the first
# byte immediate, then pace the remaining bytes without blocking the bridge loop.
_SERIAL_TX_BYTE_INTERVAL = 0.001
_SERIAL_TRANSFER_TEXT_CHUNK_BYTES = 16
_PR_SET_PDEATHSIG = 1
_TERMINAL_TASK_STATUSES = {"completed", "failed", "blocked"}
_BRIDGE_HEARTBEAT_INTERVAL = 2.0
_CONTROL_OFFSET_PERSIST_INTERVAL = 1.0


def task_devices(task: dict) -> list[tuple[str, int]]:
    """Serial devices a task references through its UART hardware channels."""

    hardware = normalize_task_hardware_debug(task.get("hardware_debug"))
    if hardware.get("mode") not in {"serial", "both"}:
        return []
    serial = hardware.get("serial") if isinstance(hardware.get("serial"), dict) else {}
    device = str(serial.get("device") or "").strip()
    return [(device, int(serial.get("baudrate") or 115200))] if device else []


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


def device_control_offset_path(root: Path, device: str) -> Path:
    return device_bridge_dir(root, device) / "control.jsonl.offset"


def device_lock_path(root: Path, device: str) -> Path:
    return device_bridge_dir(root, device) / "bridge.lock"


def device_terminal_socket_path(root: Path, device: str) -> Path:
    return device_bridge_dir(root, device) / "terminal.sock"


def device_transfer_lock_path(root: Path, device: str) -> Path:
    return device_bridge_dir(root, device) / "transfer.lock"


def _inline(value: object) -> tuple[str, bool]:
    data = str(value if value is not None else "")
    if len(data) <= _STREAM_INLINE_LIMIT:
        return data, False
    return data[:_STREAM_INLINE_LIMIT], True


def pid_alive(pid: object) -> bool:
    return process_alive(pid)


def read_bridge_state(root: Path, device: str) -> dict | None:
    path = device_bridge_state_path(root, device)
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def bridge_alive(root: Path, device: str) -> bool:
    """Liveness for a serial bridge that works across WSL/Windows.

    A bridge is alive while its PID resolves *or* its state carries a heartbeat
    fresh within the TTL (a Windows-owned bridge observed from WSL has an
    unresolvable PID, so the heartbeat is the authoritative signal). A bridge
    whose state is explicitly ``stopped`` is never alive, even with a fresh
    heartbeat: the stop path writes a heartbeat in its final state write, and
    without this guard a dead bridge would be mistaken for live and `attach`
    would never spawn a replacement.
    """
    state = read_bridge_state(root, device)
    if not state:
        return False
    if str(state.get("status") or "").strip().lower() == "stopped":
        return False
    if pid_alive(state.get("pid")):
        return True
    return state_has_fresh_heartbeat(state)


def bridge_status(root: Path, device: str) -> dict:
    """Liveness-checked view of a device bridge for callers (web / CLI)."""

    state = read_bridge_state(root, device)
    if not state or not bridge_alive(root, device):
        result = {"device": device, "status": "stopped", "alive": False, "paused": False}
        if state and state.get("error"):
            result["error"] = state["error"]
            result["device_owner"] = state.get("device_owner")
        return result
    status = str(state.get("status") or "running")
    result = {
        "device": device,
        "status": status,
        "alive": True,
        "paused": status == "paused",
        "pid": state.get("pid"),
        "baudrate": state.get("baudrate"),
        "rules": state.get("rules") or [],
        "capabilities": state.get("capabilities") or [],
    }
    if isinstance(state.get("transfer"), dict):
        result["transfer"] = state["transfer"]
    if state.get("error"):
        result["error"] = state["error"]
        result["device_owner"] = state.get("device_owner")
    return result


def append_bridge_control(root: Path, device: str, command: dict) -> dict:
    cmd = str(command.get("cmd") or "").strip().lower()
    if cmd not in HARDWARE_BRIDGE_CONTROL_COMMANDS:
        raise ValueError(f"Unknown hardware bridge command: {cmd or '(empty)'}")
    path = device_control_path(root, device)
    path.parent.mkdir(parents=True, exist_ok=True)
    # Rotate a bloated inbox so a long-lived bridge never accumulates megabytes of
    # control history; consumers keep a persisted offset that rotation clears.
    rotate_control_file(path)
    record = {**command, "cmd": cmd, "ts": str(command.get("ts") or utc_now())}
    # A `stop` is aimed at the bridge generation currently recorded: a freshly
    # spawned bridge ignores stops stamped for an earlier generation, closing the
    # hardware-stop -> hardware-attach race that killed the new bridge.
    if cmd == "stop":
        record = stamp_control_generation(record, read_bridge_state(root, device))
    append_jsonl(path, record)
    return record


def stop_all_hardware_bridges(root: Path, *, timeout: float = 3.0) -> dict:
    """Stop every Serial bridge recorded under this AHA home.

    Parent-death binding is the normal lifecycle mechanism, but a bridge from an
    older Web process may not belong to the current Windows Job Object. Web
    shutdown therefore also performs an explicit, bounded sweep so an upgrade
    never reuses a live bridge running stale code.
    """

    states: list[tuple[Path, dict, int]] = []
    owner_instance = str(os.environ.get(AHA_WEB_INSTANCE_ENV) or "")
    devices_dir = hardware_devices_dir(root)
    if devices_dir.is_dir():
        for state_path in devices_dir.glob("*/bridge.json"):
            try:
                state = json.loads(state_path.read_text(encoding="utf-8"))
                pid = int(state.get("pid") or 0)
            except (OSError, ValueError, TypeError, json.JSONDecodeError):
                continue
            device = str(state.get("device") or "").strip()
            if not device or pid <= 0:
                continue
            states.append((state_path, state, pid))
            if pid_alive(pid):
                stop_record = stamp_control_generation({"cmd": "stop", "ts": utc_now()}, state)
                append_jsonl(state_path.parent / "control.jsonl", stop_record)

    deadline = time.monotonic() + max(0.0, float(timeout))
    while any(pid_alive(pid) for _path, _state, pid in states) and time.monotonic() < deadline:
        time.sleep(0.02)

    forced = 0
    for _state_path, state, pid in states:
        if not pid_alive(pid):
            continue
        if not owner_instance or str(state.get("owner_instance") or "") != owner_instance:
            continue
        forced += 1
        try:
            if not process_control.terminate_process(pid, timeout=1.0):
                process_control.send_signal(pid, signal.SIGKILL)
        except (OSError, ProcessLookupError, PermissionError):
            pass

    force_deadline = time.monotonic() + 1.0
    while any(pid_alive(pid) for _path, _state, pid in states) and time.monotonic() < force_deadline:
        time.sleep(0.02)

    stopped = 0
    remaining: list[int] = []
    for state_path, state, pid in states:
        if pid_alive(pid):
            remaining.append(pid)
            continue
        stopped += 1
        state.pop("transfer", None)
        state.update(
            {
                "status": "stopped",
                "paused": False,
                "updated_at": utc_now(),
                "stop_reason": "web-shutdown",
            }
        )
        try:
            state_path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
            (state_path.parent / "terminal.sock").unlink(missing_ok=True)
        except OSError:
            pass
    return {"found": len(states), "stopped": stopped, "forced": forced, "remaining": remaining}


def set_parent_death_signal() -> None:
    # Backward-compatible alias; the canonical cross-platform helper is
    # ``aha_cli.process_control.parent_death_preexec``.
    process_control._posix_parent_death_signal()


def bridge_launcher() -> list[str]:
    """How to launch an ``aha`` subcommand using the authoritative runtime.

    Onebin-aware: runs the Windows-owned onebin forwarded into a WSL backend when
    present, otherwise the running zipapp or ``python -m aha_cli`` for source
    checkouts. Deliberately not a ``PATH`` lookup so a WSL-side AHA shim cannot
    select stale bridge code.
    """

    return aha_cli_invocation()


def ensure_bridge(
    root: Path,
    device: str,
    baudrate: int = 115200,
    *,
    launcher: list[str] | None = None,
    detach: bool = False,
) -> dict:
    """Idempotently make sure a live bridge owns ``device``; spawn one if absent.

    Concurrency-safe via a per-device file lock. Returns the (liveness-checked)
    bridge status.

    ``detach=False`` (the web-server path): the child is a managed runtime child
    (PDEATHSIG) so the long-lived server owns its lifecycle. ``detach=True`` (the
    transient CLI ``hardware-attach`` path): the child is NOT parent-death bound,
    so interrupting the agent does not tear the bridge down; it lives until no
    active task references the device (self-reap) or it is explicitly stopped.
    """

    from aha_cli import locking

    bridge_dir = device_bridge_dir(root, device)
    bridge_dir.mkdir(parents=True, exist_ok=True)
    lock_fd = os.open(device_lock_path(root, device), os.O_CREAT | os.O_RDWR, 0o644)
    try:
        locking.acquire(lock_fd)
        state = read_bridge_state(root, device)
        if state and bridge_alive(root, device):
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
        bridge_log = bridge_dir / "bridge.log"
        bridge_log.parent.mkdir(parents=True, exist_ok=True)
        log_handle = bridge_log.open("ab")
        try:
            proc = subprocess.Popen(
                cmd,
                stdin=subprocess.DEVNULL,
                stdout=log_handle,
                stderr=subprocess.STDOUT,
                preexec_fn=None if detach else process_control.parent_death_preexec(),
                start_new_session=False,
                env=child_env,
                **platform.hidden_subprocess_kwargs(),
            )
        finally:
            log_handle.close()
        if not detach:
            process_control.assign_parent_death(proc)
        previous_generation = int((state or {}).get("generation") or 0)
        now = time.time()
        # The child writes its own authoritative bridge.json on boot; record a
        # provisional pid so concurrent callers see it immediately. The generation
        # is bumped per spawn so stale stop records never apply to this instance.
        device_bridge_state_path(root, device).write_text(
            json.dumps(
                {
                    "device": device,
                    "baudrate": int(baudrate),
                    "pid": proc.pid,
                    "status": "starting",
                    "owner_pid": os.getpid(),
                    "owner_instance": str(child_env.get(AHA_WEB_INSTANCE_ENV) or ""),
                    "spawn_source": "cli" if detach else "web",
                    "generation": previous_generation + 1,
                    "instance_uuid": str(uuid.uuid4()),
                    "heartbeat_at": now,
                    "started_at": now,
                    "updated_at": utc_now(),
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        return {"device": device, "status": "starting", "alive": True, "paused": False, "pid": proc.pid}
    finally:
        try:
            locking.release(lock_fd)
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
        tx_byte_interval: float = _SERIAL_TX_BYTE_INTERVAL,
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
        self._tx_queue: deque[dict[str, object]] = deque()
        self._tx_pending_bytes = 0
        self._tx_byte_interval = max(0.0, float(tx_byte_interval))
        self._tx_next_write_at = 0.0
        self._transfer: dict[str, str] | None = None
        self._open_error = ""
        self._device_owner: dict | None = None
        self._terminal_ipc = BridgeTerminalIpc(
            device_terminal_socket_path(root, device),
            device_stream_path(root, device),
        )
        # Instance identity + lifecycle fields: the UUID and heartbeat let callers on
        # a different OS (WSL vs Windows) decide liveness without trusting the PID,
        # and the generation makes `stop` records apply only to their target bridge.
        self._instance_uuid = str(uuid.uuid4())
        self._generation = 1
        self._started_at = time.time()
        self._last_heartbeat_at = 0.0
        self._last_offset_persist_at = 0.0

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
    def _log(self, direction: str, data: str, *, source: str = "", encoding: str = "text") -> int:
        inline, truncated = _inline(data)
        offset = append_jsonl(
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
        if direction == "rx":
            self._terminal_ipc.broadcast("output", data=inline, offset=offset)
        return offset

    def _write_state(self, status: str) -> None:
        path = device_bridge_state_path(self.root, self.device)
        path.parent.mkdir(parents=True, exist_ok=True)
        state = {
            "device": self.device,
            "baudrate": self.baudrate,
            "pid": os.getpid(),
            "status": status,
            "owner_pid": os.getppid(),
            "owner_instance": str(os.environ.get(AHA_WEB_INSTANCE_ENV) or ""),
            "paused": self._paused,
            "updated_at": utc_now(),
            "rules": self.engine.snapshot(),
            "capabilities": ["serial-transfer-v1", "serial-transfer-v2"],
            "instance_uuid": self._instance_uuid,
            "generation": self._generation,
            "started_at": self._started_at,
            "heartbeat_at": time.time(),
        }
        if self._transfer is not None:
            state["transfer"] = dict(self._transfer)
        if self._open_error:
            state["error"] = self._open_error
            state["device_owner"] = self._device_owner
        path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
        self._terminal_ipc.broadcast(
            "status",
            bridge={**state, "alive": status != "stopped"},
        )

    def _maybe_heartbeat(self) -> None:
        """Throttled freshness write so cross-OS liveness checks stay accurate.

        A Windows-owned bridge observed from WSL has an unresolvable PID; the
        heartbeat timestamp is the authoritative "still alive" signal. Writes are
        bounded to once per interval and do not touch the port.
        """
        now = time.time()
        if now - self._last_heartbeat_at < _BRIDGE_HEARTBEAT_INTERVAL:
            return
        self._last_heartbeat_at = now
        path = device_bridge_state_path(self.root, self.device)
        try:
            state = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        except (json.JSONDecodeError, OSError):
            state = {}
        state.update(
            {
                "pid": os.getpid(),
                "status": "paused" if self._paused else "running" if self.transport is not None else str(state.get("status") or "connecting"),
                "instance_uuid": self._instance_uuid,
                "generation": self._generation,
                "heartbeat_at": now,
                "updated_at": utc_now(),
            }
        )
        try:
            path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
        except OSError:
            pass

    def _persist_control_offset(self) -> None:
        now = time.time()
        if now - self._last_offset_persist_at < _CONTROL_OFFSET_PERSIST_INTERVAL:
            return
        self._last_offset_persist_at = now
        try:
            device_control_offset_path(self.root, self.device).write_text(str(self._control_offset), encoding="utf-8")
        except OSError:
            pass

    # -- port -----------------------------------------------------------
    def _open_port(self) -> bool:
        try:
            self.transport = open_uart_transport(self.device, self.baudrate)
            self._open_error = ""
            self._device_owner = None
            return True
        except SerialDeviceBusyError as exc:
            message = str(exc)
            if message != self._open_error:
                self._log("system", message, source="bridge")
            self._open_error = message
            self._device_owner = exc.owner
            return False
        except OSError as exc:
            message = f"failed to open {self.device}: {exc}"
            if message != self._open_error:
                self._log("system", message, source="bridge")
            self._open_error = message
            self._device_owner = None
            return False

    def _close_port(self) -> None:
        if self.transport is not None:
            self.transport.close()
            self.transport = None

    def _discard_tx(self, reason: str) -> None:
        if not self._tx_pending_bytes:
            return
        dropped = self._tx_pending_bytes
        self._tx_queue.clear()
        self._tx_pending_bytes = 0
        self._log("system", f"discarded {dropped} pending TX bytes ({reason})", source="bridge")

    # -- TX + rules -----------------------------------------------------
    def _send(
        self,
        data_text: str,
        *,
        source: str,
        byte_interval: float | None = None,
        chunk_size: int = 1,
        audit_text: str | None = None,
    ) -> None:
        if not data_text:
            return
        self._send_bytes(
            data_text.encode("utf-8", "replace"),
            source=source,
            byte_interval=byte_interval,
            chunk_size=chunk_size,
            audit_text=data_text if audit_text is None else audit_text,
        )

    def _send_bytes(
        self,
        payload: bytes,
        *,
        source: str,
        byte_interval: float | None = None,
        chunk_size: int = 1,
        audit_text: str,
    ) -> None:
        if not payload or self.transport is None:
            return
        if self._tx_pending_bytes + len(payload) > _MAX_SERIAL_TX_PENDING_BYTES:
            self._log("system", f"TX queue full; rejected {len(payload)} bytes", source=source)
            return
        self._tx_queue.append({
            "data": payload,
            "offset": 0,
            "text": audit_text,
            "source": source,
            "byte_interval": self._tx_byte_interval if byte_interval is None else max(0.0, float(byte_interval)),
            "chunk_size": max(1, int(chunk_size)),
        })
        self._tx_pending_bytes += len(payload)
        self._flush_tx()

    def _flush_tx(self) -> None:
        while self.transport is not None and self._tx_queue:
            pending = self._tx_queue[0]
            byte_interval = float(pending.get("byte_interval") or 0.0)
            if byte_interval and self._clock() < self._tx_next_write_at:
                return
            payload = pending["data"]
            offset = int(pending["offset"])
            chunk_size = int(pending.get("chunk_size") or 1)
            chunk_end = min(len(payload), offset + chunk_size) if byte_interval else len(payload)
            try:
                written = int(self.transport.write(payload[offset:chunk_end]) or 0)
            except (OSError, ValueError, TypeError):
                written = 0
            if written <= 0:
                return
            written = min(written, chunk_end - offset)
            pending["offset"] = offset + written
            self._tx_pending_bytes -= written
            if int(pending["offset"]) >= len(payload):
                self._tx_queue.popleft()
                self._log("tx", str(pending["text"]), source=str(pending["source"]))
            if byte_interval:
                self._tx_next_write_at = self._clock() + byte_interval
                return

    def _fire(self, fired: list[dict]) -> None:
        for rule in fired:
            if self._transfer is not None:
                self._log("system", f"rule {rule['id']} suppressed during file transfer", source=f"rule:{rule['id']}")
                continue
            self._send(rule["send"], source=f"rule:{rule['id']}")
            self._log("system", f"rule {rule['id']} fired (fires={rule['fires']})", source=f"rule:{rule['id']}")

    def _note_expired(self, expired: list[tuple[dict, str]]) -> None:
        for rule, reason in expired:
            self._log("system", f"rule {rule['id']} disarmed ({reason})", source=f"rule:{rule['id']}")

    def _rule_unchanged(self, previous: dict | None, rule: dict) -> bool:
        if previous is None:
            return False
        return (
            previous["trigger"] == rule["trigger"]
            and previous["pattern"] == rule["pattern"]
            and previous["regex"] == rule["regex"]
            and previous["send"] == rule["send"]
            and previous["max_fires"] == rule["max_fires"]
            and previous["delay_seconds"] == rule["delay_seconds"]
            and previous["interval_seconds"] == rule["interval_seconds"]
            and previous["duration_seconds"] == rule["duration_seconds"]
        )

    # -- control --------------------------------------------------------
    def _apply_control(self) -> None:
        path = device_control_path(self.root, self.device)
        records, self._control_offset = read_control_records(path, self._control_offset, limit=200)
        for record, _line_end in records:
            cmd = str(record.get("cmd") or "").strip().lower()
            if cmd == "stop":
                # Ignore a stop aimed at an earlier bridge generation: this closes the
                # hardware-stop -> hardware-attach race where a fresh bridge consumed
                # the previous instance's stop and exited immediately.
                record_generation = int(record.get("generation") or 0)
                if record_generation > 0 and record_generation != self._generation:
                    self._log("system", f"ignored stale stop for generation {record_generation}", source="control")
                    continue
                self._log("system", "stop requested", source="control")
                self._transfer = None
                self._running = False
            elif cmd == "pause":
                if not self._paused:
                    self._paused = True
                    self._transfer = None
                    self._discard_tx("bridge paused")
                    self._close_port()
                    self._open_error = ""
                    self._device_owner = None
                    self._log("system", "bridge paused (port released)", source="control")
                    self._write_state("paused")
            elif cmd == "resume":
                if self._paused:
                    self._paused = False
                    self._log("system", "bridge resume requested", source="control")
                    self._write_state("connecting")
            elif self._paused:
                # While paused we accept no TX/rule changes against a released port.
                self._log("system", f"ignored {cmd} while paused", source="control")
            elif cmd == "transfer_begin":
                transfer_id = str(record.get("transfer_id") or "").strip()
                if not transfer_id:
                    self._log("system", "file transfer rejected: transfer_id is required", source="file-transfer")
                else:
                    if self._transfer is not None and self._transfer.get("id") != transfer_id:
                        self._log("system", "replacing stale file transfer lease", source="file-transfer")
                    self._discard_tx("file transfer lease acquired")
                    self._transfer = {
                        "id": transfer_id,
                        "source": str(record.get("source") or "file-transfer"),
                        "started_at": str(record.get("ts") or utc_now()),
                    }
                    self._log("system", "file transfer lease acquired", source="file-transfer")
                    self._write_state("running")
            elif cmd == "transfer_send":
                transfer_id = str(record.get("transfer_id") or "").strip()
                if self._transfer is None or self._transfer.get("id") != transfer_id:
                    self._log("system", "file transfer data rejected: lease mismatch", source="file-transfer")
                    continue
                data = str(record.get("data") or "")
                wire_seconds = (10.0 * _SERIAL_TRANSFER_TEXT_CHUNK_BYTES) / max(1, self.baudrate)
                self._send(
                    data,
                    source="file-transfer",
                    byte_interval=max(0.0005, wire_seconds),
                    chunk_size=_SERIAL_TRANSFER_TEXT_CHUNK_BYTES,
                    audit_text=f"[file transfer {len(data.encode('utf-8'))} wire bytes]",
                )
            elif cmd == "transfer_send_bytes":
                transfer_id = str(record.get("transfer_id") or "").strip()
                if self._transfer is None or self._transfer.get("id") != transfer_id:
                    self._log("system", "file transfer data rejected: lease mismatch", source="file-transfer")
                    continue
                try:
                    payload = base64.b64decode(str(record.get("data") or ""), validate=True)
                except (binascii.Error, ValueError):
                    self._log("system", "file transfer bytes rejected: invalid base64", source="file-transfer")
                    continue
                # The OS UART driver already drains its TX buffer at the configured
                # baudrate.  Extra userspace pacing doubles the delay on Windows,
                # where pyserial.write() blocks until the bytes are accepted/sent.
                # Raw receiver mode is designed for a continuous binary stream, so
                # hand the whole frame to the driver and retain conservative pacing
                # only for the legacy Shell text receiver above.
                self._send_bytes(
                    payload,
                    source="file-transfer",
                    byte_interval=0.0,
                    chunk_size=len(payload),
                    audit_text=f"[file transfer {len(payload)} binary bytes]",
                )
            elif cmd == "transfer_end":
                transfer_id = str(record.get("transfer_id") or "").strip()
                if self._transfer is not None and self._transfer.get("id") == transfer_id:
                    self._transfer = None
                    self._log("system", "file transfer lease released", source="file-transfer")
                    self._write_state("running")
            elif cmd in {"send", "send_raw"}:
                if self._transfer is not None:
                    self._log("system", f"ignored {cmd} during file transfer", source="control")
                    continue
                raw = record.get("data", record.get("send", ""))
                data = str(raw or "") if cmd == "send_raw" else decode_escapes(raw)
                self._send(data, source=str(record.get("source") or "interactive"))
            elif cmd == "arm":
                rule_id = str(record.get("id") or "").strip()
                previous = next((item for item in self.engine.rules if item["id"] == rule_id), None)
                try:
                    rule = self.engine.arm(record)
                except re.error as exc:
                    self._log("system", f"arm rejected: invalid regex ({exc})", source="control")
                    continue
                # Re-arming an identical rule (e.g. auto-login on every terminal open)
                # is idempotent in the engine; only log/refresh state when it changed,
                # so the bridge log is not flooded with duplicate "armed" entries.
                if self._rule_unchanged(previous, rule):
                    continue
                self._log("system", f"rule {rule['id']} armed (trigger={rule['trigger']}, send={rule['send_display']!r})", source=f"rule:{rule['id']}")
                self._write_state("running")
            elif cmd == "disarm":
                rule_id = str(record.get("id") or record.get("rule") or "").strip()
                removed = self.engine.disarm(rule_id)
                self._log("system", f"rule {rule_id} disarmed (manual)" if removed else f"disarm: no rule {rule_id}", source="control")
                self._write_state("running")

    def _apply_ipc_commands(self, commands: list[dict]) -> None:
        for command in commands:
            command_type = str(command.get("type") or "").strip().lower()
            if command_type == "input" and not self._paused:
                if self._transfer is not None:
                    continue
                self._send(str(command.get("data") or "")[:65536], source="web-xterm")

    # -- main loop ------------------------------------------------------
    def _archive_previous_stream(self) -> None:
        """Move the existing device stream aside before a new bridge session.

        A serial device is a machine-level resource shared across tasks, and the
        same physical port (e.g. COM6) may be reconnected to a different board
        between sessions (Luckfox -> MI_CW500_Dual). Keeping the accumulated
        stream would mix old and new board output in the Web Terminal replay.
        Rename the existing stream.jsonl to an archive file so each bridge
        session starts clean while the history stays available for forensics.
        """
        stream_path = device_stream_path(self.root, self.device)
        if not stream_path.exists() or stream_path.stat().st_size == 0:
            return
        archive_dir = device_bridge_dir(self.root, self.device) / "archive"
        archive_dir.mkdir(parents=True, exist_ok=True)
        stamp = utc_now().replace("+00:00", "Z").replace(":", "-").replace("T", "_")
        target = archive_dir / f"stream.{stamp}.jsonl"
        try:
            stream_path.rename(target)
            self._log("system", f"archived previous stream to {target.name}", source="bridge")
        except OSError as exc:
            # Renaming must not break bridge startup; fall back to truncating so
            # the new session still starts clean.
            self._log("system", f"stream archive failed ({exc}); truncating", source="bridge")
            try:
                stream_path.write_text("", encoding="utf-8")
            except OSError:
                pass

    def run(self) -> None:
        # Adopt the spawner's generation and UUID so this instance only consumes
        # control commands aimed at it (and stale stops for older instances are
        # ignored). Persisted control offsets let a restarted bridge resume where
        # it left off instead of re-applying the whole backlog.
        state = read_bridge_state(self.root, self.device)
        if isinstance(state, dict):
            self._generation = max(1, int(state.get("generation") or 1))
            self._instance_uuid = str(state.get("instance_uuid") or self._instance_uuid)
        path = device_control_path(self.root, self.device)
        offset_file = device_control_offset_path(self.root, self.device)
        try:
            persisted = int(offset_file.read_text(encoding="utf-8").strip() or "0") if offset_file.exists() else 0
        except (OSError, ValueError):
            persisted = 0
        file_size = path.stat().st_size if path.exists() else 0
        # Skip any control backlog so a fresh bridge never replays stale commands.
        # Resume a persisted offset when the live file still spans it (i.e. it was
        # not rotated out from under us since we last consumed).
        self._control_offset = persisted if 0 < persisted <= file_size else file_size
        self._archive_previous_stream()
        self._terminal_ipc.start()
        try:
            if self._open_port():
                self._log("system", f"bridge started ({self.device}@{self.baudrate})", source="bridge")
                self._write_state("running")
                retry_port_at = 0.0
            else:
                self._write_state("blocked")
                retry_port_at = self._clock() + 0.5
            self._last_reap_check = self._clock()
            while self._running:
                self._apply_control()
                if not self._running:
                    break
                self._maybe_heartbeat()
                self._persist_control_offset()
                self._maybe_self_reap()
                if not self._running:
                    break
                if self._paused:
                    pass
                elif self.transport is None and self._clock() >= retry_port_at:
                    previous_error = self._open_error
                    if not self._open_port():
                        retry_port_at = self._clock() + 0.5
                        if self._open_error != previous_error:
                            self._write_state("blocked")
                    else:
                        self._log("system", "bridge resumed (port reacquired)", source="bridge")
                        self._write_state("running")
                if not self._paused and self.transport is not None:
                    fired, expired = self.engine.on_tick()
                    self._fire(fired)
                    self._note_expired(expired)
                    if fired or expired:
                        self._write_state("running")
                transport_fd = self.transport.fileno() if self.transport is not None and not self._paused else None
                readers: list[object] = self._terminal_ipc.readables()
                if transport_fd is not None:
                    readers.append(transport_fd)
                writers: list[object] = self._terminal_ipc.writables()
                select_timeout = self._poll_interval
                if transport_fd is not None and self._tx_queue:
                    tx_wait = self._tx_next_write_at - self._clock()
                    if tx_wait <= 0:
                        writers.append(transport_fd)
                    else:
                        select_timeout = min(select_timeout, tx_wait)
                try:
                    readable, writable, _ = select.select(
                        readers,
                        writers,
                        [],
                        select_timeout,
                    )
                except (OSError, ValueError):
                    break
                self._apply_ipc_commands(self._terminal_ipc.process(readable, writable))
                if transport_fd is not None and transport_fd in writable:
                    self._flush_tx()
                if transport_fd is None or transport_fd not in readable:
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
            self._discard_tx("bridge stopped")
            self._close_port()
            self._paused = False
            self._transfer = None
            self._open_error = ""
            self._device_owner = None
            self._write_state("stopped")
            self._terminal_ipc.close()


def device_stream_page(
    root: Path,
    device: str,
    *,
    after: int | None = None,
    before: int | None = None,
    limit: int = 1000,
) -> dict:
    """Read the device stream for the web Terminal tab.

    Two modes:
    * ``after`` given -> incremental: the records *after* that byte offset (live tail follow).
    * ``after`` is None -> initial load: the **last** ``limit`` records (chronological). This
      must read from the END, not the start; otherwise a console with more than ``limit``
      records would freeze on the oldest page and never show new output.
    """

    path = device_stream_path(root, device)
    file_size = path.stat().st_size if path.exists() else 0
    end_offset = file_size if before is None else max(0, min(int(before), file_size))
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
    for line_start, record in iter_jsonl_reverse(path, before=end_offset):
        tail.append((record, line_start))
        if len(tail) >= safe_limit:
            break
    tail.reverse()
    return {
        "device": device,
        "events": [{**record, "offset": line_start} for record, line_start in tail],
        "after_offset": end_offset,
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
    "device_terminal_socket_path",
    "device_transfer_lock_path",
    "ensure_bridge",
    "pid_alive",
    "read_bridge_state",
    "set_parent_death_signal",
    "stop_all_hardware_bridges",
]
