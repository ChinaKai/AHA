"""Retrieve and inject knowledge into a task prompt (Phase 5).

Before a task starts, AHA looks up what the knowledge base already knows about
this project and injects a compact "已知经验" block into the task-main prompt so
the agent learns before it acts (the read edge of learn -> do -> distill).

Retrieval is deliberately simple and dependency-free (project-scoped first,
ranked by term overlap, bounded by entry/char budgets) — no vector store yet.
``knowledge_context_for_task`` is failure-isolated: any error yields an empty
string so prompt assembly is never broken.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Sequence

from aha_cli.store.knowledge import (
    iter_all_entries,
    knowledge_config,
    project_key_aliases,
)

_STOP = {
    "the", "and", "for", "with", "this", "that", "from", "into", "your", "you",
    "are", "was", "will", "task", "用户", "任务",
}


def _terms(*texts: str) -> list[str]:
    seen: list[str] = []
    for text in texts:
        raw = text or ""
        for token in re.findall(r"[a-z0-9]{3,}", raw.lower()):
            if token not in _STOP and token not in seen:
                seen.append(token)
        # CJK has no spaces; use adjacent-character bigrams so Chinese tasks and
        # Chinese knowledge can match (e.g. 串口桥接 -> 串口, 口桥, 桥接).
        for run in re.findall(r"[一-鿿]{2,}", raw):
            for i in range(len(run) - 1):
                bigram = run[i : i + 2]
                if bigram not in _STOP and bigram not in seen:
                    seen.append(bigram)
    return seen


def _score(entry: dict, terms: list[str]) -> int:
    meta = entry.get("meta", {})
    haystack = " ".join(
        [str(meta.get("title", "")), " ".join(str(t) for t in (meta.get("tags") or [])), entry.get("body", "")]
    ).lower()
    return sum(1 for term in terms if term in haystack)


def retrieve_for_task(
    root: Path,
    config: dict | None,
    *,
    project_key: str | None,
    project_keys: Sequence[str] | None = None,
    terms: list[str],
    max_entries: int = 5,
) -> list[dict]:
    """Return the most relevant entries for a project, ranked by term overlap.

    Project-scoped entries rank above general ones. When nothing matches the
    terms, fall back to the project's most recently updated entries so a project
    that has knowledge always contributes something.
    """
    keys = [key for key in [project_key, *(project_keys or [])] if key]
    project_key_set = set(keys)
    project_entries: list[dict] = []
    general_entries: list[dict] = []
    for entry in iter_all_entries(root, config):
        meta = entry.get("meta", {})
        if meta.get("status") == "deprecated":
            continue
        if meta.get("scope") == "project" and meta.get("project_key") in project_key_set:
            project_entries.append(entry)
        elif meta.get("scope") == "general":
            general_entries.append(entry)

    def rank(entries: list[dict]) -> list[tuple[int, str, dict]]:
        ranked = [(_score(e, terms), str(e.get("meta", {}).get("updated_at") or ""), e) for e in entries]
        # Higher score first, then most recently updated.
        ranked.sort(key=lambda item: (item[0], item[1]), reverse=True)
        return ranked

    # The project map (navigation) is always relevant: it tells the agent what
    # the project is and where each module lives, so pin it to the top of the
    # injection regardless of term overlap, then rank the rest as usual.
    def is_nav(entry: dict) -> bool:
        return entry.get("meta", {}).get("type") == "navigation"

    nav_entries = [e for _, _, e in rank([e for e in project_entries if is_nav(e)])]
    rest_project = [e for e in project_entries if not is_nav(e)]

    ordered = [e for score, _, e in rank(rest_project) if score > 0]
    ordered += [e for score, _, e in rank(general_entries) if score > 0]
    if not ordered:
        # Fallback: project knowledge by recency (general excluded to stay focused).
        ordered = [e for _, _, e in rank(rest_project)]
    ordered = nav_entries + [e for e in ordered if e not in nav_entries]
    return ordered[: max(0, max_entries)]


def format_injection(entries: list[dict], *, max_chars: int = 4000) -> str:
    """Render the injection block under a HARD total character budget.

    The budget is enforced even for the first entry: its excerpt is clipped to
    the remaining room rather than allowed to overflow.
    """
    if not entries:
        return ""
    header = [
        "项目已知经验 (knowledge base):",
        "开工前先参考以下从过往任务沉淀的知识；如与现状冲突以代码为准。",
    ]
    # When a project map is present, give the agent the read→locate→write-back
    # contract up front: use the map to jump to the right module instead of
    # reading the whole repo, and refresh it on structural changes at finalize.
    if any(e.get("meta", {}).get("type") == "navigation" for e in entries):
        header.append(
            "⚑ 本项目已有项目地图（navigation，置顶）：先据此定位相关模块、避免全局通读；"
            "收尾时若改动影响模块职责/入口/架构/盲区，请在 final/report 的 knowledge sidecar "
            "用 kind:\"navigation\" 回写更新地图。"
        )
    out = "\n".join(header + [""])
    for entry in entries:
        meta = entry.get("meta", {})
        kind = meta.get("type", "entry")
        title = meta.get("title", "(untitled)")
        head_line = f"\n- [{kind}] {title}"
        if len(head_line) > max_chars - len(out):
            break  # no room for even this entry's title line
        block = head_line
        excerpt = " ".join((entry.get("body", "") or "").split())
        # The project map carries the most actionable orientation, so let it keep
        # a fuller excerpt than ordinary entries (still under the total budget).
        per_entry_cap = 1200 if kind == "navigation" else 360
        if len(excerpt) > per_entry_cap:
            excerpt = excerpt[:per_entry_cap].rstrip() + " …"
        if excerpt:
            room = max_chars - len(out) - len(block) - len("\n  ")
            if room > 8:
                if len(excerpt) > room:
                    excerpt = excerpt[: room - 2].rstrip() + " …"
                block += "\n  " + excerpt
        out += block
    return out.strip()


def knowledge_context_for_task(root: Path, run_id: str, task: dict) -> str:
    """Failure-isolated knowledge injection for a task-main prompt."""
    try:
        # Lazy imports keep this off the hot import path and avoid cycles.
        from aha_cli.store.config import load_config

        config = load_config(root)
        cfg = knowledge_config(config)
        if not cfg.get("enabled"):
            return ""
        workspace_path = task.get("workspace_path")
        if not workspace_path:
            return ""
        # Read edge of remote sync: pull before learning so we study the latest
        # shared knowledge. Failure-isolated — a failed pull falls back to the
        # local KB rather than blocking the task.
        try:
            from aha_cli.services.knowledge_git import auto_pull_before_task

            auto_pull_before_task(root, config)
        except (Exception, SystemExit):
            pass
        goal = _plan_goal(root, run_id)
        project_keys = project_key_aliases(Path(workspace_path), goal=goal)
        key = project_keys[0]
        retrieval = cfg.get("retrieval", {}) if isinstance(cfg.get("retrieval"), dict) else {}
        max_entries = int(retrieval.get("max_entries", 5) or 5)
        max_chars = int(retrieval.get("max_chars", 4000) or 4000)
        terms = _terms(task.get("title", ""), task.get("description", ""))
        entries = retrieve_for_task(root, config, project_key=key, project_keys=project_keys, terms=terms, max_entries=max_entries)
        return format_injection(entries, max_chars=max_chars)
    except (Exception, SystemExit):  # injection must never break prompt assembly
        return ""


def _plan_goal(root: Path, run_id: str) -> str | None:
    try:
        from aha_cli.store.runs import require_plan

        return require_plan(root, run_id).get("goal")
    except (Exception, SystemExit):  # require_plan raises SystemExit when absent
        return None
