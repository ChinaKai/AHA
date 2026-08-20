from __future__ import annotations

import json
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from aha_cli.domain.models import default_knowledge_config, is_knowledge_run, is_knowledge_task
from aha_cli.backends.codex import handle_codex_event
from aha_cli.services.knowledge_tasks import (
    KNOWLEDGE_RUN_TITLE,
    create_knowledge_task,
    ensure_knowledge_run,
    finish_knowledge_task,
    knowledge_agent_execution_context,
    resolve_knowledge_agent_config,
    run_knowledge_agent_turn,
    start_knowledge_task,
)
from aha_cli.services.run_delete import RunDeleteError, delete_run
from aha_cli.services.run_lifecycle_actions import RunLifecycleActionError, set_run_lifecycle_status
from aha_cli.services.run_retention import RunRetentionError, apply_run_retention
from aha_cli.store.config import load_config
from aha_cli.store import knowledge_capture as capture
from aha_cli.store import knowledge_nav_drafts as nav_drafts
from aha_cli.store.filesystem import append_event, conversation_events_page, task_snapshot
from aha_cli.store.io import write_json
from aha_cli.store.knowledge import init_knowledge_base
from aha_cli.store.paths import config_path, event_path, run_dir, session_path
from aha_cli.store.runs import list_run_summaries, locked_plan, require_plan, save_plan


def _setup(tmp_path: Path) -> tuple[Path, dict]:
    root = tmp_path / ".aha"
    kb = default_knowledge_config()
    cfg = {"backend": "codex", "knowledge": kb}
    write_json(config_path(root), cfg)
    init_knowledge_base(root, cfg)
    return root, load_config(root)


class _ImmediateThread:
    def __init__(self, *, target, daemon=None, name=None):
        self.target = target
        self.name = name

    def start(self) -> None:
        if self.name != "aha-sidecar-lock-heartbeat":
            self.target()

    def join(self, timeout=None) -> None:
        return None


def test_knowledge_run_is_reused_and_visible(tmp_path: Path) -> None:
    root, cfg = _setup(tmp_path)
    first = ensure_knowledge_run(root, cfg)
    second = ensure_knowledge_run(root, cfg)

    assert first == second
    plan = require_plan(root, first)
    assert plan["goal"] == KNOWLEDGE_RUN_TITLE
    assert is_knowledge_run(plan)
    summary = next(item for item in list_run_summaries(root) if item["id"] == first)
    assert summary["system_managed"] is True
    assert summary["system_purpose"] == "knowledge_management"


def test_reused_knowledge_run_tracks_saved_agent_profile(tmp_path: Path) -> None:
    root, cfg = _setup(tmp_path)
    run_id = ensure_knowledge_run(root, cfg)
    cfg["knowledge"]["agent"] = {
        "backend": "claude",
        "model": "claude-sonnet-4-6",
        "reasoning_effort": "high",
        "proxy_enabled": True,
    }

    assert ensure_knowledge_run(root, cfg) == run_id
    main_agent = require_plan(root, run_id)["main_agent"]
    assert main_agent["backend"] == "claude"
    assert main_agent["model"] == "claude-sonnet-4-6"
    assert main_agent["reasoning_effort"] == "high"
    assert main_agent["proxy_enabled"] is True


def test_reused_knowledge_run_migrates_legacy_title_to_english(tmp_path: Path) -> None:
    root, cfg = _setup(tmp_path)
    run_id = ensure_knowledge_run(root, cfg)
    with locked_plan(root, run_id):
        plan = require_plan(root, run_id)
        plan["goal"] = "KB 管理"
        save_plan(root, plan)

    assert ensure_knowledge_run(root, cfg) == run_id
    assert require_plan(root, run_id)["goal"] == "AHA Knowledge Manager"
    summary = next(item for item in list_run_summaries(root) if item["id"] == run_id)
    assert summary["goal"] == "AHA Knowledge Manager"


def test_knowledge_task_can_use_operation_workspace(tmp_path: Path) -> None:
    root, cfg = _setup(tmp_path)
    workspace = tmp_path / "project"
    workspace.mkdir()

    context = create_knowledge_task(
        root,
        cfg,
        operation="project_navigation",
        title="Project navigation",
        description="Build project navigation.",
        workspace_path=str(workspace),
    )

    assert context["task"]["workspace_path"] == str(workspace)


