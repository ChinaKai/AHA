"""Machine-level Telnet terminal bridge for task network hardware debugging."""

from __future__ import annotations

import codecs
import json
import os
import re
import select
import socket
import subprocess
import sys
import time
from pathlib import Path

from aha_cli.constants import PLAN_FILE, RUNS_DIR
from aha_cli.domain.models import normalize_task_hardware_debug, utc_now
from aha_cli.services.hardware_bridge import pid_alive, set_parent_death_signal
from aha_cli.services.hardware_session import ArmedRuleEngine, decode_escapes
from aha_cli.services.terminal_ipc import BridgeTerminalIpc
from aha_cli.store.io import append_jsonl, iter_jsonl_records_from, iter_jsonl_reverse
from aha_cli.store.paths import aha_home_path

NETWORK_CONTROL_COMMANDS = {"send", "send_raw", "resize", "arm", "disarm", "pause", "resume", "stop"}
_STREAM_INLINE_LIMIT = 12000
_TERMINAL_TASK_STATUSES = {"completed", "failed", "blocked"}


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


def network_credentials_path(root: Path, host: str, port: int = 23) -> Path:
    return network_terminal_dir(root, host, port) / "credentials.json"


def network_terminal_socket_path(root: Path, host: str, port: int = 23) -> Path:
    return network_terminal_dir(root, host, port) / "terminal.sock"


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
    if not state or not pid_alive(state.get("pid")):
        return {"endpoint": endpoint, "host": host, "port": int(port), "status": "stopped", "alive": False, "paused": False}
    status = str(state.get("status") or "connecting")
    return {
        "endpoint": endpoint,
        "host": host,
        "port": int(port),
        "status": status,
        "alive": True,
        "paused": status == "paused",
        "connected": status == "running",
        "pid": state.get("pid"),
        "rules": state.get("rules") or [],
    }


def append_network_control(root: Path, host: str, port: int, command: dict) -> dict:
    cmd = str(command.get("cmd") or "").strip().lower()
    if cmd not in NETWORK_CONTROL_COMMANDS:
        raise ValueError(f"Unknown network terminal command: {cmd or '(empty)'}")
    record = {**command, "cmd": cmd, "ts": str(command.get("ts") or utc_now())}
    append_jsonl(network_control_path(root, host, port), record)
    return record


