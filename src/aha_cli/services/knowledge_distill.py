"""Distill knowledge from a finished task (Phase 3).

When a task round is finalized, this module turns the final report (plus the
``record_task_update`` context: summary / changed_files / verification / risks)
into one or more **project solution candidates** and routes them through the
curation gate:

- ``manual`` (default) → candidate lands in the ``.pending`` review queue
- ``auto``             → candidate is written straight into the knowledge base
                         (and auto-committed if git sync is on)
- ``off``              → distillation is skipped

The transform that produces candidates is pluggable via ``distiller`` so a
backend sub-agent can replace the deterministic heuristic later. The default
heuristic is dependency-free and deterministic, which keeps finalize cheap and
testable; the manual gate means a human refines the candidate before it counts.

All entry points are failure-isolated: ``distill_after_finalize`` never raises,
so knowledge distillation can never break task finalization.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from aha_cli.store.knowledge import (
    auto_commit_message_for,
    enqueue_candidate,
    init_knowledge_base,
    knowledge_config,
    project_key,
    write_entry,
)

Distiller = Callable[[dict], list[dict]]


def build_distill_context(
    *,
    final_body: str,
    final_context: dict | None,
    task_title: str,
    project_key_value: str,
    source: dict,
) -> dict:
    ctx = final_context or {}
    return {
        "final_body": final_body or "",
        "task_title": task_title or "",
        "project_key": project_key_value,
        "summary": ctx.get("summary") or "",
        "changed_files": _as_list(ctx.get("changed_files")),
        "verification": _as_list(ctx.get("verification")),
        "risks": _as_list(ctx.get("risks")),
        "source": source,
    }


def _as_list(value) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value] if value.strip() else []
    if isinstance(value, list):
        return [str(item) for item in value if str(item).strip()]
    return [str(value)]


def _excerpt(text: str, limit: int = 600) -> str:
    """Collapse whitespace and truncate to a bounded single-paragraph excerpt."""
    collapsed = " ".join((text or "").split())
    if len(collapsed) <= limit:
        return collapsed
    return collapsed[:limit].rstrip() + " …"


def heuristic_solution_candidate(context: dict) -> list[dict]:
    """Deterministic fallback distiller: one solution candidate from the final.

    It does not invent content; it reorganizes what the task already recorded
    into the solution schema so a reviewer can confirm/trim it. The final report
    body is always used: as the "effective solution" when no summary is given,
    and otherwise kept as a dedicated excerpt section.
    """
    summary = (context.get("summary") or "").strip()
    final_excerpt = _excerpt(context.get("final_body") or "")

    title_basis = summary or context.get("task_title") or final_excerpt or "Untitled solution"
    title = title_basis.strip().splitlines()[0][:120] if title_basis.strip() else "Untitled solution"

    changed = context.get("changed_files") or []
    verification = context.get("verification") or []
    risks = context.get("risks") or []

    # Effective solution prefers the explicit summary, but falls back to the
    # final report excerpt so distillation always reflects the final.
    solution = summary or final_excerpt or "(见任务 final)"

    body_parts = ["## 问题 / 触发条件", context.get("task_title") or "(见任务标题)"]
    body_parts += ["", "## 有效解法", solution]
    # When a summary exists, keep the final excerpt as its own section so the
    # final body is still captured rather than discarded.
    if summary and final_excerpt:
        body_parts += ["", "## final 摘录", final_excerpt]
    if changed:
        body_parts += ["", "## 涉及文件"] + [f"- {f}" for f in changed]
    if verification:
        body_parts += ["", "## 验证方式"] + [f"- {v}" for v in verification]
    body_parts += [
        "",
        "## 失效条件 / 适用边界",
        "\n".join(f"- {r}" for r in risks) if risks else "- (待补充)",
    ]
    body = "\n".join(body_parts).strip() + "\n"

    candidate = {
        "kind": "solutions",
        "scope": "project",
        "project_key": context.get("project_key"),
        "title": title,
        "body": body,
        "meta": {
            "type": "solution",
            "outcome": "success",
            "confidence": 0.4,  # heuristic, unreviewed
            "tags": [],
            "related_files": changed,
            "source_tasks": [_source_ref(context.get("source"))],
            "distilled_by": "heuristic",
        },
        "source": context.get("source"),
    }
    return [candidate]


def _source_ref(source: dict | None) -> str:
    source = source or {}
    return "/".join(
        str(source.get(key))
        for key in ("run_id", "task_id", "round_id")
        if source.get(key)
    )


def distill_and_enqueue(
    root: Path,
    config: dict | None,
    context: dict,
    *,
    distiller: Distiller | None = None,
) -> dict:
    """Produce candidates and route them through the curation gate."""
    cfg = knowledge_config(config)
    if not cfg.get("enabled"):
        return {"ok": True, "skipped": "knowledge disabled", "candidates": 0}

    gate = (cfg.get("curation") or {}).get("gate", "manual")
    if gate == "off":
        return {"ok": True, "skipped": "curation gate off", "candidates": 0}

    produce = distiller or heuristic_solution_candidate
    candidates = [c for c in (produce(context) or []) if c.get("title")]
    if not candidates:
        return {"ok": True, "candidates": 0, "gate": gate}

    # Ensure the KB skeleton (index, README, and the .gitignore that excludes
    # .pending/) exists before writing anything — including the first time this
    # is triggered by finalize without a prior `aha kb init`.
    init_knowledge_base(root, config)

    if gate == "auto":
        written = []
        for cand in candidates:
            path = write_entry(
                root,
                config=config,
                scope=cand.get("scope", "project"),
                kind=cand.get("kind", "solutions"),
                project_key_value=cand.get("project_key"),
                title=cand["title"],
                body=cand.get("body", ""),
                meta=cand.get("meta", {}),
            )
            written.append(str(path))
        result = {"ok": True, "gate": "auto", "written": written, "candidates": len(written)}
        # Auto-commit (and optionally push) the freshly written entries.
        from aha_cli.services.knowledge_git import auto_commit_after_change

        result["git"] = auto_commit_after_change(
            root, auto_commit_message_for(candidates), config
        )
        return result

    # Default: manual gate -> queue for review, never touches the tracked tree.
    enqueued = [str(enqueue_candidate(root, config, cand)) for cand in candidates]
    return {"ok": True, "gate": "manual", "enqueued": enqueued, "candidates": len(enqueued)}


def distill_after_finalize(
    root: Path,
    run_id: str,
    task_id: str,
    final_body: str,
    final_context: dict | None,
    *,
    task_title: str = "",
    workspace_path: str | None = None,
    goal: str | None = None,
    round_id: str | None = None,
    distiller: Distiller | None = None,
) -> dict:
    """Failure-isolated finalize hook. Never raises into the task flow."""
    try:
        config = config_for(root)
        if not knowledge_config(config).get("enabled"):
            return {"ok": True, "skipped": "knowledge disabled"}
        key = project_key(Path(workspace_path), goal=goal) if workspace_path else None
        if not key:
            return {"ok": True, "skipped": "no workspace for project key"}
        context = build_distill_context(
            final_body=final_body,
            final_context=final_context,
            task_title=task_title,
            project_key_value=key,
            source={"run_id": run_id, "task_id": task_id, "round_id": round_id},
        )
        return distill_and_enqueue(root, config, context, distiller=distiller)
    except Exception as exc:  # noqa: BLE001 - distillation must never break finalize
        return {"ok": False, "error": f"distill failed: {exc}"}


def config_for(root: Path) -> dict:
    # Lazy import keeps store/finals.py free of a services import cycle.
    from aha_cli.store.config import load_config

    return load_config(root)
