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

from aha_cli.services.prompt_templates import render_prompt_template
from aha_cli.store.knowledge import (
    NAVIGATION_SLUG,
    iter_all_entries,
    knowledge_config,
    knowledge_root,
    resolved_project_key_aliases,
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
    base = sum(1 for term in terms if term in haystack)
    # Backlink weighting: entries referenced by more others are treated as more
    # trusted/central (Obsidian graph intuition). A small multiplier avoids
    # overwhelming term relevance.
    backlinks = meta.get("backlinks") if isinstance(meta.get("backlinks"), list) else []
    if base > 0 and backlinks:
        return base * (1 + 0.1 * min(len(backlinks), 10))
    return base


def _wikilink_neighbors(entry: dict) -> list[str]:
    """Return the canonical slugs of entries this entry links to (out-links).

    ``meta.links`` is maintained by the wikilink index (Phase 2); reuse it for
    graph propagation without re-parsing bodies.
    """
    links = entry.get("meta", {}).get("links") if isinstance(entry.get("meta", {}).get("links"), list) else []
    return [str(slug) for slug in links if slug]


def _wikilink_backlink_slugs(entry: dict) -> list[str]:
    """Return the canonical slugs of entries that link to this one (in-links)."""
    backlinks = entry.get("meta", {}).get("backlinks") if isinstance(entry.get("meta", {}).get("backlinks"), list) else []
    return [str(slug) for slug in backlinks if slug]


def _expand_by_wikilinks(entries: list[dict], all_entries: list[dict], *, max_expansion: int = 4) -> list[dict]:
    """Expand the ranked hit set by one wikilink hop.

    For each ranked entry, pull its out-links and in-links that exist in the KB
    and are not already in the result, up to ``max_expansion``. This lets a term
    hit bring related notes into the injected context (Obsidian graph spread).
    """
    by_slug = {str(e.get("meta", {}).get("slug") or ""): e for e in all_entries}
    seen = {str(e.get("meta", {}).get("slug") or "") for e in entries}
    expanded: list[dict] = []
    for entry in entries:
        for slug in [*_wikilink_neighbors(entry), *_wikilink_backlink_slugs(entry)]:
            if slug in seen:
                continue
            neighbor = by_slug.get(slug)
            if neighbor is None:
                continue
            neighbor_meta = neighbor.get("meta", {})
            if neighbor_meta.get("status") == "deprecated":
                continue
            seen.add(slug)
            expanded.append(neighbor)
            if len(expanded) >= max_expansion:
                return expanded
    return expanded


def _is_navigation_entry(entry: dict) -> bool:
    return entry.get("meta", {}).get("type") == "navigation"


def _is_task_worklog_entry(entry: dict) -> bool:
    return entry.get("meta", {}).get("type") == "task_worklog"


def _is_navigation_index(entry: dict) -> bool:
    meta = entry.get("meta", {})
    return meta.get("type") == "navigation" and meta.get("slug") == NAVIGATION_SLUG


def _is_navigation_detail(entry: dict) -> bool:
    meta = entry.get("meta", {})
    slug = str(meta.get("slug") or "")
    return meta.get("type") == "navigation" and slug != NAVIGATION_SLUG


def _project_nav_enabled(config: dict | None) -> bool:
    cfg = knowledge_config(config)
    project_nav = cfg.get("project_nav") if isinstance(cfg.get("project_nav"), dict) else {}
    return bool(project_nav.get("enabled", True))


def retrieve_for_task(
    root: Path,
    config: dict | None,
    *,
    project_key: str | None,
    project_keys: Sequence[str] | None = None,
    terms: list[str],
    max_entries: int = 5,
    include_navigation_details: bool = True,
) -> list[dict]:
    """Return the most relevant entries for a project, ranked by term overlap.

    Project-scoped entries rank above general ones. When nothing matches the
    terms, fall back to the project's most recently updated entries so a project
    that has knowledge always contributes something.
    """
    keys = [key for key in [project_key, *(project_keys or [])] if key]
    project_key_set = set(keys)
    include_project_nav = _project_nav_enabled(config)
    project_entries: list[dict] = []
    general_entries: list[dict] = []
    for entry in iter_all_entries(root, config):
        meta = entry.get("meta", {})
        if meta.get("status") == "deprecated":
            continue
        if _is_task_worklog_entry(entry):
            continue
        if meta.get("type") == "navigation" and not include_project_nav:
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

    # The navigation index is always relevant: it routes the agent to module
    # docs. In reference mode, navigation detail docs stay on disk and are found
    # by reading the index, so they should not consume prompt ref slots.
    nav_entries = [e for _, _, e in rank([e for e in project_entries if _is_navigation_index(e)])]
    rest_project = [e for e in project_entries if not _is_navigation_index(e)]
    rankable_project = [
        e for e in rest_project
        if include_navigation_details or not _is_navigation_detail(e)
    ]
    rankable_general = [
        e for e in general_entries
        if include_navigation_details or not _is_navigation_entry(e)
    ]

    ordered = [e for score, _, e in rank(rankable_project) if score > 0]
    ordered += [e for score, _, e in rank(rankable_general) if score > 0]
    if not ordered:
        # Fallback: non-navigation project knowledge by recency. Navigation
        # details remain on-demand: read them only when the task matches them.
        ordered = [e for _, _, e in rank([e for e in rankable_project if not _is_navigation_detail(e)])]
    # Wikilink propagation: when term hits exist, bring one-hop related notes
    # into the context (Obsidian graph spread) up to the entry budget.
    all_scoped = project_entries + general_entries
    if ordered and len(ordered) < max_entries:
        ordered += _expand_by_wikilinks(ordered, all_scoped, max_expansion=max_entries - len(ordered))
    ordered = nav_entries + [e for e in ordered if e not in nav_entries]
    return ordered[: max(0, max_entries)]


def _clip_to_budget(text: str, max_chars: int) -> str:
    if max_chars <= 0:
        return ""
    if len(text) <= max_chars:
        return text.strip()
    if max_chars <= 2:
        return text[:max_chars].strip()
    return (text[: max_chars - 2].rstrip() + " …").strip()


def _entry_kind(meta: dict) -> str:
    return str(meta.get("type") or "entry")


def _entry_path(entry: dict, kb_root: Path | None) -> str:
    raw = str(entry.get("path") or "").strip()
    if not raw:
        return ""
    path = Path(raw)
    if kb_root is not None:
        try:
            return path.relative_to(kb_root).as_posix()
        except ValueError:
            pass
    return raw


def _entry_tags(meta: dict) -> str:
    tags = meta.get("tags") or []
    if not isinstance(tags, list):
        return ""
    return ", ".join(str(tag).strip() for tag in tags if str(tag).strip())


def _entry_summary(entry: dict, *, summary_chars: int, include_navigation_body: bool = False) -> str:
    meta = entry.get("meta", {})
    kind = _entry_kind(meta)
    if kind == "navigation" and not include_navigation_body:
        return ""
    summary = str(meta.get("summary") or meta.get("description") or "").strip()
    if not summary:
        summary = " ".join((entry.get("body", "") or "").split())
    return _clip_to_budget(summary, max(0, summary_chars))


def _navigation_header(entries: list[dict]) -> list[str]:
    if not any(_is_navigation_entry(e) for e in entries):
        return []
    return [render_prompt_template("knowledge_navigation_header.md").rstrip("\n")]


def _navigation_reference_lines(entries: list[dict], kb_root: Path | None) -> list[str]:
    nav_index = next((entry for entry in entries if _is_navigation_index(entry)), None)
    if nav_index is None:
        return []
    path = _entry_path(nav_index, kb_root)
    lines = [render_prompt_template("knowledge_navigation_rule.md").rstrip("\n")]
    if path:
        lines.append(f"Project nav path: {path}")
    return lines


def _format_reference_injection(
    entries: list[dict],
    *,
    max_chars: int,
    kb_root: Path | None = None,
    project_key: str | None = None,
    summary_chars: int = 120,
) -> str:
    header = [
        "项目已知经验 (knowledge base):",
        "开工前先参考以下从过往任务沉淀的知识；如与现状冲突以代码为准。",
        "默认只注入引用和极短摘要；需要完整正文时按 path 主动读取对应知识文件。",
    ]
    if kb_root is not None:
        header.append(f"KB root: {kb_root}")
    if project_key:
        header.append(f"Project key: {project_key}")
    header.extend(_navigation_reference_lines(entries, kb_root))
    lines = header + [""]
    for entry in entries:
        if _is_navigation_entry(entry):
            continue
        meta = entry.get("meta", {})
        kind = _entry_kind(meta)
        title = str(meta.get("title") or "(untitled)")
        lines.append(f"- [{kind}] {title}")
        path = _entry_path(entry, kb_root)
        if path:
            lines.append(f"  path: {path}")
        slug = str(meta.get("slug") or "").strip()
        if slug:
            lines.append(f"  slug: {slug}")
        tags = _entry_tags(meta)
        if tags:
            lines.append(f"  tags: {tags}")
        updated_at = str(meta.get("updated_at") or "").strip()
        if updated_at:
            lines.append(f"  updated_at: {updated_at}")
        summary = _entry_summary(entry, summary_chars=summary_chars)
        if summary:
            lines.append(f"  summary: {summary}")
    return _clip_to_budget("\n".join(lines), max_chars)


def _format_excerpt_injection(entries: list[dict], *, max_chars: int) -> str:
    """Legacy formatter: include bounded body excerpts in the prompt."""
    header = [
        "项目已知经验 (knowledge base):",
        "开工前先参考以下从过往任务沉淀的知识；如与现状冲突以代码为准。",
        *_navigation_header(entries),
    ]
    out = "\n".join(header + [""])
    for entry in entries:
        meta = entry.get("meta", {})
        kind = _entry_kind(meta)
        title = meta.get("title", "(untitled)")
        head_line = f"\n- [{kind}] {title}"
        if len(head_line) > max_chars - len(out):
            break  # no room for even this entry's title line
        block = head_line
        excerpt = " ".join((entry.get("body", "") or "").split())
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
    return _clip_to_budget(out, max_chars)


def format_injection(
    entries: list[dict],
    *,
    max_chars: int = 4000,
    mode: str = "references",
    kb_root: Path | None = None,
    project_key: str | None = None,
    summary_chars: int = 120,
) -> str:
    """Render the injection block under a HARD total character budget.

    The default mode is reference-first: paths, metadata, and tiny summaries are
    injected, while complete bodies remain on disk for the agent to read on
    demand. ``mode="excerpts"`` preserves the previous bounded body-excerpt
    behavior for compatibility.
    """
    if not entries:
        return ""
    if _is_excerpt_mode(mode):
        return _format_excerpt_injection(entries, max_chars=max_chars)
    return _format_reference_injection(
        entries,
        max_chars=max_chars,
        kb_root=kb_root,
        project_key=project_key,
        summary_chars=summary_chars,
    )


def _is_excerpt_mode(mode: str) -> bool:
    return (mode or "references").strip().lower() in {"excerpt", "excerpts", "body"}


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
        project_keys = resolved_project_key_aliases(
            root, config, Path(workspace_path), goal=goal
        )
        key = project_keys[0]
        retrieval = cfg.get("retrieval", {}) if isinstance(cfg.get("retrieval"), dict) else {}
        max_entries = int(retrieval.get("max_entries", 5) or 5)
        max_chars = int(retrieval.get("max_chars", 4000) or 4000)
        inject_mode = str(retrieval.get("inject_mode") or retrieval.get("mode") or "references")
        summary_chars = int(retrieval.get("summary_chars", 120) or 120)
        terms = _terms(task.get("title", ""), task.get("description", ""))
        entries = retrieve_for_task(
            root,
            config,
            project_key=key,
            project_keys=project_keys,
            terms=terms,
            max_entries=max_entries,
            include_navigation_details=_is_excerpt_mode(inject_mode),
        )
        return format_injection(
            entries,
            max_chars=max_chars,
            mode=inject_mode,
            kb_root=knowledge_root(root, config),
            project_key=key,
            summary_chars=summary_chars,
        )
    except (Exception, SystemExit):  # injection must never break prompt assembly
        return ""


def _plan_goal(root: Path, run_id: str) -> str | None:
    try:
        from aha_cli.store.runs import require_plan

        return require_plan(root, run_id).get("goal")
    except (Exception, SystemExit):  # require_plan raises SystemExit when absent
        return None