def test_knowledge_operation_task_records_visible_lifecycle_without_redistill(tmp_path: Path, monkeypatch) -> None:
    root, cfg = _setup(tmp_path)
    distilled: list[bool] = []
    monkeypatch.setattr("aha_cli.store.finals._distill_knowledge_safe", lambda *args, **kwargs: distilled.append(True))

    context = create_knowledge_task(
        root,
        cfg,
        operation="capture_distill",
        title="Capture 整理 · demo",
        description="整理 demo Capture。",
        metadata={"note_id": "demo"},
    )
    start_knowledge_task(root, context)
    finish_knowledge_task(root, context, "整理完成。", ok=True)

    detail = task_snapshot(root, context["run_id"], context["task_id"])
    assert is_knowledge_task(detail["task"])
    assert detail["task"]["status"] == "completed"
    assert detail["task"]["knowledge_operation"] == "capture_distill"
    assert distilled == []
    messages = (run_dir(root, context["run_id"]) / "tasks" / context["task_id"] / "messages.jsonl").read_text(encoding="utf-8")
    assert "整理 demo Capture" in messages
    assert "整理完成" in messages
    assert "开始整理" not in messages


def test_knowledge_agent_context_uses_standard_event_log_without_status_messages(tmp_path: Path) -> None:
    root, cfg = _setup(tmp_path)
    context = create_knowledge_task(
        root,
        cfg,
        operation="capture_distill",
        title="Capture",
        description="Distill capture.",
    )
    progress: list[tuple[str, dict | None]] = []

    prepared = knowledge_agent_execution_context(
        root,
        context,
        {"progress_callback": lambda event_type, data: progress.append((event_type, data))},
    )
    prepared["progress_callback"]("agent_command_started", {"command": "git status"})

    assert prepared["events_file"] == event_path(root, context["run_id"])
    assert prepared["run_id"] == context["run_id"]
    assert prepared["task_id"] == context["task_id"]
    assert prepared["source"] == "main"
    assert prepared["target"] == "main"
    assert prepared["knowledge_root"] == root
    assert prepared["knowledge_task_context"] == context
    assert progress == [("agent_command_started", {"command": "git status"})]
    messages = (run_dir(root, context["run_id"]) / "tasks" / context["task_id"] / "messages.jsonl").read_text(encoding="utf-8")
    assert "Started: git status" not in messages


def test_knowledge_agent_turn_uses_standard_task_chat_artifacts(tmp_path: Path, monkeypatch) -> None:
    from aha_cli.services.backend_runtime import backend_state_path, backend_status
    from aha_cli.services.chat import agent_chat

    root, cfg = _setup(tmp_path)
    context = create_knowledge_task(
        root,
        cfg,
        operation="capture_distill",
        title="Capture",
        description="Distill capture.",
    )
    start_knowledge_task(root, context)
    seen: dict = {}

    def run_codex(prompt, **kwargs):
        seen["prompt"] = prompt
        seen["session"] = dict(kwargs["session"])
        session = dict(kwargs["session"])
        session["backend_session_id"] = "kb-codex-session"
        append_event(
            root,
            context["run_id"],
            "agent_usage",
            {
                "task_id": context["task_id"],
                "target": "main",
                "usage": {"input_tokens": 1_000, "model_context_window": 10_000},
            },
        )
        return 0, "distilled reply", session

    def start_backend(_root, run_id, target, **kwargs):
        write_json(
            backend_state_path(root, run_id, target, context["task_id"]),
            {
                "target": target,
                "task_id": context["task_id"],
                "backend": "codex-chat",
                "status": "stopped",
                "managed": True,
                "model": kwargs.get("model"),
            },
        )
        args = SimpleNamespace(
            target=target,
            task_id=context["task_id"],
            from_start=False,
            once=True,
            interval=0.01,
            sender=target,
            reply_target=None,
            sandbox=kwargs.get("sandbox") or "read-only",
            approval=kwargs.get("approval") or "never",
            reasoning_effort=kwargs.get("reasoning_effort"),
            model=kwargs.get("model"),
            requested_model=None,
            prompt_prefix="",
            codex_bin="codex",
            claude_bin="claude",
            no_json=False,
            extra_arg=[],
        )
        assert agent_chat(root, run_id, args, backend_name="codex") == 0
        return {"status": "stopped", "started": True}

    monkeypatch.setattr("aha_cli.services.backend_runtime.start_backend", start_backend)
    monkeypatch.setattr("aha_cli.services.chat.run_codex_exec", run_codex)

    reply = run_knowledge_agent_turn(
        root,
        context,
        {
            "prompt": "distill this exact note",
            "backend": "codex",
            "model": "gpt-5.5",
            "reasoning_effort": "high",
        },
    )

    assert reply == "distilled reply"
    assert seen["session"]["task_id"] == context["task_id"]
    assert "distill this exact note" in seen["prompt"]
    saved_session = json.loads(session_path(root, context["run_id"], context["task_id"], "main").read_text(encoding="utf-8"))
    assert saved_session["backend_session_id"] == "kb-codex-session"
    events = [json.loads(line) for line in event_path(root, context["run_id"]).read_text(encoding="utf-8").splitlines()]
    metrics = next(event["data"] for event in events if event["type"] == "agent_prompt_metrics")
    assert metrics["total"]["chars"] > 0
    prompt_path = run_dir(root, context["run_id"]) / metrics["prompt_ref"]["path"]
    assert "distill this exact note" in prompt_path.read_text(encoding="utf-8")
    status = backend_status(root, context["run_id"], "main", context["task_id"])
    assert status["context_pressure"]["percent"] == 10.0


