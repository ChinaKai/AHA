"""Persistent hardware serial session with agent-controlled armed rules.

Real board bringup is not a request/response RPC: once powered, the board streams
log output continuously, and the operator watches that stream to decide *when* to
send anything. Some windows (U-Boot's ``Hit any key to stop autoboot`` countdown)
are too short to survive an LLM round-trip, so the agent must be able to pre-arm a
reaction that fires locally at native speed.

This module models exactly that:

* a long-lived daemon (:class:`HardwareSessionDaemon`) holds the transport open and
  continuously appends the raw RX stream to the task ``hardware_io.jsonl`` timeline;
* the agent drives it through an append-only control inbox (``send`` / ``arm`` /
  ``disarm`` / ``stop``);
* :class:`ArmedRuleEngine` matches armed rules against the live stream (or timers)
  and fires the configured TX without waiting for the agent — but the agent decides
  *whether* a rule is armed at all, so control stays with the task.

The rule engine is pure and clock-injectable so it can be unit tested without any
real device; the daemon loop is verified against a PTY loopback.
"""

from __future__ import annotations

import json
import os
import re
import select
import time
from pathlib import Path

from aha_cli.domain.models import utc_now
from aha_cli.services.hardware_io import append_hardware_io_record, require_hardware_io_task
from aha_cli.store.io import append_jsonl, iter_jsonl_records_from
from aha_cli.store.paths import run_dir

HARDWARE_SESSION_CONTROL_COMMANDS = {"send", "arm", "disarm", "stop"}
_MATCH_BUFFER_LIMIT = 8192

_ESCAPE_MAP = {
    "r": "\r",
    "n": "\n",
    "t": "\t",
    "0": "\x00",
    "\\": "\\",
    '"': '"',
    "'": "'",
}


def decode_escapes(text: object) -> str:
    """Interpret the backslash escapes operators type for serial input (``\\r`` etc.)."""

    source = str(text if text is not None else "")
    out: list[str] = []
    index = 0
    length = len(source)
    while index < length:
        char = source[index]
        if char == "\\" and index + 1 < length:
            nxt = source[index + 1]
            if nxt == "x" and index + 3 < length:
                try:
                    out.append(chr(int(source[index + 2 : index + 4], 16)))
                    index += 4
                    continue
                except ValueError:
                    pass
            if nxt in _ESCAPE_MAP:
                out.append(_ESCAPE_MAP[nxt])
                index += 2
                continue
        out.append(char)
        index += 1
    return "".join(out)


def _safe_channel(channel: object) -> str:
    text = str(channel or "hardware").strip().lower() or "hardware"
    return re.sub(r"[^a-z0-9_.-]+", "-", text)


def hardware_session_dir(root: Path, run_id: str, task_id: str, channel: str) -> Path:
    return run_dir(root, run_id) / "tasks" / task_id / "hardware" / _safe_channel(channel)


def hardware_session_state_path(root: Path, run_id: str, task_id: str, channel: str) -> Path:
    return hardware_session_dir(root, run_id, task_id, channel) / "session.json"


def hardware_session_control_path(root: Path, run_id: str, task_id: str, channel: str) -> Path:
    return hardware_session_dir(root, run_id, task_id, channel) / "control.jsonl"


def append_session_control(
    root: Path,
    run_id: str,
    task_id: str,
    channel: str,
    command: dict,
) -> dict:
    """Queue a control command for the (possibly already running) session daemon."""

    require_hardware_io_task(root, run_id, task_id)
    cmd = str(command.get("cmd") or "").strip().lower()
    if cmd not in HARDWARE_SESSION_CONTROL_COMMANDS:
        raise ValueError(f"Unknown hardware session command: {cmd or '(empty)'}")
    record = {**command, "cmd": cmd, "ts": str(command.get("ts") or utc_now())}
    append_jsonl(hardware_session_control_path(root, run_id, task_id, channel), record)
    return record