def ensure_network_terminal(
    root: Path,
    host: str,
    port: int = 23,
    *,
    username: str = "",
    password: str = "",
    launcher: list[str] | None = None,
) -> dict:
    import fcntl

    terminal_dir = network_terminal_dir(root, host, port)
    terminal_dir.mkdir(parents=True, exist_ok=True)
    _write_credentials(root, host, int(port), username, password)
    lock_path = terminal_dir / "bridge.lock"
    lock_fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o644)
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        status = network_status(root, host, port)
        if status.get("alive"):
            return status
        command = [
            *(launcher or [sys.executable, "-m", "aha_cli"]),
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
        proc = subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            preexec_fn=set_parent_death_signal,
            start_new_session=False,
            env=child_env,
        )
        network_state_path(root, host, port).write_text(
            json.dumps(
                {"host": host, "port": int(port), "pid": proc.pid, "status": "starting", "updated_at": utc_now()},
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        return {"endpoint": f"{host}:{int(port)}", "status": "starting", "alive": True, "paused": False, "pid": proc.pid}
    finally:
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
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

    def __init__(self, cols: int = 100, rows: int = 28) -> None:
        self._pending = b""
        self.cols = max(20, min(int(cols), 240))
        self.rows = max(8, min(int(rows), 80))

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
                    response = self.DO if option in {self.ECHO, self.SGA} else self.DONT
                    reply.extend((self.IAC, response, option))
                elif command == self.DO:
                    response = self.WILL if option in {self.SGA, self.TTYPE, self.NAWS} else self.WONT
                    reply.extend((self.IAC, response, option))
                    if option == self.NAWS:
                        reply.extend(self.window_size())
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
        self.engine = ArmedRuleEngine(clock=clock)
        self._terminal_ipc = BridgeTerminalIpc(
            network_terminal_socket_path(root, host, port),
            network_stream_path(root, host, port),
        )

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
            "updated_at": utc_now(),
            "rules": self.engine.snapshot(),
        }
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

    def _connect(self) -> bool:
        try:
            sock = socket.create_connection((self.host, self.port), timeout=3.0)
            sock.setblocking(False)
        except OSError as exc:
            self._log("system", f"connect failed: {exc}", source="network")
            return False
        self._socket = sock
        self._codec = TelnetCodec(self._cols, self._rows)
        self._decoder = codecs.getincrementaldecoder("utf-8")("replace")
        self._login_buffer = ""
        self._username_sent = False
        self._password_sent = False
        self._log("system", f"connected ({self.endpoint})", source="network")
        self._write_state("running")
        return True

    def _disconnect(self) -> None:
        if self._socket is not None:
            try:
                self._socket.close()
            except OSError:
                pass
        self._socket = None

    def _send(self, text: str, *, source: str, secret: bool = False) -> None:
        if not text or self._socket is None:
            return
        try:
            self._socket.sendall(TelnetCodec.encode(text.encode("utf-8", "replace")))
        except OSError:
            self._disconnect()
            return
        if secret:
            self._log("system", "password submitted", source=source)
        else:
            self._log("tx", text, source=source)

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
        records, self._control_offset = iter_jsonl_records_from(path, self._control_offset, limit=200)
        for record, _line_end in records:
            cmd = str(record.get("cmd") or "").strip().lower()
            if cmd == "stop":
                self._running = False
            elif cmd == "pause":
                self._paused = True
                self._disconnect()
                self._log("system", "network terminal paused", source="control")
                self._write_state("paused")
            elif cmd == "resume":
                self._paused = False
                self._write_state("connecting")
            elif self._paused:
                self._log("system", f"ignored {cmd} while paused", source="control")
            elif cmd in {"send", "send_raw"}:
                raw = record.get("data", record.get("send", ""))
                data = str(raw or "") if cmd == "send_raw" else decode_escapes(raw)
                self._send(data, source=str(record.get("source") or "interactive"))
            elif cmd == "resize":
                self._resize(record.get("cols"), record.get("rows"))
            elif cmd == "arm":
                try:
                    rule = self.engine.arm(record)
                except re.error as exc:
                    self._log("system", f"arm rejected: invalid regex ({exc})", source="control")
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
                self._send(str(command.get("data") or "")[:65536], source="web-xterm")
            elif command_type == "resize":
                self._resize(command.get("cols"), command.get("rows"))

    def _fire(self, rules: list[dict]) -> None:
        for rule in rules:
            self._send(rule["send"], source=f"rule:{rule['id']}")
            self._log("system", f"rule {rule['id']} fired (fires={rule['fires']})", source=f"rule:{rule['id']}")

    def run(self) -> None:
        control = network_control_path(self.root, self.host, self.port)
        self._control_offset = control.stat().st_size if control.exists() else 0
        self._terminal_ipc.start()
        try:
            self._write_state("connecting")
            retry_at = 0.0
            while self._running:
                self._apply_control()
                if not self._running:
                    break
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
                try:
                    readable, writable, _ = select.select(
                        readers,
                        self._terminal_ipc.writables(),
                        [],
                        self._poll_interval,
                    )
                except (OSError, TypeError, ValueError):
                    self._disconnect()
                    continue
                self._apply_ipc_commands(self._terminal_ipc.process(readable, writable))
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
                payload, reply = self._codec.feed(chunk)
                if reply and self._socket is not None:
                    try:
                        self._socket.sendall(reply)
                    except OSError:
                        self._disconnect()
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
    "network_status",
    "network_stream_page",
    "network_terminal_socket_path",
    "network_target_referenced_by_active_task",
    "task_network_target",
]
