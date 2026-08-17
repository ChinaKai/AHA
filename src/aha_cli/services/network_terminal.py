"""Machine-level Telnet terminal bridge for task network hardware debugging."""

from __future__ import annotations

import base64
import binascii
import codecs
from collections import deque
import json
import os
import re
import select
import signal
import socket
import subprocess
import sys
import time
import uuid
from pathlib import Path

from aha_cli import platform, process_control
from aha_cli.constants import AHA_WEB_INSTANCE_ENV, PLAN_FILE, RUNS_DIR
from aha_cli.domain.models import normalize_task_hardware_debug, utc_now
from aha_cli.services.hardware_bridge import pid_alive
from aha_cli.services.onebin import aha_cli_invocation
from aha_cli.services.hardware_session import ArmedRuleEngine, decode_escapes
from aha_cli.services.terminal_ipc import (
    BridgeTerminalIpc,
    read_control_records,
    rotate_control_file,
    stamp_control_generation,
    state_has_fresh_heartbeat,
)
from aha_cli.store.io import append_jsonl, iter_jsonl_reverse
from aha_cli.store.paths import aha_home_path

NETWORK_CONTROL_COMMANDS = {
    "send",
    "send_raw",
    "resize",
    "arm",
    "disarm",
    "pause",
    "resume",
    "stop",
    "transfer_begin",
    "transfer_send",
    "transfer_send_bytes",
    "transfer_end",
}
_STREAM_INLINE_LIMIT = 12000
_MAX_NETWORK_TX_PENDING_BYTES = 512 * 1024
_TERMINAL_TASK_STATUSES = {"completed", "failed", "blocked"}
_BRIDGE_HEARTBEAT_INTERVAL = 2.0
_CONTROL_OFFSET_PERSIST_INTERVAL = 1.0


def task_network_target(task: dict) -> tuple[str, int, str, str] | None:
    hardware = normalize_task_hardware_debug(task.get("hardware_debug"))
    if hardware.get("mode") not in {"network", "both"}:
        return None
    network = hardware.get("network") if isinstance(hardware.get("network"), dict) else {}
    host = str(network.get("device_ip") or "").strip()
    if not host:
        return None
    credentials = hardware.get("credentials") if isinstance(hardware.get("credentials"), dict) else {}
    return host, 23, str(credentials.get("username") or ""), str(credentials.get("password") or "")


def network_target_referenced_by_active_task(root: Path, host: str, port: int = 23) -> bool:
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
                target = task_network_target(task)
                if target and target[0] == host and target[1] == int(port):
                    return True
        return False
    except Exception:
        return True


def network_key(host: str, port: int = 23) -> str:
    safe_host = re.sub(r"[^A-Za-z0-9_.-]+", "-", str(host or "").strip()) or "host"
    return f"telnet-{safe_host}-{int(port)}"


def network_terminal_dir(root: Path, host: str, port: int = 23) -> Path:
    return aha_home_path(root) / "hardware" / "network" / network_key(host, port)


def network_stream_path(root: Path, host: str, port: int = 23) -> Path:
    return network_terminal_dir(root, host, port) / "stream.jsonl"


def network_state_path(root: Path, host: str, port: int = 23) -> Path:
    return network_terminal_dir(root, host, port) / "bridge.json"


def network_control_path(root: Path, host: str, port: int = 23) -> Path:
    return network_terminal_dir(root, host, port) / "control.jsonl"


def network_control_offset_path(root: Path, host: str, port: int = 23) -> Path:
    return network_terminal_dir(root, host, port) / "control.jsonl.offset"


def network_alive(root: Path, host: str, port: int = 23) -> bool:
    """Liveness for a Telnet bridge that works across WSL/Windows (heartbeat-based)."""
    try:
        state = json.loads(network_state_path(root, host, port).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    if str(state.get("status") or "").strip().lower() == "stopped":
        return False
    if pid_alive(state.get("pid")):
        return True
    return state_has_fresh_heartbeat(state)


def network_credentials_path(root: Path, host: str, port: int = 23) -> Path:
    return network_terminal_dir(root, host, port) / "credentials.json"


def network_terminal_socket_path(root: Path, host: str, port: int = 23) -> Path:
    return network_terminal_dir(root, host, port) / "terminal.sock"


def network_transfer_lock_path(root: Path, host: str, port: int = 23) -> Path:
    return network_terminal_dir(root, host, port) / "transfer.lock"


def _write_credentials(root: Path, host: str, port: int, username: str, password: str) -> None:
    path = network_credentials_path(root, host, port)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"username": username, "password": password}), encoding="utf-8")
    path.chmod(0o600)