def test_knowledge_backend_events_use_standard_chat_and_command_views(tmp_path: Path) -> None:
    root, cfg = _setup(tmp_path)
    context = create_knowledge_task(
        root,
        cfg,
        operation="capture_distill",
        title="Capture",
        description="Distill capture.",
    )
    prepared = knowledge_agent_execution_context(root, context)
    common = {
        "events_file": prepared["events_file"],
        "run_id": prepared["run_id"],
        "task_id": prepared["task_id"],
        "source": prepared["source"],
        "target": prepared["target"],
    }
    handle_codex_event(
        json.dumps({"type": "item.started", "item": {"type": "command_execution", "command": "git status"}}),
        **common,
    )
    handle_codex_event(
        json.dumps({
            "type": "item.completed",
            "item": {
                "type": "command_execution",
                "command": "git status",
                "status": "completed",
                "exit_code": 0,
                "aggregated_output": "clean",
            },
        }),
        **common,
    )
    handle_codex_event(
        json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": "Distill complete."}}),
        **common,
    )

    chat = conversation_events_page(
        root,
        context["run_id"],
        context["task_id"],
        "main",
        categories={"chat"},
    )
    commands = conversation_events_page(
        root,
        context["run_id"],
        context["task_id"],
        "main",
        categories={"commands"},
    )

    assert any(event["type"] == "agent_message" and event["data"]["text"] == "Distill complete." for event in chat["events"])
    assert [event["type"] for event in commands["events"]] == ["agent_command_started", "agent_command_finished"]


def test_knowledge_tasks_use_saved_agent_profile_and_allow_explicit_override(tmp_path: Path) -> None:
    root, cfg = _setup(tmp_path)
    cfg["knowledge"]["agent"] = {
        "backend": "claude",
        "model": "claude-sonnet-4-6",
        "reasoning_effort": "high",
        "proxy_enabled": True,
    }

    inherited = create_knowledge_task(
        root,
        cfg,
        operation="sync_conflict",
        title="Resolve conflict",
        description="Resolve safely.",
    )
    assert inherited["backend"] == "claude"
    assert inherited["model"] == "claude-sonnet-4-6"
    assert inherited["reasoning_effort"] == "high"
    assert inherited["proxy_enabled"] is True
    inherited_detail = task_snapshot(root, inherited["run_id"], inherited["task_id"])["task"]
    assert inherited_detail["backend"] == "claude"
    assert inherited_detail["model"] == "claude-sonnet-4-6"
    assert inherited_detail["reasoning_effort"] == "high"
    assert inherited_detail["proxy_enabled"] is True

    overridden = create_knowledge_task(
        root,
        cfg,
        operation="capture_distill",
        title="Capture",
        description="Compatibility override.",
        backend="codex",
        model="gpt-5.5",
        reasoning_effort="xhigh",
        proxy_enabled=False,
    )
    assert overridden["backend"] == "codex"
    assert overridden["model"] == "gpt-5.5"
    assert overridden["reasoning_effort"] == "xhigh"
    assert overridden["proxy_enabled"] is False

    backend_only = resolve_knowledge_agent_config(cfg, backend="codex")
    assert backend_only["backend"] == "codex"
    assert backend_only["model"] is None
    assert backend_only["reasoning_effort"] is None
    assert backend_only["proxy_enabled"] is False


