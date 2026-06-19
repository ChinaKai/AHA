"""Distill knowledge from a finished task (Phase 3).

When a task round is finalized, this module turns the final report (plus the
``record_task_update`` context: summary / changed_files / verification / risks)
into one or more **project solution candidates** and routes them through the
curation gate:

- ``manual`` (default) → candidate lands in the ``.pending`` review queue
- ``auto``             → candidate is written straight into the knowledge base
                         (and auto-committed if git sync is on)
- ``off``              → distillation is skipped

The preferred path is a machine-readable knowledge sidecar emitted by the same
agent that writes the task final or memo report. The deterministic heuristic is
kept as a dependency-free fallback for old prompts or malformed/missing
sidecars; the manual gate means a human refines the candidate before it counts.

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
    prior_entries: list[dict] | None = None,
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
        "prior_entries": _prior_entry_summaries(prior_entries or []),
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


def _clean_markdown(text: str) -> str:
    lines = [line.rstrip() for line in (text or "").strip().splitlines()]
    while lines and not lines[0].strip():
        lines.pop(0)
    while lines and not lines[-1].strip():
        lines.pop()
    return "\n".join(lines).strip()


_USEFUL_SECTION_KEYWORDS = (
    "完成内容",
    "稳定结果",
    "关键结论",
    "产出物",
    "可复用经验",
    "有效解法",
    "处理结果",
    "改了什么",
    "验证情况",
)


def _extract_useful_markdown(text: str) -> str:
    cleaned = _clean_markdown(text)
    if not cleaned:
        return ""
    sections: list[list[str]] = []
    current: list[str] = []
    current_useful = False
    for line in cleaned.splitlines():
        heading = line.lstrip()
        is_heading = heading.startswith("#")
        if is_heading:
            if current and current_useful:
                sections.append(current)
            current = [line]
            current_useful = any(keyword in heading for keyword in _USEFUL_SECTION_KEYWORDS)
        elif current:
            current.append(line)
        else:
            current = [line]
            current_useful = True
    if current and current_useful:
        sections.append(current)
    useful = "\n\n".join("\n".join(section).strip() for section in sections if "\n".join(section).strip())
    return useful or cleaned


def _prior_entry_summaries(entries: list[dict]) -> list[dict]:
    summaries: list[dict] = []
    for entry in entries[:5]:
        meta = entry.get("meta", {}) if isinstance(entry.get("meta"), dict) else {}
        summaries.append({
            "id": str(meta.get("id") or meta.get("slug") or ""),
            "title": str(meta.get("title") or "(untitled)"),
            "project_key": str(meta.get("project_key") or ""),
            "excerpt": _excerpt(entry.get("body") or "", limit=240),
        })
    return summaries


def heuristic_solution_candidate(context: dict) -> list[dict]:
    """Deterministic fallback distiller: one solution candidate from the final.

    It does not invent content; it reorganizes what the task already recorded
    into the solution schema so a reviewer can confirm/trim it. The final report
    body is always used: as the "effective solution" when no summary is given,
    and otherwise kept as a dedicated excerpt section.
    """
    summary = (context.get("summary") or "").strip()
    source_body = _clean_markdown(context.get("final_body") or "")
    useful_body = _extract_useful_markdown(source_body)

    title_basis = summary or context.get("task_title") or useful_body or "Untitled solution"
    title = title_basis.strip().splitlines()[0][:120] if title_basis.strip() else "Untitled solution"

    changed = context.get("changed_files") or []
    verification = context.get("verification") or []
    risks = context.get("risks") or []

    # Effective solution prefers the explicit summary, but falls back to useful
    # sections from the source report so stored knowledge keeps structure and is
    # not reduced to a short, lossy prefix.
    solution = summary or useful_body or "(见任务 final/report)"

    body_parts = ["## 问题 / 触发条件", context.get("task_title") or "(见任务标题)"]
    body_parts += ["", "## 有效解法", solution]
    # When a summary exists, keep useful source sections as their own section so
    # the body still contains the evidence behind the summary.
    if summary and useful_body:
        body_parts += ["", "## 来源摘录", useful_body]
    if changed:
        body_parts += ["", "## 涉及文件"] + [f"- {f}" for f in changed]
    if verification:
        body_parts += ["", "## 验证方式"] + [f"- {v}" for v in verification]
    prior_entries = context.get("prior_entries") or []
    if prior_entries:
        body_parts += [
            "",
            "## 既有知识复核",
            "以下旧知识与本次任务相关；如本次结论与其冲突，审核时应更新或废弃旧条目。",
        ]
        for entry in prior_entries:
            label = entry.get("title") or "(untitled)"
            entry_id = entry.get("id") or "-"
            excerpt = entry.get("excerpt") or ""
            body_parts.append(f"- {label} ({entry_id}): {excerpt}")
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


def _prior_entries_for_distill(
    root: Path,
    config: dict | None,
    *,
    workspace_path: str | None,
    goal: str | None,
    texts: list[str],
) -> list[dict]:
    if not workspace_path:
        return []
    try:
        from aha_cli.services.knowledge_retrieval import _terms, retrieve_for_task
        from aha_cli.store.knowledge import project_key_aliases

        keys = project_key_aliases(Path(workspace_path), goal=goal)
        return retrieve_for_task(
            root,
            config,
            project_key=keys[0],
            project_keys=keys,
            terms=_terms(*texts),
            max_entries=3,
        )
    except Exception:  # noqa: BLE001 - prior knowledge is advisory only
        return []


def _source_ref(source: dict | None) -> str:
    source = source or {}
    if source.get("source_type") == "memo_report" and source.get("memo_id"):
        return "/".join(
            str(source.get(key))
            for key in ("run_id", "task_id", "memo_id")
            if source.get(key)
        )
    return "/".join(
        str(source.get(key))
        for key in ("run_id", "task_id", "round_id")
        if source.get(key)
    )


def _sidecar_kind(candidate: dict) -> str:
    raw = str(candidate.get("kind") or "solutions").strip().lower()
    if raw in {"wiki", "wikis"}:
        return "wiki"
    return "solutions"


def _sidecar_body(candidate: dict, kind: str) -> str:
    body = str(candidate.get("body") or "").strip()
    if body:
        return body.rstrip() + "\n"
    if kind == "wiki":
        conclusion = str(candidate.get("conclusion") or candidate.get("summary") or "").strip()
        scope = str(candidate.get("applicability") or candidate.get("scope_note") or "").strip()
        rules = str(candidate.get("rules") or candidate.get("rule") or "").strip()
        example = str(candidate.get("example") or "").strip()
        locations = "\n".join(f"- {item}" for item in _as_list(candidate.get("related_files") or candidate.get("files")))
        update_condition = str(candidate.get("update_when") or candidate.get("invalid_when") or "").strip()
        parts = [
            "## 结论",
            conclusion,
            "",
            "## 适用范围",
            scope,
            "",
            "## 规则 / 约定",
            rules,
            "",
            "## 示例",
            example or "-",
            "",
            "## 相关位置",
            locations or "-",
            "",
            "## 更新条件",
            update_condition or "-",
        ]
        return "\n".join(parts).strip() + "\n"
    problem = str(candidate.get("problem") or candidate.get("trigger") or "").strip()
    solution = str(candidate.get("solution") or candidate.get("fix") or "").strip()
    locations = "\n".join(f"- {item}" for item in _as_list(candidate.get("related_files") or candidate.get("files")))
    verification = "\n".join(f"- {item}" for item in _as_list(candidate.get("verification") or candidate.get("checks")))
    invalid_when = str(candidate.get("invalid_when") or "").strip()
    parts = [
        "## 适用场景",
        problem or "-",
        "",
        "## 问题 / 触发信号",
        problem or "-",
        "",
        "## 推荐做法",
        solution or str(candidate.get("summary") or "").strip() or "-",
        "",
        "## 关键位置",
        locations or "-",
        "",
        "## 验证方式",
        verification or "-",
        "",
        "## 失效条件 / 适用边界",
        invalid_when or "-",
    ]
    return ("\n".join(parts).strip() or str(candidate.get("summary") or "").strip()) + "\n"


def _sidecar_source_meta(source: dict | None) -> dict:
    source = source or {}
    ref = _source_ref(source)
    if source.get("source_type") == "memo_report":
        meta: dict = {"source_memos": [ref] if ref else []}
        if source.get("run_id") and source.get("task_id"):
            meta["source_tasks"] = [f"{source.get('run_id')}/{source.get('task_id')}"]
        return meta
    return {"source_tasks": [ref] if ref else []}


def normalize_sidecar_candidates(context: dict, raw_candidates: list[dict]) -> list[dict]:
    """Convert final/report sidecar JSON into pending candidate records."""
    normalized: list[dict] = []
    source = context.get("source")
    for raw in raw_candidates:
        title = str(raw.get("title") or "").strip()
        kind = _sidecar_kind(raw)
        body = _sidecar_body(raw, kind)
        if not title or not body.strip():
            continue
        tags = _as_list(raw.get("tags"))
        related_files = _as_list(raw.get("related_files") or raw.get("files"))
        meta = {
            "type": "wiki" if kind == "wiki" else "solution",
            "outcome": str(raw.get("outcome") or "success"),
            "confidence": raw.get("confidence", 0.7),
            "tags": tags,
            "related_files": related_files,
            "distilled_by": "sidecar",
            **_sidecar_source_meta(source),
        }
        invalid_when = str(raw.get("invalid_when") or "").strip()
        if invalid_when:
            meta["invalid_when"] = invalid_when
            if "失效条件" not in body and "invalid" not in body.lower():
                body = body.rstrip() + f"\n\n## 失效条件 / 适用边界\n{invalid_when}\n"
        normalized.append({
            "kind": kind,
            "scope": str(raw.get("scope") or "project"),
            "project_key": raw.get("project_key") or context.get("project_key"),
            "title": title,
            "body": body,
            "meta": meta,
            "source": source,
        })
    return normalized


def distill_and_enqueue(
    root: Path,
    config: dict | None,
    context: dict,
    *,
    distiller: Distiller | None = None,
    candidates: list[dict] | None = None,
) -> dict:
    """Produce candidates and route them through the curation gate."""
    cfg = knowledge_config(config)
    if not cfg.get("enabled"):
        return {"ok": True, "skipped": "knowledge disabled", "candidates": 0}

    gate = (cfg.get("curation") or {}).get("gate", "manual")
    if gate == "off":
        return {"ok": True, "skipped": "curation gate off", "candidates": 0}

    produce = distiller or heuristic_solution_candidate
    produced = candidates if candidates is not None else produce(context)
    candidates = [c for c in (produced or []) if c.get("title")]
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
    sidecar_candidates: list[dict] | None = None,
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
            prior_entries=_prior_entries_for_distill(
                root,
                config,
                workspace_path=workspace_path,
                goal=goal,
                texts=[task_title, final_body, final_context.get("summary", "") if isinstance(final_context, dict) else ""],
            ),
        )
        candidates = (
            normalize_sidecar_candidates(context, sidecar_candidates)
            if sidecar_candidates is not None
            else None
        )
        return distill_and_enqueue(root, config, context, distiller=distiller, candidates=candidates)
    except Exception as exc:  # noqa: BLE001 - distillation must never break finalize
        return {"ok": False, "error": f"distill failed: {exc}"}


def distill_after_memo_report(
    root: Path,
    run_id: str,
    task_id: str,
    memo_id: str,
    report_body: str,
    *,
    memo_title: str = "",
    workspace_path: str | None = None,
    goal: str | None = None,
    distiller: Distiller | None = None,
    sidecar_candidates: list[dict] | None = None,
) -> dict:
    """Failure-isolated memo report hook. Never raises into report writeback."""
    try:
        config = config_for(root)
        if not knowledge_config(config).get("enabled"):
            return {"ok": True, "skipped": "knowledge disabled"}
        key = project_key(Path(workspace_path), goal=goal) if workspace_path else None
        if not key:
            return {"ok": True, "skipped": "no workspace for project key"}
        context = build_distill_context(
            final_body=report_body,
            final_context=None,
            task_title=memo_title or "Memo completion report",
            project_key_value=key,
            source={"source_type": "memo_report", "run_id": run_id, "task_id": task_id, "memo_id": memo_id},
            prior_entries=_prior_entries_for_distill(
                root,
                config,
                workspace_path=workspace_path,
                goal=goal,
                texts=[memo_title, report_body],
            ),
        )
        candidates = (
            normalize_sidecar_candidates(context, sidecar_candidates)
            if sidecar_candidates is not None
            else None
        )
        return distill_and_enqueue(root, config, context, distiller=distiller, candidates=candidates)
    except Exception as exc:  # noqa: BLE001 - distillation must never break memo reports
        return {"ok": False, "error": f"distill failed: {exc}"}


def config_for(root: Path) -> dict:
    # Lazy import keeps store/finals.py free of a services import cycle.
    from aha_cli.store.config import load_config

    return load_config(root)
