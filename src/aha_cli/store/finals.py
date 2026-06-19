from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from aha_cli.domain.models import utc_now
from aha_cli.store.events import append_event as default_append_event
from aha_cli.store.io import write_json
from aha_cli.store.knowledge_sidecar import split_knowledge_sidecar
from aha_cli.store.paths import run_dir
from aha_cli.store.rounds import (
    ensure_task_round_record,
    task_lifecycle_round_path,
    task_round_final_meta_path,
    task_round_final_path,
)
from aha_cli.store.runs import locked_plan, require_plan, save_plan


def _run_relative_path(root: Path, run_id: str, path: Path) -> str:
    try:
        return str(path.relative_to(run_dir(root, run_id)))
    except ValueError:
        return str(path)


def write_task_result(
    root: Path,
    run_id: str,
    task_id: str,
    content: str,
    policy: str = "finalize",
    *,
    final_context: dict | None = None,
    now_func: Callable[[], str] = utc_now,
    append_event_func: Callable[[Path, str, str, dict], dict] = default_append_event,
    render_overview_func: Callable[..., object] | None = None,
) -> Path:
    now = now_func()
    visible_content, sidecar_candidates, sidecar_error = split_knowledge_sidecar(content)
    body = visible_content.rstrip() + "\n"
    distill_meta: dict | None = None
    with locked_plan(root, run_id):
        plan = require_plan(root, run_id)
        task = next((item for item in plan["tasks"] if item["id"] == task_id), None)
        if task is None or task.get("deleted_at"):
            raise SystemExit(f"Task not found: {task_id}")
        path = run_dir(root, run_id) / task["output_file"]
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8")
        meta = {"task_id": task_id, "policy": policy, "updated_at": now}
        if final_context:
            meta["final_context"] = final_context
        if sidecar_error:
            meta["knowledge_sidecar_error"] = sidecar_error
        if policy == "finalize":
            round_record = ensure_task_round_record(root, run_id, task, now_func=now_func)
            round_id = str(round_record["round_id"])
            final_path = task_round_final_path(root, run_id, task_id, round_id)
            final_path.parent.mkdir(parents=True, exist_ok=True)
            final_path.write_text(body, encoding="utf-8")
            final_meta_path = task_round_final_meta_path(root, run_id, task_id, round_id)
            meta |= {
                "round_id": round_id,
                "round_sequence": round_record.get("sequence"),
                "final_path": _run_relative_path(root, run_id, final_path),
            }
            write_json(final_meta_path, meta)
            round_record["status"] = "finalized"
            round_record["finalized_at"] = now
            round_record["final_path"] = _run_relative_path(root, run_id, final_path)
            round_record["final_meta_path"] = _run_relative_path(root, run_id, final_meta_path)
            write_json(task_lifecycle_round_path(root, run_id, task_id, round_id), round_record)
            task["last_final_round_id"] = round_id
            task["last_final_at"] = now
            distill_meta = {
                "task_title": task.get("title", ""),
                "workspace_path": task.get("workspace_path"),
                "goal": plan.get("goal"),
                "round_id": round_id,
            }
        write_json(path.with_suffix(".meta.json"), meta)
        plan["updated_at"] = now
        save_plan(root, plan)
        write_json(run_dir(root, run_id) / "tasks" / task_id / "task.json", task)
    append_event_func(
        root,
        run_id,
        "task_result_written",
        {"task_id": task_id, "path": str(path), "chars": len(body.strip()), "policy": policy, "round_id": meta.get("round_id")},
    )
    if policy == "finalize" and render_overview_func:
        render_overview_func(root, run_id, task_id, policy=policy)
    if policy == "finalize" and distill_meta is not None:
        _distill_knowledge_safe(root, run_id, task_id, body, final_context, distill_meta, sidecar_candidates)
    return path


def _distill_knowledge_safe(
    root: Path,
    run_id: str,
    task_id: str,
    body: str,
    final_context: dict | None,
    distill_meta: dict,
    sidecar_candidates: list[dict] | None,
) -> None:
    """Best-effort knowledge distillation hook. Never breaks finalization."""
    try:
        # Lazy import avoids a store -> services import cycle at module load.
        from aha_cli.services.knowledge_distill import distill_after_finalize

        distill_after_finalize(
            root,
            run_id,
            task_id,
            body,
            final_context,
            task_title=distill_meta.get("task_title", ""),
            workspace_path=distill_meta.get("workspace_path"),
            goal=distill_meta.get("goal"),
            round_id=distill_meta.get("round_id"),
            sidecar_candidates=sidecar_candidates,
        )
    except Exception:  # noqa: BLE001 - distillation is strictly best-effort
        pass