def test_knowledge_system_run_rejects_destructive_run_operations(tmp_path: Path) -> None:
    root, cfg = _setup(tmp_path)
    run_id = ensure_knowledge_run(root, cfg)

    with pytest.raises(RunDeleteError) as delete_error:
        delete_run(root, run_id, force=True)
    with pytest.raises(RunLifecycleActionError) as lifecycle_error:
        set_run_lifecycle_status(root, run_id, "hidden")
    with pytest.raises(RunRetentionError) as retention_error:
        apply_run_retention(root, run_id, force=True)

    assert delete_error.value.reason == "system_managed_run"
    assert lifecycle_error.value.reason == "system_managed_run"
    assert retention_error.value.reason == "system_managed_run"


def test_capture_dispatch_exception_finishes_visible_task(tmp_path: Path, monkeypatch) -> None:
    import aha_cli.services.knowledge_capture_distill as distill
    import aha_cli.web.knowledge_routes as routes

    root, cfg = _setup(tmp_path)
    note = capture.create_note(root, cfg, text="raw", title="broken capture")
    monkeypatch.setattr(threading, "Thread", _ImmediateThread)

    def fail_distill(*_args, **_kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(distill, "run_distill_job", fail_distill)

    dispatched = routes._default_dispatch_distill_job(root, cfg, note["id"], "codex", None)

    detail = task_snapshot(root, dispatched["management_task"]["run_id"], dispatched["management_task"]["task_id"])
    assert detail["task"]["status"] == "failed"
    assert capture.read_note(root, cfg, note["id"])["status"] == "failed"


def test_capture_dispatch_uses_saved_knowledge_agent_profile(tmp_path: Path, monkeypatch) -> None:
    import aha_cli.services.knowledge_capture_distill as distill
    import aha_cli.web.knowledge_routes as routes

    root, cfg = _setup(tmp_path)
    cfg["knowledge"]["agent"] = {
        "backend": "claude",
        "model": "claude-sonnet-4-6",
        "reasoning_effort": "high",
        "proxy_enabled": True,
    }
    note = capture.create_note(root, cfg, text="raw", title="profile capture")
    monkeypatch.setattr(threading, "Thread", _ImmediateThread)
    received: dict = {}

    def record_distill(*_args, **kwargs):
        received.update(kwargs)
        return {"ok": True, "candidates": 0}

    monkeypatch.setattr(distill, "run_distill_job", record_distill)

    routes._default_dispatch_distill_job(root, cfg, note["id"], None, None)

    assert received["backend"] == "claude"
    assert received["model"] == "claude-sonnet-4-6"
    assert received["reasoning_effort"] == "high"
    assert received["proxy_enabled"] is True


def test_project_nav_dispatch_exception_finishes_visible_task(tmp_path: Path, monkeypatch) -> None:
    import aha_cli.web.knowledge_routes as routes

    root, cfg = _setup(tmp_path)
    draft = nav_drafts.create_draft(root, cfg, {"project_key": "demo", "workspace_path": str(tmp_path)})
    monkeypatch.setattr(threading, "Thread", _ImmediateThread)

    def fail_nav(*_args, **_kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(routes, "run_project_nav_draft_job", fail_nav)

    dispatched = routes._default_dispatch_project_nav_job(
        root,
        cfg,
        draft["id"],
        project_key_value="demo",
        workspace_path=str(tmp_path),
    )

    detail = task_snapshot(root, dispatched["management_task"]["run_id"], dispatched["management_task"]["task_id"])
    assert detail["task"]["status"] == "failed"
    assert nav_drafts.read_draft(root, cfg, draft["id"])["status"] == "failed"


def test_project_nav_dispatch_uses_saved_knowledge_agent_profile(tmp_path: Path, monkeypatch) -> None:
    import aha_cli.web.knowledge_routes as routes

    root, cfg = _setup(tmp_path)
    cfg["knowledge"]["agent"] = {
        "backend": "claude",
        "model": "claude-sonnet-4-6",
        "reasoning_effort": "high",
        "proxy_enabled": True,
    }
    draft = nav_drafts.create_draft(root, cfg, {"project_key": "demo", "workspace_path": str(tmp_path)})
    monkeypatch.setattr(threading, "Thread", _ImmediateThread)
    received: dict = {}

    def record_nav(*_args, **kwargs):
        received.update(kwargs)
        return {"ok": True, "status": "completed", "summary": "done"}

    monkeypatch.setattr(routes, "run_project_nav_draft_job", record_nav)

    routes._default_dispatch_project_nav_job(
        root,
        cfg,
        draft["id"],
        project_key_value="demo",
        workspace_path=str(tmp_path),
    )

    assert received["backend"] == "claude"
    assert received["model"] == "claude-sonnet-4-6"
    assert received["reasoning_effort"] == "high"
    assert received["proxy_enabled"] is True