class ArmedRuleEngine:
    """Match agent-armed rules against the live RX stream and timers.

    Two trigger kinds:

    * ``match`` — fire when ``pattern`` appears in the rolling RX buffer. This catches
      prompts (``stop autoboot``, ``login:``) the instant the bytes arrive locally.
    * ``timer`` — fire on a ``delay``/``interval`` schedule regardless of output. This
      is the "spam ``\\r`` for 3s right after reset" trick that reliably lands a board
      in the U-Boot prompt without depending on detection latency.

    Rules auto-disarm on ``max_fires`` or ``ttl_seconds``/``duration_seconds`` so a
    one-shot interrupt never keeps stealing input from later normal boots.
    """

    def __init__(self, clock=time.monotonic) -> None:
        self._clock = clock
        self.rules: list[dict] = []
        self._counter = 0
        self._buffer = ""

    def _next_id(self) -> str:
        self._counter += 1
        return f"r{self._counter}"

    def arm(self, payload: dict) -> dict:
        now = self._clock()
        rule_id = str(payload.get("id") or "").strip() or self._next_id()
        self.rules = [rule for rule in self.rules if rule["id"] != rule_id]
        delay = float(payload.get("delay_seconds", 0) or 0)
        interval = float(payload.get("interval_seconds", 0) or 0)
        explicit = str(payload.get("trigger") or "").strip().lower()
        is_timer = explicit == "timer" or (not explicit and (delay or interval) and not payload.get("pattern"))
        send_raw = payload.get("send", payload.get("data", ""))
        rule = {
            "id": rule_id,
            "trigger": "timer" if is_timer else "match",
            "pattern": str(payload.get("pattern") or ""),
            "regex": bool(payload.get("regex")),
            "send": decode_escapes(send_raw),
            "send_display": str(send_raw),
            "max_fires": max(0, int(payload.get("max_fires", 1) or 0)),
            "ttl_seconds": max(0.0, float(payload.get("ttl_seconds", 0) or 0)),
            "delay_seconds": max(0.0, delay),
            "interval_seconds": max(0.0, interval),
            "duration_seconds": max(0.0, float(payload.get("duration_seconds", 0) or 0)),
            "fires": 0,
            "armed_at": now,
            "next_fire_at": (now + delay) if is_timer else None,
        }
        if rule["trigger"] == "match" and rule["regex"]:
            re.compile(rule["pattern"])  # surface invalid patterns to the caller
        self.rules.append(rule)
        return rule

    def disarm(self, rule_id: str) -> int:
        rule_id = str(rule_id or "").strip()
        before = len(self.rules)
        self.rules = [rule for rule in self.rules if rule["id"] != rule_id]
        return before - len(self.rules)

    def _expire(self, now: float) -> list[tuple[dict, str]]:
        expired: list[tuple[dict, str]] = []
        keep: list[dict] = []
        for rule in self.rules:
            if rule["ttl_seconds"] and now - rule["armed_at"] >= rule["ttl_seconds"]:
                expired.append((rule, "ttl"))
                continue
            if (
                rule["trigger"] == "timer"
                and rule["duration_seconds"]
                and now - rule["armed_at"] >= rule["duration_seconds"]
            ):
                expired.append((rule, "duration"))
                continue
            keep.append(rule)
        self.rules = keep
        return expired

    def on_text(self, text: str, *, now: float | None = None):
        moment = self._clock() if now is None else now
        expired = self._expire(moment)
        self._buffer = (self._buffer + str(text or ""))[-_MATCH_BUFFER_LIMIT:]
        fired: list[dict] = []
        survivors: list[dict] = []
        for rule in self.rules:
            if rule["trigger"] != "match":
                survivors.append(rule)
                continue
            if rule["regex"]:
                matched = re.search(rule["pattern"], self._buffer) is not None
            else:
                matched = bool(rule["pattern"]) and rule["pattern"] in self._buffer
            if not matched:
                survivors.append(rule)
                continue
            rule["fires"] += 1
            rule["last_fire"] = moment
            fired.append(rule)
            if rule["max_fires"] and rule["fires"] >= rule["max_fires"]:
                continue  # auto-disarm
            survivors.append(rule)
        self.rules = survivors
        if fired:
            # Consume the matched window so a lingering substring does not re-fire.
            self._buffer = ""
        return fired, expired

    def on_tick(self, *, now: float | None = None):
        moment = self._clock() if now is None else now
        expired = self._expire(moment)
        fired: list[dict] = []
        survivors: list[dict] = []
        for rule in self.rules:
            if rule["trigger"] != "timer" or rule["next_fire_at"] is None or moment < rule["next_fire_at"]:
                survivors.append(rule)
                continue
            rule["fires"] += 1
            rule["last_fire"] = moment
            fired.append(rule)
            if rule["interval_seconds"] > 0:
                rule["next_fire_at"] = moment + rule["interval_seconds"]
            else:
                rule["next_fire_at"] = None
            if rule["max_fires"] and rule["fires"] >= rule["max_fires"]:
                continue  # auto-disarm
            if rule["next_fire_at"] is None:
                continue  # one-shot timer consumed
            survivors.append(rule)
        self.rules = survivors
        return fired, expired

    def snapshot(self) -> list[dict]:
        keys = (
            "id",
            "trigger",
            "pattern",
            "regex",
            "send_display",
            "max_fires",
            "ttl_seconds",
            "delay_seconds",
            "interval_seconds",
            "duration_seconds",
            "fires",
        )
        return [{key: rule.get(key) for key in keys} for rule in self.rules]


