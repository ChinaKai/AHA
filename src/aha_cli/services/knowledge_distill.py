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
    NAVIGATION_FLOWS_DIR,
    NAVIGATION_MODULES_DIR,
    NAVIGATION_SLUG,
    auto_commit_message_for,
    enqueue_candidate,
    entry_path_for,
    init_knowledge_base,
    knowledge_config,
    normalize_entry_slug,
    project_key,
    read_entry,
    slugify,
    type_for_kind,
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
    workspace_path: str | None = None,
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
        "workspace_path": workspace_path,
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
    "稳定结果",
    "关键结论",
    "可复用经验",
    "有效解法",
)

_REUSABLE_SUMMARY_KEYWORDS = (
    "可复用",
    "通用",
    "稳定规则",
    "约定",
    "惯例",
    "模式",
    "playbook",
    "reusable",
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
            current_useful = any(keyword in line for keyword in _USEFUL_SECTION_KEYWORDS)
    if current and current_useful:
        sections.append(current)
    useful = "\n\n".join("\n".join(section).strip() for section in sections if "\n".join(section).strip())
    # When the report is structured but every section is pure task narrative
    # (任务轮次/变更文件/验证/剩余风险 …) there is nothing reusable to keep, so we
    # return "" rather than dumping the whole report. The value gate in
    # ``heuristic_solution_candidate`` turns that empty result into "no candidate".
    return useful


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
    """Deterministic fallback distiller: at most one solution candidate.

    It does not invent content; it reorganizes what the task already recorded
    into the solution schema so a reviewer can confirm/trim it. Crucially it is
    a *gate*, not an unconditional producer: when the task left no reusable
    signal (no record_task_update summary, no changed files, no verification,
    and no genuinely useful report section — e.g. a pure Q&A or trivial task)
    it returns ``[]`` so the KB is not polluted with task narrative. The real
    quality path is the agent-emitted sidecar; the heuristic only salvages a
    candidate when there is concrete substance to salvage.
    """
    summary = (context.get("summary") or "").strip()
    source_body = _clean_markdown(context.get("final_body") or "")
    useful_body = _extract_useful_markdown(source_body)

    changed = context.get("changed_files") or []
    verification = context.get("verification") or []
    risks = context.get("risks") or []

    # Value gate: do not turn ordinary bug-fix/task-closeout facts into
    # long-lived knowledge. Fallback distillation only salvages content that the
    # final/report explicitly framed as reusable, or whose summary says it is a
    # reusable rule/playbook. Rich sidecars remain the preferred path.
    reusable_summary = any(keyword.lower() in summary.lower() for keyword in _REUSABLE_SUMMARY_KEYWORDS)
    if not (useful_body or reusable_summary):
        return []

    title_basis = summary or context.get("task_title") or useful_body or "Untitled solution"
    title = title_basis.strip().splitlines()[0][:120] if title_basis.strip() else "Untitled solution"

    # Effective solution prefers the explicit summary, but falls back to useful
    # sections from the source report so stored knowledge keeps structure and is
    # not reduced to a short, lossy prefix.
    solution = summary or useful_body or "(见任务 final/report，待人工提炼)"

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
    if raw in {"navigation", "nav", "map"}:
        return "navigation"
    return "solutions"


def _navigation_slug(candidate: dict) -> str:
    raw_slug = str(candidate.get("slug") or candidate.get("nav_path") or candidate.get("doc_path") or "").strip()
    if raw_slug:
        return raw_slug
    module = str(candidate.get("module") or "").strip()
    if module:
        return f"{NAVIGATION_MODULES_DIR}/{slugify(module)}"
    flow = str(candidate.get("flow") or candidate.get("workflow") or "").strip()
    if flow:
        return f"{NAVIGATION_FLOWS_DIR}/{slugify(flow)}"
    return NAVIGATION_SLUG


def _navigation_role_for_slug(slug: str) -> str:
    if slug == NAVIGATION_SLUG:
        return "index"
    if slug.startswith(f"{NAVIGATION_MODULES_DIR}/"):
        return "module"
    if slug.startswith(f"{NAVIGATION_FLOWS_DIR}/"):
        return "flow"
    return "navigation"


def _navigation_parent_slug(slug: str) -> str | None:
    slug = normalize_entry_slug(str(slug or "").strip())
    if not slug or slug == NAVIGATION_SLUG:
        return None
    parts = slug.split("/")
    if len(parts) <= 2 and parts[0] in {NAVIGATION_MODULES_DIR, NAVIGATION_FLOWS_DIR}:
        return NAVIGATION_SLUG
    if len(parts) > 2 and parts[0] in {NAVIGATION_MODULES_DIR, NAVIGATION_FLOWS_DIR}:
        return "/".join(parts[:-1])
    return NAVIGATION_SLUG


def _project_nav_config(config: dict | None) -> dict:
    cfg = knowledge_config(config)
    nav = cfg.get("project_nav") if isinstance(cfg.get("project_nav"), dict) else {}
    return {
        "enabled": bool(nav.get("enabled", True)),
        "maintain_during_task": bool(nav.get("maintain_during_task", True)),
    }


def project_nav_enabled(config: dict | None) -> bool:
    """Whether project navigation candidates should be produced/reviewed."""
    nav = _project_nav_config(config)
    return bool(nav.get("enabled", True))


def _is_navigation_candidate(candidate: dict) -> bool:
    return str(candidate.get("kind") or "").strip().lower() == "navigation"


def _navigation_candidate_brief(candidate: dict) -> dict:
    meta = candidate.get("meta") if isinstance(candidate.get("meta"), dict) else {}
    item = {
        "title": candidate.get("title"),
        "slug": candidate.get("slug"),
        "scope": candidate.get("scope", "project"),
        "project_key": candidate.get("project_key"),
        "navigation_role": meta.get("navigation_role"),
        "update_mode": meta.get("update_mode"),
        "action": candidate.get("action"),
        "updates_entry_id": candidate.get("updates_entry_id"),
    }
    related_files = meta.get("related_files") or candidate.get("related_files")
    if related_files:
        item["related_files"] = related_files
    reason = meta.get("navigation_reason")
    if reason:
        item["navigation_reason"] = reason
    return {key: value for key, value in item.items() if value not in (None, "", [])}


def navigation_distill_summary(
    candidates: list[dict],
    *,
    gate: str | None = None,
    enqueued: list[str] | None = None,
    written: list[str] | None = None,
    skipped: dict | None = None,
) -> dict:
    """Compact, event/log-friendly summary for navigation deltas."""
    items: list[dict] = []
    enqueued = enqueued or []
    written = written or []
    for index, candidate in enumerate(candidates or []):
        if not _is_navigation_candidate(candidate):
            continue
        item = _navigation_candidate_brief(candidate)
        if index < len(enqueued):
            path = Path(enqueued[index])
            item["candidate_id"] = path.stem
            item["pending_path"] = str(path)
        if index < len(written):
            item["path"] = written[index]
        items.append(item)
    summary = {
        "candidates": len(items),
        "slugs": [str(item.get("slug")) for item in items if item.get("slug")],
        "items": items,
    }
    if gate:
        summary["gate"] = gate
    if skipped:
        summary["skipped"] = skipped
    return summary


def filter_project_nav_candidates(
    root: Path | None,
    config: dict | None,
    candidates: list[dict],
    context: dict | None = None,
    *,
    allow_bootstrap: bool = False,
) -> tuple[list[dict], dict | None]:
    if project_nav_enabled(config):
        if allow_bootstrap:
            return candidates, None
        skipped_missing_index: list[dict] = []
        kept: list[dict] = []
        for candidate in candidates:
            if not _is_navigation_candidate(candidate):
                kept.append(candidate)
                continue
            project_key_value = candidate.get("project_key") or (context or {}).get("project_key")
            has_index = bool(
                root is not None
                and project_key_value
                and entry_path_for(root, config, "project", "navigation", str(project_key_value), NAVIGATION_SLUG)
            )
            if has_index:
                kept.append(candidate)
            else:
                skipped_missing_index.append(candidate)
        if not skipped_missing_index:
            return candidates, None
        return kept, {
            "reason": "project navigation index missing",
            "candidates": len(skipped_missing_index),
            "slugs": [str(candidate.get("slug") or "") for candidate in skipped_missing_index if candidate.get("slug")],
        }

    skipped = [candidate for candidate in candidates if _is_navigation_candidate(candidate)]
    if not skipped:
        return candidates, None
    kept = [candidate for candidate in candidates if not _is_navigation_candidate(candidate)]
    return kept, {
        "reason": "project navigation disabled",
        "candidates": len(skipped),
        "slugs": [str(candidate.get("slug") or "") for candidate in skipped if candidate.get("slug")],
    }


def navigation_delta_event_payload(
    result: dict | None,
    *,
    source_type: str,
    task_id: str | None = None,
    memo_id: str | None = None,
    note_id: str | None = None,
) -> dict | None:
    if not isinstance(result, dict):
        return None
    navigation = result.get("navigation") if isinstance(result.get("navigation"), dict) else {}
    if not navigation or (not navigation.get("candidates") and not navigation.get("skipped")):
        return None
    payload = {
        "source_type": source_type,
        "navigation": navigation,
        "gate": result.get("gate"),
    }
    if task_id:
        payload["task_id"] = task_id
    if memo_id:
        payload["memo_id"] = memo_id
    if note_id:
        payload["note_id"] = note_id
    return payload


def _navigation_link_label(slug: str, fallback: str = "") -> str:
    if fallback:
        return fallback
    tail = str(slug or "").strip("/").rsplit("/", 1)[-1]
    return tail.replace("-", " ") or "navigation"


def _navigation_child_href(child_slug: str) -> str:
    return f"{normalize_entry_slug(child_slug)}.md"


def _navigation_parent_title(parent_slug: str, project_key_value: str | None) -> str:
    if parent_slug == NAVIGATION_SLUG:
        return f"{project_key_value or '项目'} 导航入口"
    label = _navigation_link_label(parent_slug)
    role = _navigation_role_for_slug(parent_slug)
    return f"{label} {'流程' if role == 'flow' else '模块'}导航"


def _navigation_section_for(parent_slug: str, child_slug: str) -> str:
    if parent_slug == NAVIGATION_SLUG:
        return "### 入口 / 关键流程" if child_slug.startswith(f"{NAVIGATION_FLOWS_DIR}/") else "### 模块索引"
    return "## 下级入口"


def _append_navigation_link(body: str, *, section: str, label: str, href: str) -> str:
    body = (body or "").rstrip()
    if href in body:
        return body + "\n"
    line = f"- [{label}]({href})"
    lines = body.splitlines() if body else []
    try:
        section_index = next(i for i, item in enumerate(lines) if item.strip() == section)
    except StopIteration:
        return (body + ("\n\n" if body else "") + f"{section}\n{line}\n").lstrip()

    insert_at = len(lines)
    for idx in range(section_index + 1, len(lines)):
        if lines[idx].lstrip().startswith("#"):
            insert_at = idx
            break
    while insert_at > section_index + 1 and not lines[insert_at - 1].strip():
        insert_at -= 1
    lines.insert(insert_at, line)
    return "\n".join(lines).rstrip() + "\n"


def _new_navigation_parent_body(parent_slug: str, child_slug: str, child_title: str) -> str:
    href = _navigation_child_href(child_slug)
    label = _navigation_link_label(child_slug, child_title)
    if parent_slug == NAVIGATION_SLUG:
        section = _navigation_section_for(parent_slug, child_slug)
        module_lines = f"- [{label}]({href})" if section == "### 模块索引" else "-"
        flow_lines = f"- [{label}]({href})" if section == "### 入口 / 关键流程" else "-"
        return "\n".join([
            "## Project README",
            "- 待补充：项目目标、技术栈、运行/测试方式、代码组织约定和 agent 开工前注意事项。",
            "",
            "## 使用规则",
            "- 本入口只列第一层模块/流程；更深层入口由对应父文档维护。",
            "- 新增子文档时只补直接父入口，避免全量展开。",
            "",
            "## Project Map",
            "",
            "### 模块索引",
            module_lines,
            "",
            "### 入口 / 关键流程",
            flow_lines,
        ]).strip() + "\n"
    return "\n".join([
        f"# {_navigation_link_label(parent_slug)}",
        "",
        "## 模块职责" if _navigation_role_for_slug(parent_slug) == "module" else "## 流程职责",
        "-",
        "",
        "## 关键源文件",
        "-",
        "",
        "## 下级入口",
        f"- [{label}]({href})",
        "",
        "## 修改注意",
        "- 本文只维护直接子入口；更深层入口由对应子文档维护。",
    ]).strip() + "\n"


def _bootstrap_navigation_index_body(workspace_path: str | None, child_slug: str, child_title: str) -> str:
    # Parent backfill is not the same as full project bootstrap: only link the
    # direct child in the current candidate batch, otherwise scanned-but-not-
    # queued modules become dead links in navigation/index.md.
    return _new_navigation_parent_body(NAVIGATION_SLUG, child_slug, child_title)


def _navigation_meta(role: str, source: dict | None, *, update_mode: str = "incremental") -> dict:
    return {
        "type": "navigation",
        "outcome": "success",
        "confidence": 0.55,
        "tags": ["navigation", role],
        "related_files": [],
        "distilled_by": "parent-link",
        "update_mode": update_mode,
        "navigation_role": role,
        **_sidecar_source_meta(source),
    }


def _navigation_parent_candidate(
    root: Path,
    config: dict | None,
    *,
    scope: str,
    project_key_value: str | None,
    parent_slug: str,
    child_slug: str,
    child_title: str,
    source: dict | None,
    workspace_path: str | None = None,
) -> dict | None:
    parent_path = entry_path_for(root, config, scope, "navigation", project_key_value, parent_slug)
    href = _navigation_child_href(child_slug)
    label = _navigation_link_label(child_slug, child_title)
    section = _navigation_section_for(parent_slug, child_slug)
    if parent_path:
        try:
            existing = read_entry(parent_path)
        except (OSError, ValueError):
            existing = None
        if existing:
            body = existing.get("body") or ""
            if href in body:
                return None
            meta = dict(existing.get("meta") or {})
            meta.update(_navigation_meta(_navigation_role_for_slug(parent_slug), source))
            return {
                "kind": "navigation",
                "scope": scope,
                "project_key": project_key_value,
                "slug": parent_slug,
                "title": str(meta.get("title") or _navigation_parent_title(parent_slug, project_key_value)),
                "body": _append_navigation_link(body, section=section, label=label, href=href),
                "meta": meta,
                "source": source,
            }
    update_mode = "bootstrap" if parent_slug == NAVIGATION_SLUG else "incremental"
    return {
        "kind": "navigation",
        "scope": scope,
        "project_key": project_key_value,
        "slug": parent_slug,
        "title": _navigation_parent_title(parent_slug, project_key_value),
        "body": (
            _bootstrap_navigation_index_body(workspace_path, child_slug, child_title)
            if parent_slug == NAVIGATION_SLUG
            else _new_navigation_parent_body(parent_slug, child_slug, child_title)
        ),
        "meta": _navigation_meta(_navigation_role_for_slug(parent_slug), source, update_mode=update_mode),
        "source": source,
    }


def _ensure_navigation_parent_entries(
    root: Path,
    config: dict | None,
    candidates: list[dict],
    context: dict | None = None,
) -> list[dict]:
    """Add minimal parent navigation candidates for unreachable child docs."""
    by_slug: dict[tuple[str, str | None, str], dict] = {}
    ordered = list(candidates)
    for cand in ordered:
        if cand.get("kind") == "navigation" and cand.get("scope", "project") == "project" and cand.get("slug"):
            by_slug[(str(cand.get("scope") or "project"), cand.get("project_key"), normalize_entry_slug(cand["slug"]))] = cand

    index = 0
    workspace_path = str((context or {}).get("workspace_path") or "").strip() or None
    while index < len(ordered):
        cand = ordered[index]
        index += 1
        if cand.get("kind") != "navigation" or cand.get("scope", "project") != "project":
            continue
        child_slug = normalize_entry_slug(str(cand.get("slug") or ""))
        parent_slug = _navigation_parent_slug(child_slug)
        if not parent_slug:
            continue
        key = (str(cand.get("scope") or "project"), cand.get("project_key"), parent_slug)
        child_title = str(cand.get("title") or _navigation_link_label(child_slug))
        href = _navigation_child_href(child_slug)
        parent = by_slug.get(key)
        if parent:
            parent["body"] = _append_navigation_link(
                str(parent.get("body") or ""),
                section=_navigation_section_for(parent_slug, child_slug),
                label=_navigation_link_label(child_slug, child_title),
                href=href,
            )
            continue
        parent = _navigation_parent_candidate(
            root,
            config,
            scope=key[0],
            project_key_value=key[1],
            parent_slug=parent_slug,
            child_slug=child_slug,
            child_title=child_title,
            source=cand.get("source"),
            workspace_path=workspace_path,
        )
        if parent is None:
            continue
        by_slug[key] = parent
        ordered.append(parent)
    return ordered


def ensure_navigation_parent_entries(
    root: Path,
    config: dict | None,
    candidates: list[dict],
    context: dict | None = None,
) -> list[dict]:
    """Public wrapper used by non-final distill paths such as capture notes."""
    return _ensure_navigation_parent_entries(root, config, candidates, context)


def _sidecar_navigation_body(candidate: dict) -> str:
    slug = _navigation_slug(candidate)
    diagnostic_paths = "\n".join(
        f"- {item}"
        for item in _as_list(
            candidate.get("diagnostic_paths")
            or candidate.get("troubleshooting")
            or candidate.get("debug_paths")
            or candidate.get("diagnostics")
        )
    )
    if slug.startswith(f"{NAVIGATION_MODULES_DIR}/"):
        module = str(candidate.get("module") or candidate.get("title") or "").strip()
        role = str(candidate.get("role") or candidate.get("responsibility") or candidate.get("summary") or "").strip()
        files = "\n".join(f"- `{item}`" for item in _as_list(candidate.get("related_files") or candidate.get("files")))
        entry_points = "\n".join(f"- `{item}`" for item in _as_list(candidate.get("entry_points") or candidate.get("entries")))
        tests = "\n".join(f"- `{item}`" for item in _as_list(candidate.get("tests") or candidate.get("verification")))
        caveats = str(candidate.get("caveats") or candidate.get("notes") or candidate.get("invalid_when") or "").strip()
        parts = [
            f"# {module or '模块文档'}",
            "",
            "## 模块职责",
            role or "-",
            "",
            "## 关键源文件",
            files or "-",
            "",
            "## 入口 / 调用方",
            entry_points or "-",
            "",
            "## 常用排查路径",
            diagnostic_paths or "-",
            "",
            "## 修改注意",
            caveats or "- 修改职责、入口、关键文件或约束后同步更新本文。",
            "",
            "## 相关测试",
            tests or "-",
            "",
            "## 盲区 / 待补充",
            str(candidate.get("gaps") or candidate.get("todo") or "-").strip() or "-",
            "",
            "## 维护规则",
            "- 只在本模块职责、入口、关键文件、约束或盲区变化时更新本文。",
            "- 不要为无关任务全量重写模块文档；新增模块/流程时再补入口链接。",
        ]
        return "\n".join(parts).strip() + "\n"

    project_readme = str(
        candidate.get("project_readme")
        or candidate.get("readme")
        or candidate.get("overview")
        or candidate.get("summary")
        or ""
    ).strip()
    architecture = str(candidate.get("architecture") or "").strip()
    modules = candidate.get("modules")
    module_lines: list[str] = []
    if isinstance(modules, list):
        for mod in modules:
            if isinstance(mod, dict):
                name = str(mod.get("name") or mod.get("module") or "").strip()
                role = str(mod.get("role") or mod.get("desc") or "").strip()
                files = ", ".join(_as_list(mod.get("files") or mod.get("entry")))
                line = f"- {name or '?'}"
                if role:
                    line += f" — {role}"
                if files:
                    line += f" (`{files}`)"
                module_lines.append(line)
            elif str(mod).strip():
                module_lines.append(f"- {str(mod).strip()}")
    entry_points = "\n".join(f"- `{item}`" for item in _as_list(candidate.get("entry_points") or candidate.get("entries")))
    gaps = str(candidate.get("gaps") or candidate.get("todo") or "").strip()
    readme_lines = [project_readme or "-"]
    if architecture:
        readme_lines.extend(["", "### 架构 / 组织", architecture])
    parts = [
        "## Project README",
        "\n".join(readme_lines),
        "",
        "## Project Map",
        "",
        "### 模块索引",
        "\n".join(module_lines) if module_lines else "-",
        "",
        "### 入口 / 关键流程",
        entry_points or "-",
        "",
        "## 常用排查路径",
        diagnostic_paths or "-",
        "",
        "## 盲区 / 待补充",
        gaps or "-",
        "",
        "## 使用规则",
        "- 开工先读本入口，再按任务命中的模块/流程链接读取少量文档；不要把整个 navigation 全量读入。",
        "- 收尾只更新本次真实影响的 `modules/*`、`flows/*` 或入口链接；普通任务不要全量重写项目导航。",
    ]
    return "\n".join(parts).strip() + "\n"


def _sidecar_body(candidate: dict, kind: str) -> str:
    body = str(candidate.get("body") or "").strip()
    if body:
        return body.rstrip() + "\n"
    if kind == "navigation":
        return _sidecar_navigation_body(candidate)
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
        raw_scope = str(raw.get("scope") or "").strip().lower()
        if kind == "wiki":
            if raw_scope == "project":
                if context.get("project_key"):
                    kind = "navigation"
                    raw.setdefault("module", raw.get("module") or raw.get("title"))
                else:
                    raw_scope = "personal"
            else:
                raw_scope = raw_scope if raw_scope == "personal" else "general"
        body = _sidecar_body(raw, kind)
        if not title or not body.strip():
            continue
        tags = _as_list(raw.get("tags"))
        related_files = _as_list(raw.get("related_files") or raw.get("files"))
        meta = {
            "type": type_for_kind(kind),
            "outcome": str(raw.get("outcome") or "success"),
            "confidence": raw.get("confidence", 0.7),
            "tags": tags,
            "related_files": related_files,
            "distilled_by": "sidecar",
            **_sidecar_source_meta(source),
        }
        if kind == "navigation":
            nav_slug = _navigation_slug(raw)
            meta["update_mode"] = str(raw.get("update_mode") or "incremental")
            meta["navigation_role"] = _navigation_role_for_slug(nav_slug)
            meta.setdefault("tags", [])
            for tag in ("navigation", meta["navigation_role"]):
                if tag not in meta["tags"]:
                    meta["tags"].append(tag)
            navigation_reason = str(raw.get("navigation_reason") or raw.get("reason") or "").strip()
            if navigation_reason:
                meta["navigation_reason"] = navigation_reason
            diagnostic_meta = _as_list(
                raw.get("diagnostic_paths")
                or raw.get("troubleshooting")
                or raw.get("debug_paths")
                or raw.get("diagnostics")
            )
            if diagnostic_meta:
                meta["diagnostic_paths"] = diagnostic_meta
        invalid_when = str(raw.get("invalid_when") or "").strip()
        if invalid_when:
            meta["invalid_when"] = invalid_when
            if "失效条件" not in body and "invalid" not in body.lower():
                body = body.rstrip() + f"\n\n## 失效条件 / 适用边界\n{invalid_when}\n"
        scope = raw_scope if raw_scope in ("general", "personal", "project") else "project"
        if kind == "wiki" and scope == "project":
            scope = "general"
        # Only project-scoped knowledge carries a project_key. General (shared)
        # and personal (user scratch) knowledge must never inherit the current
        # project's key, or they would be filed/retrieved as one project's
        # private knowledge instead of their own scope.
        project_key_value = (raw.get("project_key") or context.get("project_key")) if scope == "project" else None
        candidate = {
            "kind": kind,
            "scope": scope,
            "project_key": project_key_value,
            "title": title,
            "body": body,
            "meta": meta,
            "source": source,
        }
        if kind == "navigation":
            candidate["slug"] = _navigation_slug(raw)
        normalized.append(candidate)
    return normalized


def general_tutorial_candidate(
    *,
    title: str,
    body: str,
    kind: str = "wiki",
    tags: list[str] | None = None,
    related_files: list[str] | None = None,
    source: dict | None = None,
) -> dict:
    """Build a cross-project (general scope) tutorial/doc candidate.

    This is the manual-authoring counterpart to the distillers: it produces a
    candidate that is *not* tied to any project_key, so it lands in the shared
    ``general/`` scope and is only injected when relevant to a task.
    """
    kind = "solutions" if str(kind).strip().lower() == "solutions" else "wiki"
    return {
        "kind": kind,
        "scope": "general",
        "project_key": None,
        "title": str(title or "").strip(),
        "body": (str(body or "").strip() + "\n") if str(body or "").strip() else "",
        "meta": {
            "type": type_for_kind(kind),
            "outcome": "success",
            "confidence": 0.5,
            "tags": [t for t in (tags or []) if str(t).strip()],
            "related_files": [f for f in (related_files or []) if str(f).strip()],
            "distilled_by": "manual",
        },
        "source": source or {"source_type": "manual_tutorial"},
    }


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
    candidates, skipped_navigation = filter_project_nav_candidates(
        root,
        config,
        candidates,
        context,
        allow_bootstrap=bool((context or {}).get("allow_navigation_bootstrap")),
    )
    if not candidates:
        result = {"ok": True, "candidates": 0, "gate": gate}
        if skipped_navigation:
            result["navigation"] = navigation_distill_summary([], gate=gate, skipped=skipped_navigation)
        return result

    # Ensure the KB skeleton (index, README, and the .gitignore that excludes
    # .pending/) exists before writing anything — including the first time this
    # is triggered by finalize without a prior `aha kb init`.
    init_knowledge_base(root, config)
    candidates = _ensure_navigation_parent_entries(root, config, candidates, context)
    has_navigation_candidates = any(_is_navigation_candidate(candidate) for candidate in candidates)
    from aha_cli.services.knowledge_navigation import validate_navigation_candidates

    navigation_validation = validate_navigation_candidates(root, config, candidates)
    if not navigation_validation["ok"]:
        navigation = navigation_distill_summary(candidates, gate=gate, skipped=skipped_navigation)
        navigation["validation"] = navigation_validation
        return {
            "ok": False,
            "error": "navigation validation failed",
            "gate": gate,
            "candidates": 0,
            "navigation": navigation,
            "validation": navigation_validation,
        }

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
                slug=cand.get("slug"),
            )
            written.append(str(path))
        result = {
            "ok": True,
            "gate": "auto",
            "written": written,
            "candidates": len(written),
        }
        if has_navigation_candidates or skipped_navigation:
            result["navigation"] = navigation_distill_summary(
                candidates, gate="auto", written=written, skipped=skipped_navigation
            )
            result["navigation"]["validation"] = navigation_validation
            result["validation"] = navigation_validation
        # Auto-commit (and optionally push) the freshly written entries.
        from aha_cli.services.knowledge_git import auto_commit_after_change

        result["git"] = auto_commit_after_change(
            root, auto_commit_message_for(candidates), config
        )
        return result

    # Default: manual gate -> queue for review, never touches the tracked tree.
    enqueued = [str(enqueue_candidate(root, config, cand)) for cand in candidates]
    result = {
        "ok": True,
        "gate": "manual",
        "enqueued": enqueued,
        "candidates": len(enqueued),
    }
    if has_navigation_candidates or skipped_navigation:
        result["navigation"] = navigation_distill_summary(
            candidates, gate="manual", enqueued=enqueued, skipped=skipped_navigation
        )
        result["navigation"]["validation"] = navigation_validation
        result["validation"] = navigation_validation
    return result


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
            workspace_path=workspace_path,
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
            workspace_path=workspace_path,
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
