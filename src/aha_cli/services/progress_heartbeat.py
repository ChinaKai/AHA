from __future__ import annotations

import json
import time
from collections.abc import Callable
from pathlib import Path

from aha_cli.store.filesystem import append_event, append_message


HEARTBEAT_COORDINATION = "agent_progress_heartbeat"
HEARTBEAT_TOOL_CALL_THRESHOLD = 8
HEARTBEAT_SECONDS_THRESHOLD = 60.0
HEARTBEAT_MIN_INTERVAL_SECONDS = 30.0

EDIT_TOOL_NAMES = {"Edit", "MultiEdit", "NotebookEdit", "Write"}
TEST_COMMAND_MARKERS = (
    "pytest",
    "python -m unittest",
    "python3 -m unittest",
    "unittest",
    "npm test",
    "pnpm test",
    "yarn test",
)
COMMIT_COMMAND_MARKERS = ("aha commit", "git commit")


class AgentProgressHeartbeat:
    def __init__(
        self,
        root: Path,
        run_id: str,
        *,
        task_id: str,
        agent_id: str,
        role: str,
        model_family: str,
        now: Callable[[], float] | None = None,
        tool_call_threshold: int = HEARTBEAT_TOOL_CALL_THRESHOLD,
        seconds_threshold: float = HEARTBEAT_SECONDS_THRESHOLD,
        min_interval_seconds: float = HEARTBEAT_MIN_INTERVAL_SECONDS,
    ) -> None:
        self.root = root
        self.run_id = run_id
        self.task_id = task_id
        self.agent_id = agent_id
        self.role = role
        self.model_family = model_family
        self.now = now or time.monotonic
        self.tool_call_threshold = tool_call_threshold
        self.seconds_threshold = seconds_threshold
        self.min_interval_seconds = min_interval_seconds
        self.started_at = self.now()
        self.last_heartbeat_at: float | None = None
        self.tool_calls_since_heartbeat = 0
        self.last_command = ""

    def handle_event(self, event_type: str, data: dict) -> None:
        normalized_type = normalized_tool_event_type(event_type)
        if normalized_type not in {"agent_command_started", "agent_command_finished"}:
            return
        now = self.now()
        command = tool_event_command(data)
        if command:
            self.last_command = command
        if normalized_type == "agent_command_started":
            self.tool_calls_since_heartbeat += 1
        reason = self._heartbeat_reason(normalized_type, data, now)
        if reason:
            self._emit(reason, now)

    def _heartbeat_reason(self, event_type: str, data: dict, now: float) -> str | None:
        if self._throttled(now):
            return None
        phase = tool_event_phase(data)
        if event_type == "agent_command_started" and phase:
            return phase
        if self.tool_calls_since_heartbeat >= self.tool_call_threshold:
            return "tool_loop"
        if now - self.started_at >= self.seconds_threshold:
            return "elapsed"
        return None

    def _throttled(self, now: float) -> bool:
        return self.last_heartbeat_at is not None and now - self.last_heartbeat_at < self.min_interval_seconds

    def _emit(self, reason: str, now: float) -> None:
        message = heartbeat_message(self.agent_id, reason, self.tool_calls_since_heartbeat, self.last_command)
        append_message(
            self.root,
            self.run_id,
            "browser",
            message,
            sender="aha",
            task_id=self.task_id,
            role=self.role,
            from_agent="aha",
            to_agent="browser",
            coordination=HEARTBEAT_COORDINATION,
            agent_id=self.agent_id,
        )
        append_event(
            self.root,
            self.run_id,
            "agent_progress_heartbeat",
            {
                "task_id": self.task_id,
                "agent_id": self.agent_id,
                "model_family": self.model_family,
                "reason": reason,
                "tool_calls_since_heartbeat": self.tool_calls_since_heartbeat,
                "last_command": self.last_command,
            },
        )
        self.last_heartbeat_at = now
        self.started_at = now
        self.tool_calls_since_heartbeat = 0


def normalized_tool_event_type(event_type: str) -> str:
    value = str(event_type or "")
    if value in {"agent_command_started", "tool_use"}:
        return "agent_command_started"
    if value in {"agent_command_finished", "tool_result", "toolUseResult"}:
        return "agent_command_finished"
    return value


def tool_event_phase(data: dict) -> str | None:
    tool_name = str(data.get("tool_name") or data.get("name") or "").strip()
    if tool_name in EDIT_TOOL_NAMES:
        return "edit"
    command = tool_event_command(data).lower()
    if any(marker in command for marker in COMMIT_COMMAND_MARKERS):
        return "commit"
    if any(marker in command for marker in TEST_COMMAND_MARKERS):
        return "test"
    return None


def tool_event_command(data: dict) -> str:
    command = str(data.get("command") or "").strip()
    if command:
        return command
    tool_name = str(data.get("tool_name") or data.get("name") or "").strip()
    tool_input = data.get("input") if isinstance(data.get("input"), dict) else {}
    if tool_name == "Bash" and tool_input.get("command"):
        return str(tool_input.get("command") or "").strip()
    if tool_input:
        return f"{tool_name} {json.dumps(tool_input, ensure_ascii=False, sort_keys=True)}".strip()
    return tool_name or "tool"


def heartbeat_message(agent_id: str, reason: str, tool_count: int, command: str) -> str:
    command_text = command[:180] if command else "unknown"
    if reason == "edit":
        detail = "即将进入编辑/写入阶段"
    elif reason == "test":
        detail = "正在进入测试/验证阶段"
    elif reason == "commit":
        detail = "正在进入提交阶段"
    elif reason == "elapsed":
        detail = "本轮已持续较久且还没有可见中途 update"
    else:
        detail = f"已连续执行 {tool_count} 次工具调用且还没有可见中途 update"
    return f"AHA 进度：`{agent_id}` {detail}；最近动作：{command_text}"