class FdTransport:
    """Minimal byte transport over a file descriptor (UART device or PTY)."""

    def __init__(self, fd: int, *, close_fd: bool = True) -> None:
        self._fd = fd
        self._close_fd = close_fd

    def fileno(self) -> int:
        return self._fd

    def read(self, size: int = 4096) -> bytes | None:
        try:
            return os.read(self._fd, size)  # b"" means EOF
        except (BlockingIOError, InterruptedError):
            return None
        except OSError:
            return b""

    def write(self, data: bytes) -> int:
        try:
            return os.write(self._fd, data)
        except OSError:
            return 0

    def close(self) -> None:
        if self._close_fd:
            try:
                os.close(self._fd)
            except OSError:
                pass


def _configure_tty(fd: int, baudrate: int) -> None:
    try:
        import termios
        import tty
    except ImportError:
        return
    try:
        if not os.isatty(fd):
            return
        tty.setraw(fd)
        baud_const = getattr(termios, f"B{int(baudrate)}", None)
        if baud_const is not None:
            attrs = termios.tcgetattr(fd)
            attrs[4] = baud_const  # input speed
            attrs[5] = baud_const  # output speed
            termios.tcsetattr(fd, termios.TCSANOW, attrs)
    except Exception:
        # Best effort: a non-serial fd or unsupported baud should not abort the session.
        pass


