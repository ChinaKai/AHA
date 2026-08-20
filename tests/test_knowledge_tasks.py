from __future__ import annotations

import threading
from pathlib import Path

import pytest

from aha_cli.domain.models import default_knowledge_config, is_knowledge_run, is_knowledge_task
from aha_cli.services.knowledge_tasks import (
    KNOWLEDGE_RUN_TITLE,
    create_knowledge_task,
    ensure_knowledge_run,
    finish_knowledge_task,
    start_knowledge_task,
)
from aha_cli.services.run_delete import RunDeleteError, delete_run
from aha_cli.services.run_lifecycle_actions import RunLifecycleActionError, set_run_lifecycle_status
from aha_cli.services.run_retention import RunRetentionError, apply_run_retention
from aha_cli.store.config import load_config
from aha_cli.store import knowledge_capture as capture
from aha_cli.store import knowledge_nav_drafts as nav_drafts
from aha_cli.store.filesystem import task_snapshot
from aha_cli.store.io import write_json
from aha_cli.store.knowledge import init_knowledge_base
from aha_cli.store.paths import config_path, run_dir
from aha_cli.store.runs import list_run_summaries, require_plan


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
    start_knowledge_task(root, context, "开始整理。")
    finish_knowledge_task(root, context, "整理完成。", ok=True)

    detail = task_snapshot(root, context["run_id"], context["task_id"])
    assert is_knowledge_task(detail["task"])
    assert detail["task"]["status"] == "completed"
    assert detail["task"]["knowledge_operation"] == "capture_distill"
    assert distilled == []
    messages = (run_dir(root, context["run_id"]) / "tasks" / context["task_id"] / "messages.jsonl").read_text(encoding="utf-8")
    assert "整理 demo Capture" in messages
    assert "整理完成" in messages


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