def _read_credentials(root: Path, host: str, port: int) -> tuple[str, str]:
    try:
        raw = json.loads(network_credentials_path(root, host, port).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        raw = {}
    return str(raw.get("username") or ""), str(raw.get("password") or "")


def network_status(root: Path, host: str, port: int = 23) -> dict:
    try:
        state = json.loads(network_state_path(root, host, port).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        state = None
    endpoint = f"{host}:{int(port)}"
    if not state or not network_alive(root, host, port):
        return {"endpoint": endpoint, "host": host, "port": int(port), "status": "stopped", "alive": False, "paused": False}
    status = str(state.get("status") or "connecting")
    result = {
        "endpoint": endpoint,
        "host": host,
        "port": int(port),
        "status": status,
        "alive": True,
        "paused": status == "paused",
        "connected": status == "running",
        "pid": state.get("pid"),
        "rules": state.get("rules") or [],
        "capabilities": state.get("capabilities") or [],
        "telnet_binary": bool(state.get("telnet_binary")),
    }
    if isinstance(state.get("transfer"), dict):
        result["transfer"] = state["transfer"]
    return result


def stop_all_network_terminals(root: Path, *, timeout: float = 3.0) -> dict:
    """Stop every Telnet bridge recorded under this AHA home."""

    states: list[tuple[Path, dict, int]] = []
    owner_instance = str(os.environ.get(AHA_WEB_INSTANCE_ENV) or "")
    network_dir = aha_home_path(root) / "hardware" / "network"
    if network_dir.is_dir():
        for state_path in network_dir.glob("*/bridge.json"):
            try:
                state = json.loads(state_path.read_text(encoding="utf-8"))
                pid = int(state.get("pid") or 0)
                port = int(state.get("port") or 23)
            except (OSError, ValueError, TypeError, json.JSONDecodeError):
                continue
            host = str(state.get("host") or "").strip()
            if not host or pid <= 0:
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


def append_network_control(root: Path, host: str, port: int, command: dict) -> dict:
    cmd = str(command.get("cmd") or "").strip().lower()
    if cmd not in NETWORK_CONTROL_COMMANDS:
        raise ValueError(f"Unknown network terminal command: {cmd or '(empty)'}")
    path = network_control_path(root, host, port)
    path.parent.mkdir(parents=True, exist_ok=True)
    rotate_control_file(path)
    record = {**command, "cmd": cmd, "ts": str(command.get("ts") or utc_now())}
    if cmd == "stop":
        try:
            state = json.loads(network_state_path(root, host, port).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            state = None
        record = stamp_control_generation(record, state)
    append_jsonl(path, record)
    return record


def ensure_network_terminal(
    root: Path,
    host: str,
    port: int = 23,
    *,
    username: str = "",
    password: str = "",
    launcher: list[str] | None = None,
    detach: bool = False,
) -> dict:
    from aha_cli import locking

    terminal_dir = network_terminal_dir(root, host, port)
    terminal_dir.mkdir(parents=True, exist_ok=True)
    _write_credentials(root, host, int(port), username, password)
    lock_path = terminal_dir / "bridge.lock"
    lock_fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o644)
    try:
        locking.acquire(lock_fd)
        status = network_status(root, host, port)
        if status.get("alive"):
            return status
        command = [
            *(launcher or aha_cli_invocation()),
            "--home",
            str(aha_home_path(root)),
            "hardware-network-bridge",
            host,
            "--port",
            str(int(port)),
        ]
        child_env = dict(os.environ)
        child_env["PYTHONPATH"] = os.pathsep.join(item for item in sys.path if item) + (
            os.pathsep + child_env["PYTHONPATH"] if child_env.get("PYTHONPATH") else ""
        )
        bridge_log = terminal_dir / "bridge.log"
        bridge_log.parent.mkdir(parents=True, exist_ok=True)
        log_handle = bridge_log.open("ab")
        try:
            proc = subprocess.Popen(
                command,
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
        previous_generation = 0
        try:
            previous_state = json.loads(network_state_path(root, host, port).read_text(encoding="utf-8"))
            previous_generation = int(previous_state.get("generation") or 0)
        except (OSError, json.JSONDecodeError):
            pass
        now = time.time()
        network_state_path(root, host, port).write_text(
            json.dumps(
                {
                    "host": host,
                    "port": int(port),
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
        return {"endpoint": f"{host}:{int(port)}", "status": "starting", "alive": True, "paused": False, "pid": proc.pid}
    finally:
        try:
            locking.release(lock_fd)
        finally:
            os.close(lock_fd)


class TelnetCodec:
    IAC = 255
    DONT = 254
    DO = 253
    WONT = 252
    WILL = 251
    SB = 250
    SE = 240
    ECHO = 1
    SGA = 3
    TTYPE = 24
    NAWS = 31
    IS = 0
    SEND = 1
    BINARY = 0

    def __init__(self, cols: int = 100, rows: int = 28) -> None:
        self._pending = b""
        self.cols = max(20, min(int(cols), 240))
        self.rows = max(8, min(int(rows), 80))
        self.local_binary = False
        self.remote_binary = False

    @property
    def binary_ready(self) -> bool:
        return self.local_binary and self.remote_binary

    def initial_negotiation(self) -> bytes:
        return bytes((self.IAC, self.WILL, self.BINARY, self.IAC, self.DO, self.BINARY))

    @classmethod
    def _escape_iac(cls, data: bytes) -> bytes:
        return data.replace(bytes((cls.IAC,)), bytes((cls.IAC, cls.IAC)))

    def window_size(self) -> bytes:
        payload = self.cols.to_bytes(2, "big") + self.rows.to_bytes(2, "big")
        return bytes((self.IAC, self.SB, self.NAWS)) + self._escape_iac(payload) + bytes((self.IAC, self.SE))

    def resize(self, cols: object, rows: object) -> bytes:
        try:
            self.cols = max(20, min(int(cols), 240))
            self.rows = max(8, min(int(rows), 80))
        except (TypeError, ValueError):
            return b""
        return self.window_size()

    def feed(self, chunk: bytes) -> tuple[bytes, bytes]:
        data = self._pending + chunk
        self._pending = b""
        output = bytearray()
        reply = bytearray()
        index = 0
        while index < len(data):
            if data[index] != self.IAC:
                output.append(data[index])
                index += 1
                continue
            if index + 1 >= len(data):
                self._pending = data[index:]
                break
            command = data[index + 1]
            if command == self.IAC:
                output.append(self.IAC)
                index += 2
                continue
            if command == self.SB:
                end = data.find(bytes((self.IAC, self.SE)), index + 2)
                if end < 0:
                    self._pending = data[index:]
                    break
                option = data[index + 2] if index + 2 < end else None
                subcommand = data[index + 3] if index + 3 < end else None
                if option == self.TTYPE and subcommand == self.SEND:
                    reply.extend((self.IAC, self.SB, self.TTYPE, self.IS))
                    reply.extend(b"xterm-256color")
                    reply.extend((self.IAC, self.SE))
                index = end + 2
                continue
            if command in {self.DO, self.DONT, self.WILL, self.WONT}:
                if index + 2 >= len(data):
                    self._pending = data[index:]
                    break
                option = data[index + 2]
                if command == self.WILL:
                    response = self.DO if option in {self.BINARY, self.ECHO, self.SGA} else self.DONT
                    if option == self.BINARY:
                        self.remote_binary = True
                    reply.extend((self.IAC, response, option))
                elif command == self.DO:
                    response = self.WILL if option in {self.BINARY, self.SGA, self.TTYPE, self.NAWS} else self.WONT
                    if option == self.BINARY:
                        self.local_binary = True
                    reply.extend((self.IAC, response, option))
                    if option == self.NAWS:
                        reply.extend(self.window_size())
                elif command == self.WONT and option == self.BINARY:
                    self.remote_binary = False
                elif command == self.DONT and option == self.BINARY:
                    self.local_binary = False
                index += 3
                continue
            index += 2
        return bytes(output), bytes(reply)

    @classmethod
    def encode(cls, data: bytes) -> bytes:
        return data.replace(bytes((cls.IAC,)), bytes((cls.IAC, cls.IAC)))


class NetworkTerminalDaemon:
    def __init__(
        self,
        root: Path,
        host: str,
        port: int = 23,
        *,
        clock=time.monotonic,
        poll_interval: float = 0.02,
        self_reap: bool = True,
    ) -> None:
        self.root = root
        self.host = host
        self.port = int(port)
        self._clock = clock
        self._poll_interval = max(0.01, float(poll_interval))
        self._self_reap = bool(self_reap)
        self._last_reap_check = 0.0
        self._control_offset = 0
        self._running = True
        self._paused = False
        self._socket: socket.socket | None = None
        self._codec = TelnetCodec()
        self._decoder = codecs.getincrementaldecoder("utf-8")("replace")
        self._cols = 100
        self._rows = 28
        self._login_buffer = ""
        self._username_sent = False
        self._password_sent = False
        self._transfer: dict[str, str] | None = None
        self._tx_queue: deque[dict[str, object]] = deque()
        self._tx_pending_bytes = 0
        self.engine = ArmedRuleEngine(clock=clock)
        self._terminal_ipc = BridgeTerminalIpc(
            network_terminal_socket_path(root, host, port),
            network_stream_path(root, host, port),
        )
        self._instance_uuid = str(uuid.uuid4())
        self._generation = 1
        self._started_at = time.time()
        self._last_heartbeat_at = 0.0
        self._last_offset_persist_at = 0.0

    @property
    def endpoint(self) -> str:
        return f"{self.host}:{self.port}"

    def _log(self, direction: str, data: str, *, source: str = "") -> int:
        text = str(data or "")
        inline = text[:_STREAM_INLINE_LIMIT]
        offset = append_jsonl(
            network_stream_path(self.root, self.host, self.port),
            {
                "ts": utc_now(),
                "endpoint": self.endpoint,
                "direction": direction,
                "encoding": "text",
                "data": inline,
                "truncated": len(text) > _STREAM_INLINE_LIMIT,
                "source": source,
            },
        )
        if direction == "rx":
            self._terminal_ipc.broadcast("output", data=inline, offset=offset)
        return offset

    def _write_state(self, status: str) -> None:
        path = network_state_path(self.root, self.host, self.port)
        path.parent.mkdir(parents=True, exist_ok=True)
        state = {
            "host": self.host,
            "port": self.port,
            "pid": os.getpid(),
            "status": status,
            "owner_pid": os.getppid(),
            "owner_instance": str(os.environ.get(AHA_WEB_INSTANCE_ENV) or ""),
            "updated_at": utc_now(),
            "rules": self.engine.snapshot(),
            "capabilities": ["network-transfer-v1"] + (["network-transfer-v2"] if self._codec.binary_ready else []),
            "telnet_binary": self._codec.binary_ready,
            "instance_uuid": self._instance_uuid,
            "generation": self._generation,
            "started_at": self._started_at,
            "heartbeat_at": time.time(),
        }
        if self._transfer is not None:
            state["transfer"] = dict(self._transfer)
        path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
        self._terminal_ipc.broadcast(
            "status",
            bridge={
                **state,
                "endpoint": self.endpoint,
                "alive": status != "stopped",
                "paused": status == "paused",
                "connected": status == "running",
            },
        )

    def _maybe_heartbeat(self) -> None:
        now = time.time()
        if now - self._last_heartbeat_at < _BRIDGE_HEARTBEAT_INTERVAL:
            return
        self._last_heartbeat_at = now
        path = network_state_path(self.root, self.host, self.port)
        try:
            state = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        except (json.JSONDecodeError, OSError):
            state = {}
        state.update(
            {
                "pid": os.getpid(),
                "status": "paused" if self._paused else "running" if self._socket is not None else str(state.get("status") or "connecting"),
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
            network_control_offset_path(self.root, self.host, self.port).write_text(str(self._control_offset), encoding="utf-8")
        except OSError:
            pass

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

    def _connect(self) -> bool:
        try:
            sock = socket.create_connection((self.host, self.port), timeout=3.0)
            sock.setblocking(False)
        except OSError as exc:
            self._log("system", f"connect failed: {exc}", source="network")
            return False
        self._socket = sock
        self._codec = TelnetCodec(self._cols, self._rows)
        try:
            self._socket.sendall(self._codec.initial_negotiation())
        except OSError:
            self._disconnect()
            return False
        self._decoder = codecs.getincrementaldecoder("utf-8")("replace")
        self._login_buffer = ""
        self._username_sent = False
        self._password_sent = False
        self._log("system", f"connected ({self.endpoint})", source="network")
        self._write_state("running")
        return True

    def _disconnect(self) -> None:
        self._discard_tx("network disconnected")
        if self._socket is not None:
            try:
                self._socket.close()
            except OSError:
                pass
        self._socket = None

    def _discard_tx(self, reason: str) -> None:
        if not self._tx_pending_bytes:
            return
        dropped = self._tx_pending_bytes
        self._tx_queue.clear()
        self._tx_pending_bytes = 0
        self._log("system", f"discarded {dropped} pending TX bytes ({reason})", source="network")

    def _send(self, text: str, *, source: str, secret: bool = False, audit_text: str | None = None) -> None:
        if not text:
            return
        self._send_bytes(
            text.encode("utf-8", "replace"),
            source=source,
            secret=secret,
            audit_text=audit_text if audit_text is not None else text,
        )

    def _send_bytes(self, payload: bytes, *, source: str, secret: bool = False, audit_text: str) -> None:
        if not payload or self._socket is None:
            return
        wire_payload = TelnetCodec.encode(payload)
        if self._tx_pending_bytes + len(wire_payload) > _MAX_NETWORK_TX_PENDING_BYTES:
            self._log("system", f"TX queue full; rejected {len(payload)} bytes", source=source)
            return
        self._tx_queue.append({
            "data": wire_payload,
            "offset": 0,
            "source": source,
            "secret": secret,
            "audit_text": audit_text,
        })
        self._tx_pending_bytes += len(wire_payload)
        self._flush_tx()

    def _flush_tx(self) -> None:
        while self._socket is not None and self._tx_queue:
            pending = self._tx_queue[0]
            payload = pending["data"]
            offset = int(pending["offset"])
            try:
                written = int(self._socket.send(payload[offset:]) or 0)
            except (BlockingIOError, InterruptedError):
                return
            except OSError:
                self._disconnect()
                return
            if written <= 0:
                return
            written = min(written, len(payload) - offset)
            pending["offset"] = offset + written
            self._tx_pending_bytes -= written
            if int(pending["offset"]) < len(payload):
                return
            self._tx_queue.popleft()
            if bool(pending["secret"]):
                self._log("system", "password submitted", source=str(pending["source"]))
            else:
                self._log("tx", str(pending["audit_text"]), source=str(pending["source"]))

    def _auto_login(self, text: str) -> None:
        self._login_buffer = (self._login_buffer + text)[-2048:]
        username, password = _read_credentials(self.root, self.host, self.port)
        if username and not self._username_sent and re.search(r"(?:login|username)\s*:\s*$", self._login_buffer, re.I):
            self._send(f"{username}\r", source="credential")
            self._username_sent = True
            self._login_buffer = ""
            return
        if self._username_sent and not self._password_sent and re.search(r"password\s*:\s*$", self._login_buffer, re.I):
            self._send(f"{password}\r", source="credential", secret=True)
            self._password_sent = True
            self._login_buffer = ""

    def _apply_control(self) -> None:
        path = network_control_path(self.root, self.host, self.port)
        records, self._control_offset = read_control_records(path, self._control_offset, limit=200)
        for record, _line_end in records:
            cmd = str(record.get("cmd") or "").strip().lower()
            if cmd == "stop":
                record_generation = int(record.get("generation") or 0)
                if record_generation > 0 and record_generation != self._generation:
                    self._log("system", f"ignored stale stop for generation {record_generation}", source="control")
                    continue
                self._transfer = None
                self._running = False
            elif cmd == "pause":
                self._paused = True
                self._transfer = None
                self._disconnect()
                self._log("system", "network terminal paused", source="control")
                self._write_state("paused")
            elif cmd == "resume":
                self._paused = False
                self._write_state("connecting")
            elif self._paused:
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
                    self._write_state("running" if self._socket else "connecting")
            elif cmd == "transfer_send":
                transfer_id = str(record.get("transfer_id") or "").strip()
                if self._transfer is None or self._transfer.get("id") != transfer_id:
                    self._log("system", "file transfer data rejected: lease mismatch", source="file-transfer")
                    continue
                data = str(record.get("data") or "")
                self._send(
                    data,
                    source="file-transfer",
                    audit_text=f"[file transfer {len(data.encode('utf-8'))} wire bytes]",
                )
            elif cmd == "transfer_send_bytes":
                transfer_id = str(record.get("transfer_id") or "").strip()
                if self._transfer is None or self._transfer.get("id") != transfer_id:
                    self._log("system", "file transfer data rejected: lease mismatch", source="file-transfer")
                    continue
                if not self._codec.binary_ready:
                    self._log("system", "file transfer bytes rejected: Telnet BINARY is not active", source="file-transfer")
                    continue
                try:
                    payload = base64.b64decode(str(record.get("data") or ""), validate=True)
                except (binascii.Error, ValueError):
                    self._log("system", "file transfer bytes rejected: invalid base64", source="file-transfer")
                    continue
                self._send_bytes(
                    payload,
                    source="file-transfer",
                    audit_text=f"[file transfer {len(payload)} binary bytes]",
                )
            elif cmd == "transfer_end":
                transfer_id = str(record.get("transfer_id") or "").strip()
                if self._transfer is not None and self._transfer.get("id") == transfer_id:
                    self._transfer = None
                    self._log("system", "file transfer lease released", source="file-transfer")
                    self._write_state("running" if self._socket else "connecting")
            elif cmd in {"send", "send_raw"}:
                if self._transfer is not None:
                    self._log("system", f"ignored {cmd} during file transfer", source="control")
                    continue
                raw = record.get("data", record.get("send", ""))
                data = str(raw or "") if cmd == "send_raw" else decode_escapes(raw)
                self._send(data, source=str(record.get("source") or "interactive"))
            elif cmd == "resize":
                if self._transfer is not None:
                    self._log("system", "ignored resize during file transfer", source="control")
                    continue
                self._resize(record.get("cols"), record.get("rows"))
            elif cmd == "arm":
                rule_id = str(record.get("id") or "").strip()
                previous = next((item for item in self.engine.rules if item["id"] == rule_id), None)
                try:
                    rule = self.engine.arm(record)
                except re.error as exc:
                    self._log("system", f"arm rejected: invalid regex ({exc})", source="control")
                    continue
                if self._rule_unchanged(previous, rule):
                    continue
                self._log("system", f"rule {rule['id']} armed", source=f"rule:{rule['id']}")
                self._write_state("running" if self._socket else "connecting")
            elif cmd == "disarm":
                rule_id = str(record.get("id") or "").strip()
                self.engine.disarm(rule_id)
                self._log("system", f"rule {rule_id} disarmed", source="control")

    def _resize(self, cols: object, rows: object) -> None:
        try:
            self._cols = max(20, min(int(cols or self._cols), 240))
            self._rows = max(8, min(int(rows or self._rows), 80))
        except (TypeError, ValueError):
            return
        payload = self._codec.resize(self._cols, self._rows)
        if payload and self._socket is not None:
            try:
                self._socket.sendall(payload)
            except OSError:
                self._disconnect()

    def _apply_ipc_commands(self, commands: list[dict]) -> None:
        for command in commands:
            command_type = str(command.get("type") or "").strip().lower()
            if command_type == "input" and not self._paused:
                if self._transfer is not None:
                    continue
                self._send(str(command.get("data") or "")[:65536], source="web-xterm")
            elif command_type == "resize" and self._transfer is None:
                self._resize(command.get("cols"), command.get("rows"))

    def _fire(self, rules: list[dict]) -> None:
        for rule in rules:
            if self._transfer is not None:
                self._log("system", f"rule {rule['id']} suppressed during file transfer", source=f"rule:{rule['id']}")
                continue
            self._send(rule["send"], source=f"rule:{rule['id']}")
            self._log("system", f"rule {rule['id']} fired (fires={rule['fires']})", source=f"rule:{rule['id']}")

    def run(self) -> None:
        control = network_control_path(self.root, self.host, self.port)
        try:
            state = json.loads(network_state_path(self.root, self.host, self.port).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            state = None
        if isinstance(state, dict):
            self._generation = max(1, int(state.get("generation") or 1))
            self._instance_uuid = str(state.get("instance_uuid") or self._instance_uuid)
        offset_file = network_control_offset_path(self.root, self.host, self.port)
        try:
            persisted = int(offset_file.read_text(encoding="utf-8").strip() or "0") if offset_file.exists() else 0
        except (OSError, ValueError):
            persisted = 0
        file_size = control.stat().st_size if control.exists() else 0
        self._control_offset = persisted if 0 < persisted <= file_size else file_size
        self._terminal_ipc.start()
        try:
            self._write_state("connecting")
            retry_at = 0.0
            while self._running:
                self._apply_control()
                if not self._running:
                    break
                self._maybe_heartbeat()
                self._persist_control_offset()
                now = self._clock()
                if self._self_reap and now - self._last_reap_check >= 8.0:
                    self._last_reap_check = now
                    if not network_target_referenced_by_active_task(self.root, self.host, self.port):
                        self._log("system", "no active task references endpoint; reaping bridge", source="network")
                        break
                if not self._paused and self._socket is None and now >= retry_at:
                    if not self._connect():
                        self._write_state("connecting")
                        retry_at = now + 2.0
                if not self._paused and self._socket is not None:
                    fired, expired = self.engine.on_tick()
                    self._fire(fired)
                    for rule, reason in expired:
                        self._log("system", f"rule {rule['id']} disarmed ({reason})", source=f"rule:{rule['id']}")
                remote_socket = self._socket if not self._paused else None
                readers: list[object] = self._terminal_ipc.readables()
                if remote_socket is not None:
                    readers.append(remote_socket)
                writers: list[object] = self._terminal_ipc.writables()
                if remote_socket is not None and self._tx_queue:
                    writers.append(remote_socket)
                try:
                    readable, writable, _ = select.select(
                        readers,
                        writers,
                        [],
                        self._poll_interval,
                    )
                except (OSError, TypeError, ValueError):
                    self._disconnect()
                    continue
                self._apply_ipc_commands(self._terminal_ipc.process(readable, writable))
                if remote_socket is not None and remote_socket in writable:
                    self._flush_tx()
                if remote_socket is None or remote_socket not in readable:
                    continue
                try:
                    chunk = remote_socket.recv(4096)
                except (BlockingIOError, InterruptedError):
                    continue
                except OSError:
                    chunk = b""
                if not chunk:
                    self._log("system", "connection closed; reconnecting", source="network")
                    self._disconnect()
                    self._write_state("connecting")
                    retry_at = self._clock() + 1.0
                    continue
                binary_before = self._codec.binary_ready
                payload, reply = self._codec.feed(chunk)
                if reply and self._socket is not None:
                    try:
                        self._socket.sendall(reply)
                    except OSError:
                        self._disconnect()
                if self._codec.binary_ready != binary_before:
                    self._write_state("running" if self._socket else "connecting")
                text = self._decoder.decode(payload)
                if not text:
                    continue
                self._log("rx", text)
                self._auto_login(text)
                fired, expired = self.engine.on_text(text)
                self._fire(fired)
                for rule, reason in expired:
                    self._log("system", f"rule {rule['id']} disarmed ({reason})", source=f"rule:{rule['id']}")
        finally:
            self._log("system", "network terminal stopped", source="network")
            self._disconnect()
            self._paused = False
            self._transfer = None
            self._write_state("stopped")
            self._terminal_ipc.close()


def network_stream_page(
    root: Path,
    host: str,
    port: int = 23,
    *,
    after: int | None = None,
    before: int | None = None,
    limit: int = 1000,
) -> dict:
    path = network_stream_path(root, host, port)
    file_size = path.stat().st_size if path.exists() else 0
    end_offset = file_size if before is None else max(0, min(int(before), file_size))
    safe_limit = max(1, min(int(limit or 1000), 4000))
    if after is not None:
        records, next_offset = iter_jsonl_records_from(path, max(0, int(after)), limit=safe_limit)
        return {
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
        "events": [{**record, "offset": offset} for record, offset in tail],
        "after_offset": end_offset,
        "has_more": False,
        "limit": safe_limit,
    }


__all__ = [
    "NetworkTerminalDaemon",
    "TelnetCodec",
    "append_network_control",
    "ensure_network_terminal",
    "network_alive",
    "network_control_offset_path",
    "network_status",
    "network_stream_page",
    "network_stream_path",
    "network_terminal_socket_path",
    "network_transfer_lock_path",
    "network_target_referenced_by_active_task",
    "stop_all_network_terminals",
    "task_network_target",
]