def open_uart_transport(device: str, baudrate: int = 115200) -> FdTransport:
    fd = os.open(device, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
    _configure_tty(fd, baudrate)
    return FdTransport(fd)


class HardwareSessionDaemon:
    """Hold the transport open, stream RX, and execute the agent's armed rules."""

    def __init__(
        self,
        root: Path,
        run_id: str,
        task_id: str,
        channel: str,
        transport,
        *,
        endpoint: str = "",
        agent_id: str = "main",
        clock=time.monotonic,
        poll_interval: float = 0.02,
        idle_timeout: float | None = None,
    ) -> None:
        self.root = root
        self.run_id = run_id
        self.task_id = task_id
        self.channel = _safe_channel(channel)
        self.transport = transport
        self.endpoint = endpoint
        self.agent_id = agent_id
        self._clock = clock
        self._poll_interval = max(0.001, float(poll_interval))
        self._idle_timeout = idle_timeout
        self.engine = ArmedRuleEngine(clock=clock)
        self._control_offset = 0
        self._running = True

    def _log(self, direction: str, data: str, *, source: str = "", encoding: str = "text") -> None:
        append_hardware_io_record(
            self.root,
            self.run_id,
            self.task_id,
            {
                "agent_id": self.agent_id,
                "channel": self.channel,
                "endpoint": self.endpoint,
                "direction": direction,
                "encoding": encoding,
                "data": data,
                "source": source,
            },
            default_agent_id=self.agent_id,
        )

    def _write_state(self, status: str) -> None:
        path = hardware_session_state_path(self.root, self.run_id, self.task_id, self.channel)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {
                    "channel": self.channel,
                    "endpoint": self.endpoint,
                    "agent_id": self.agent_id,
                    "status": status,
                    "pid": os.getpid(),
                    "updated_at": utc_now(),
                    "rules": self.engine.snapshot(),
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )

    def _send(self, data_text: str, *, source: str) -> None:
        if not data_text:
            return
        self.transport.write(data_text.encode("utf-8", "replace"))
        self._log("tx", data_text, source=source)

    def _fire(self, fired: list[dict]) -> None:
        for rule in fired:
            self._send(rule["send"], source=f"rule:{rule['id']}")
            self._log(
                "system",
                f"rule {rule['id']} fired (fires={rule['fires']})",
                source=f"rule:{rule['id']}",
            )

    def _note_expired(self, expired: list[tuple[dict, str]]) -> None:
        for rule, reason in expired:
            self._log("system", f"rule {rule['id']} disarmed ({reason})", source=f"rule:{rule['id']}")

    def _apply_control(self) -> None:
        path = hardware_session_control_path(self.root, self.run_id, self.task_id, self.channel)
        records, self._control_offset = iter_jsonl_records_from(path, self._control_offset, limit=200)
        for record, _line_end in records:
            cmd = str(record.get("cmd") or "").strip().lower()
            if cmd == "stop":
                self._log("system", "stop requested", source="control")
                self._running = False
            elif cmd == "send":
                self._send(decode_escapes(record.get("data", record.get("send", ""))), source="interactive")
            elif cmd == "arm":
                try:
                    rule = self.engine.arm(record)
                except re.error as exc:
                    self._log("system", f"arm rejected: invalid regex ({exc})", source="control")
                    continue
                self._log(
                    "system",
                    f"rule {rule['id']} armed (trigger={rule['trigger']}, send={rule['send_display']!r})",
                    source=f"rule:{rule['id']}",
                )
                self._write_state("running")
            elif cmd == "disarm":
                rule_id = str(record.get("id") or record.get("rule") or "").strip()
                removed = self.engine.disarm(rule_id)
                self._log(
                    "system",
                    f"rule {rule_id} disarmed (manual)" if removed else f"disarm: no rule {rule_id}",
                    source="control",
                )
                self._write_state("running")

    def run(self) -> None:
        self._log("system", f"session started ({self.endpoint or self.channel})", source="session")
        self._write_state("running")
        fd = self.transport.fileno()
        last_activity = self._clock()
        try:
            while self._running:
                self._apply_control()
                fired, expired = self.engine.on_tick()
                self._fire(fired)
                self._note_expired(expired)
                if fired or expired:
                    self._write_state("running")
                if not self._running:
                    break
                try:
                    readable, _, _ = select.select([fd], [], [], self._poll_interval)
                except (OSError, ValueError):
                    break
                if readable:
                    chunk = self.transport.read(4096)
                    if chunk is None:
                        continue
                    if chunk == b"":
                        self._log("system", "transport closed (EOF)", source="session")
                        break
                    text = chunk.decode("utf-8", "replace")
                    self._log("rx", text)
                    fired, expired = self.engine.on_text(text)
                    self._fire(fired)
                    self._note_expired(expired)
                    if fired or expired:
                        self._write_state("running")
                    last_activity = self._clock()
                elif self._idle_timeout is not None and self._clock() - last_activity >= self._idle_timeout:
                    self._log("system", "idle timeout", source="session")
                    break
        finally:
            self._log("system", "session stopped", source="session")
            self._write_state("stopped")
            self.transport.close()


def read_session_state(root: Path, run_id: str, task_id: str, channel: str) -> dict | None:
    path = hardware_session_state_path(root, run_id, task_id, channel)
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


__all__ = [
    "ArmedRuleEngine",
    "FdTransport",
    "HardwareSessionDaemon",
    "HARDWARE_SESSION_CONTROL_COMMANDS",
    "append_session_control",
    "decode_escapes",
    "hardware_session_control_path",
    "hardware_session_dir",
    "hardware_session_state_path",
    "open_uart_transport",
    "read_session_state",
]
