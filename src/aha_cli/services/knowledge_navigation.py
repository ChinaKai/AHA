"""Project navigation ("memory palace") generation.

Project navigation is a small entry point plus on-demand module docs. The entry
point is a generated project briefing, similar to an agent `/init` document,
followed by a first-level project map. The module document tells an agent which
source files, entry points, tests, and caveats matter for that module. This
keeps prompts small while preserving a durable map for agents that need to
modify a focused part of the codebase.

This module provides a deterministic, dependency-free *scan* that compresses a
workspace into evidence for an agent. AHA validates and stores the agent's
navigation draft, but does not invent the final navigation content from fixed
rules.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from pathlib import Path

from aha_cli.domain.models import utc_now
from aha_cli.services.prompt_templates import render_prompt_template
from aha_cli.store.knowledge import (
    NAVIGATION_MODULES_DIR,
    NAVIGATION_FLOWS_DIR,
    NAVIGATION_SLUG,
    entry_path_for,
    knowledge_config,
    normalize_entry_slug,
    project_key,
    slugify,
)

# Directories that are never project modules worth mapping.
_IGNORE_DIRS = {
    ".git", ".hg", ".svn", "__pycache__", ".pytest_cache", ".mypy_cache",
    ".ruff_cache", "node_modules", ".venv", "venv", "env", ".aha", ".idea",
    ".vscode", "dist", "build", ".eggs", "htmlcov", ".tox", "site-packages",
}
_DOCSTRING_RE = re.compile(r'^\s*(?:[rRbBuU]{0,2})("""|\'\'\')(.*?)\1', re.DOTALL)
_NAVIGATION_LINK_RE = re.compile(r"!?\[[^\]]*\]\(([^)]*)\)")
_JSON_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)```", re.IGNORECASE | re.DOTALL)
_NAVIGATION_ROOT_DIRS = {NAVIGATION_MODULES_DIR, NAVIGATION_FLOWS_DIR}
NavigationAgent = Callable[[dict], str]


class NavigationAgentError(RuntimeError):
    """Raised when the project navigation agent cannot produce usable output."""


def _is_module_dir(path: Path) -> bool:
    name = path.name
    if not path.is_dir() or name in _IGNORE_DIRS or name.startswith("."):
        return False
    return not (name.endswith(".egg-info") or name.endswith(".dist-info"))


_MD_LINK_RE = re.compile(r"!?\[[^\]]*\]\([^)]*\)")


def _is_chrome_line(line: str) -> bool:
    """Whether a README line is chrome (badges / language switcher / link bar).

    Such lines are markdown links separated only by ``|`` / whitespace and carry
    no prose, so they should not seed the project overview.
    """
    if line.startswith("![") or line.startswith("[!"):  # badges
        return True
    stripped = _MD_LINK_RE.sub("", line).replace("|", "").strip()
    return bool(_MD_LINK_RE.search(line)) and not stripped


def _first_paragraph(text: str, *, limit: int = 400) -> str:
    """First non-empty, non-heading paragraph of a README, collapsed and bounded."""
    paragraph: list[str] = []
    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line:
            if paragraph:
                break
            continue
        if line.startswith("#") or set(line) <= {"=", "-"}:  # heading / underline
            continue
        if _is_chrome_line(line):  # badges / language switcher / link bar
            continue
        paragraph.append(line)
    text = " ".join(paragraph).strip()
    return (text[:limit].rstrip() + " …") if len(text) > limit else text


def _module_docstring(pkg_dir: Path) -> str:
    """One-line summary from a package's ``__init__.py`` docstring, if any."""
    init = pkg_dir / "__init__.py"
    if not init.is_file():
        return ""
    try:
        head = init.read_text(encoding="utf-8", errors="replace")[:2000]
    except OSError:
        return ""
    match = _DOCSTRING_RE.match(head)
    if not match:
        return ""
    first_line = match.group(2).strip().splitlines()[0].strip() if match.group(2).strip() else ""
    return first_line[:120]


def _overview(workspace: Path) -> str:
    for name in ("README.md", "README.rst", "README.txt", "README"):
        readme = workspace / name
        if readme.is_file():
            try:
                return _first_paragraph(readme.read_text(encoding="utf-8", errors="replace"))
            except OSError:
                continue
    return ""


def _candidate_module_dirs(workspace: Path) -> list[Path]:
    """Top-level dirs, descending one level into a ``src/`` layout package root."""
    roots: list[Path] = []
    src = workspace / "src"
    if src.is_dir():
        # src-layout: the interesting modules are the package's sub-packages.
        for pkg in (p for p in sorted(src.iterdir()) if _is_module_dir(p)):
            subs = [p for p in sorted(pkg.iterdir()) if _is_module_dir(p)]
            roots.extend(subs or [pkg])
    if not roots:
        roots = [p for p in sorted(workspace.iterdir()) if _is_module_dir(p)]
    return roots


def _entry_points(workspace: Path) -> list[str]:
    points: list[str] = []
    pyproject = workspace / "pyproject.toml"
    if pyproject.is_file():
        try:
            text = pyproject.read_text(encoding="utf-8", errors="replace")
        except OSError:
            text = ""
        in_scripts = False
        for raw in text.splitlines():
            line = raw.strip()
            if line.startswith("["):
                in_scripts = line.replace(" ", "").lower() == "[project.scripts]"
                continue
            if in_scripts and "=" in line:
                name, _, target = line.partition("=")
                points.append(f"{name.strip()} = {target.strip().strip(chr(34))}")
    for rel in ("__main__.py", "main.py", "src/__main__.py", "manage.py"):
        if (workspace / rel).is_file():
            points.append(rel)
    return points


def scan_workspace(workspace_path: str | Path) -> dict:
    """Deterministically extract a project navigation skeleton from a workspace tree."""
    workspace = Path(workspace_path).expanduser()
    modules: list[dict] = []
    for mod in _candidate_module_dirs(workspace):
        rel = mod.relative_to(workspace).as_posix()
        modules.append({
            "name": mod.name,
            "role": _module_docstring(mod) or "(待补充职责)",
            "files": rel,
        })
    return {
        "project_name": workspace.name,
        "overview": _overview(workspace),
        "modules": modules,
        "entry_points": _entry_points(workspace),
    }


def _module_doc_slug(name: str) -> str:
    return f"{NAVIGATION_MODULES_DIR}/{slugify(name)}"


def render_navigation_body(scan: dict, *, link_modules: bool = True) -> str:
    """Render a scan dict into the navigation index body (markdown)."""
    overview = (scan.get("overview") or "").strip()
    module_lines = []
    for mod in scan.get("modules") or []:
        name = str(mod.get("name") or "?")
        role = str(mod.get("role") or "").strip()
        files = str(mod.get("files") or "").strip()
        doc = f"{_module_doc_slug(name)}.md"
        line = f"- [{name}]({doc})" if link_modules else f"- {name}"
        if role:
            line += f" — {role}"
        if files:
            line += f" (`{files}`)"
        module_lines.append(line)
    entry_lines = [f"- `{item}`" for item in (scan.get("entry_points") or [])]
    parts = [
        "## 项目介绍",
        overview or "(待补充：项目目标、技术栈、运行/测试方式、代码组织约定和 agent 开工前注意事项)",
        "",
        "## 如何编译 / 使用",
        "- (待补充：安装、运行、构建、测试或常用调试命令)",
        "",
        "## 注意事项",
        "- 本入口是首层路由：开工先读本入口，再按任务命中的模块/流程链接读取少量文档。",
        "- 不要把整个 navigation 全量读入；优先读取相关 nav 文档列出的关键文件。",
        "- 收尾只更新本次真实影响的子文档；如果子文档没有直接父入口，只补直接父入口链接。",
        "",
        "## 编码规范",
        "- (待补充：项目内命名、测试、目录、生成物、协议或 UI 约定)",
        "",
        "## 项目结构 / 核心 Nav",
        "",
        "### 模块索引",
        "\n".join(module_lines) if module_lines else "- (未发现可索引模块)",
        "",
        "### 入口 / 关键流程",
        "\n".join(entry_lines) if entry_lines else "- (暂无)",
    ]
    return "\n".join(parts).strip() + "\n"


def render_module_navigation_body(project_name: str, module: dict) -> str:
    """Render a module navigation doc skeleton."""
    name = str(module.get("name") or "?")
    role = str(module.get("role") or "").strip()
    files = str(module.get("files") or "").strip()
    parts = [
        f"# {project_name} / {name} 模块",
        "",
        "## 模块职责",
        role or "(待补充：该模块负责什么，边界是什么)",
        "",
        "## 关键源文件",
        f"- `{files}`" if files else "- (待补充)",
        "",
        "## 修改注意",
        "- 只在本模块职责、入口、关键文件、约束或盲区变化时更新本文。",
        "- 本文只维护一层子入口；新增更深层子文档时只补本文的直接链接。",
        "- 不要为无关 bug fix 全量重写模块文档；新增模块/流程时再补直接父入口链接。",
    ]
    return "\n".join(parts).strip() + "\n"


def build_navigation_candidate(
    workspace_path: str | Path,
    project_key_value: str,
    *,
    source: dict | None = None,
) -> dict:
    """Build the navigation index candidate (pending-queue shape) from a scan."""
    scan = scan_workspace(workspace_path)
    title = f"{scan.get('project_name') or '项目'} 导航入口"
    related = [m["files"] for m in scan.get("modules") or [] if m.get("files")]
    return {
        "kind": "navigation",
        "scope": "project",
        "project_key": project_key_value,
        "slug": NAVIGATION_SLUG,
        "title": title,
        "body": render_navigation_body(scan, link_modules=False),
        "meta": {
            "type": "navigation",
            "outcome": "success",
            "confidence": 0.4,  # scan skeleton, unreviewed
            "tags": ["navigation", "index"],
            "related_files": related,
            "distilled_by": "scan",
            "update_mode": "bootstrap",
            "navigation_role": "index",
        },
        "source": source or {},
    }


def build_navigation_candidates(
    workspace_path: str | Path,
    project_key_value: str,
    *,
    source: dict | None = None,
) -> list[dict]:
    """Build the navigation index and per-module doc candidates from a scan."""
    scan = scan_workspace(workspace_path)
    project_name = str(scan.get("project_name") or "项目")
    candidates = [
        {
            "kind": "navigation",
            "scope": "project",
            "project_key": project_key_value,
            "slug": NAVIGATION_SLUG,
            "title": f"{project_name} 导航入口",
            "body": render_navigation_body(scan, link_modules=True),
            "meta": {
                "type": "navigation",
                "outcome": "success",
                "confidence": 0.4,
                "tags": ["navigation", "index"],
                "related_files": [m["files"] for m in scan.get("modules") or [] if m.get("files")],
                "distilled_by": "scan",
                "update_mode": "bootstrap",
                "navigation_role": "index",
            },
            "source": source or {},
        }
    ]
    for mod in scan.get("modules") or []:
        name = str(mod.get("name") or "").strip()
        if not name:
            continue
        files = [str(mod.get("files") or "").strip()] if str(mod.get("files") or "").strip() else []
        candidates.append({
            "kind": "navigation",
            "scope": "project",
            "project_key": project_key_value,
            "slug": _module_doc_slug(name),
            "title": f"{project_name} / {name} 模块文档",
            "body": render_module_navigation_body(project_name, mod),
            "meta": {
                "type": "navigation",
                "outcome": "success",
                "confidence": 0.35,
                "tags": ["navigation", "module", slugify(name)],
                "related_files": files,
                "distilled_by": "scan",
                "update_mode": "bootstrap",
                "navigation_role": "module",
            },
            "source": source or {},
        })
    return candidates


def _navigation_parent_slug(slug: str) -> str | None:
    slug = normalize_entry_slug(str(slug or "").strip())
    if not slug or slug == NAVIGATION_SLUG:
        return None
    parts = slug.split("/")
    if len(parts) <= 2 and parts[0] in _NAVIGATION_ROOT_DIRS:
        return NAVIGATION_SLUG
    if len(parts) > 2 and parts[0] in _NAVIGATION_ROOT_DIRS:
        return "/".join(parts[:-1])
    return NAVIGATION_SLUG


def _navigation_role_for_slug(slug: str) -> str:
    if slug == NAVIGATION_SLUG:
        return "index"
    if slug.startswith(f"{NAVIGATION_MODULES_DIR}/"):
        return "module"
    if slug.startswith(f"{NAVIGATION_FLOWS_DIR}/"):
        return "flow"
    return "navigation"


def _navigation_error(code: str, message: str, **extra) -> dict:
    item = {"code": code, "message": message}
    item.update({key: value for key, value in extra.items() if value not in (None, "", [])})
    return item


def _is_valid_navigation_slug(raw_slug: str) -> tuple[bool, str, str | None]:
    slug = str(raw_slug or "").strip().strip("/")
    normalized = normalize_entry_slug(slug)
    if not slug:
        return False, normalized, "navigation candidate is missing slug"
    if slug != normalized:
        return False, normalized, f"navigation slug must already be normalized: {normalized}"
    if slug == NAVIGATION_SLUG:
        return True, normalized, None
    parts = slug.split("/")
    if parts[0] not in _NAVIGATION_ROOT_DIRS or len(parts) < 2:
        return False, normalized, "navigation slug must be index, modules/<name>, or flows/<name>"
    return True, normalized, None


def _markdown_navigation_targets(body: str) -> list[tuple[str, str]]:
    targets: list[tuple[str, str]] = []
    for match in _NAVIGATION_LINK_RE.finditer(body or ""):
        raw = match.group(1).strip()
        if not raw:
            continue
        href = raw.split()[0].strip("<>")
        lowered = href.lower()
        if (
            lowered.startswith(("http://", "https://", "mailto:", "tel:"))
            or href.startswith("#")
            or not href.endswith(".md")
        ):
            continue
        href = href.split("#", 1)[0].split("?", 1)[0]
        target = href[:-3].strip("/")
        targets.append((href, target))
    return targets


def validate_navigation_candidates(
    root: Path,
    config: dict | None,
    candidates: list[dict],
) -> dict:
    """Validate project navigation candidates before they enter the KB.

    The result is deliberately event/log friendly: callers can store it as-is
    when a bootstrap or delta is rejected.
    """
    navigation = [
        candidate for candidate in (candidates or [])
        if str(candidate.get("kind") or "").strip().lower() == "navigation"
    ]
    errors: list[dict] = []
    if not navigation:
        return {"ok": True, "errors": [], "warnings": [], "checked": 0}

    by_key: dict[tuple[str, str | None, str], dict] = {}
    for candidate in navigation:
        raw_slug = str(candidate.get("slug") or "").strip()
        ok, slug, reason = _is_valid_navigation_slug(raw_slug)
        scope = str(candidate.get("scope") or "project")
        project_key_value = candidate.get("project_key")
        if not ok:
            errors.append(_navigation_error(
                "invalid_slug",
                reason or "invalid navigation slug",
                slug=raw_slug,
                normalized_slug=slug,
            ))
            continue
        key = (scope, project_key_value, slug)
        if key in by_key:
            errors.append(_navigation_error(
                "duplicate_slug",
                "duplicate navigation candidate slug in one batch",
                slug=slug,
                project_key=project_key_value,
            ))
            continue
        by_key[key] = candidate

        if scope != "project":
            errors.append(_navigation_error(
                "invalid_scope",
                "project navigation candidates must use project scope",
                slug=slug,
                scope=scope,
            ))
        if slug != NAVIGATION_SLUG and not project_key_value:
            errors.append(_navigation_error(
                "missing_project_key",
                "project navigation child candidate requires project_key",
                slug=slug,
            ))

    def exists(scope: str, project_key_value: str | None, slug: str) -> bool:
        return (
            (scope, project_key_value, slug) in by_key
            or entry_path_for(root, config, scope, "navigation", project_key_value, slug) is not None
        )

    for (scope, project_key_value, slug), candidate in by_key.items():
        parent_slug = _navigation_parent_slug(slug)
        if parent_slug and not exists(scope, project_key_value, parent_slug):
            errors.append(_navigation_error(
                "missing_parent",
                "navigation child is missing its direct parent entry",
                slug=slug,
                expected_parent=parent_slug,
                project_key=project_key_value,
            ))

        for href, target in _markdown_navigation_targets(str(candidate.get("body") or "")):
            if "/" in target and any(part in {"", ".", ".."} for part in target.split("/")):
                errors.append(_navigation_error(
                    "invalid_link_target",
                    "navigation link target must not use path traversal",
                    slug=slug,
                    href=href,
                ))
                continue
            normalized_target = normalize_entry_slug(target)
            if target != normalized_target or not target:
                errors.append(_navigation_error(
                    "invalid_link_target",
                    "navigation link target must already be a normalized navigation slug",
                    slug=slug,
                    href=href,
                    normalized_slug=normalized_target,
                ))
                continue
            if _navigation_parent_slug(normalized_target) != slug:
                errors.append(_navigation_error(
                    "indirect_link",
                    "navigation links must point only to direct child docs",
                    slug=slug,
                    href=href,
                    target_slug=normalized_target,
                    expected_parent=slug,
                    actual_parent=_navigation_parent_slug(normalized_target),
                ))
                continue
            if not exists(scope, project_key_value, normalized_target):
                errors.append(_navigation_error(
                    "broken_link",
                    "navigation link target is not in this batch and does not exist in the KB",
                    slug=slug,
                    href=href,
                    target_slug=normalized_target,
                    project_key=project_key_value,
                ))

    return {
        "ok": not errors,
        "errors": errors,
        "warnings": [],
        "checked": len(navigation),
        "slugs": sorted(slug for _, _, slug in by_key),
    }


def _bootstrap_event_payload(
    *,
    status: str,
    project_key_value: str | None,
    candidates: list[dict] | None = None,
    validation: dict | None = None,
    reason: str | None = None,
) -> dict:
    data = {
        "status": status,
        "project_key": project_key_value,
        "candidates": len(candidates or []),
        "slugs": [str(candidate.get("slug")) for candidate in (candidates or []) if candidate.get("slug")],
    }
    if validation is not None:
        data["validation"] = validation
    if reason:
        data["reason"] = reason
    return {"type": "knowledge_navigation_bootstrap", "data": data}


def build_navigation_bootstrap_prompt(scan: dict, *, workspace_path: str, project_key_value: str) -> str:
    """Prompt for agent-assisted first navigation generation."""
    return render_prompt_template(
        "knowledge_navigation_bootstrap.md",
        workspace_path=workspace_path,
        project_key_value=project_key_value,
    )


def default_navigation_agent(context: dict) -> str:
    """Run the bootstrap prompt through the same backend exec seam as capture notes."""
    try:
        from aha_cli.services.knowledge_capture_distill import CaptureAgentError, default_capture_agent

        return default_capture_agent(context)
    except CaptureAgentError as exc:
        raise NavigationAgentError(str(exc)) from exc
    except Exception as exc:  # noqa: BLE001 - normalize backend wiring failures
        raise NavigationAgentError(str(exc)) from exc


def _reply_excerpt(reply: str, *, limit: int = 1200) -> str:
    text = (reply or "").strip()
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + " ..."


def _prompt_excerpt(prompt: str) -> str:
    return _reply_excerpt(prompt, limit=4000)


def _json_text_variants(text: str) -> list[str]:
    variants = [text]
    variants.extend(match.group(1).strip() for match in _JSON_FENCE_RE.finditer(text) if match.group(1).strip())
    return variants


def _json_candidates_from_agent_reply(reply: str) -> tuple[list[dict] | None, str | None]:
    text = (reply or "").strip()
    if not text:
        return None, "empty agent reply"
    parsed = None
    json_error: str | None = None
    for variant in _json_text_variants(text):
        try:
            parsed = json.loads(variant)
            break
        except json.JSONDecodeError as exc:
            json_error = str(exc)
            continue
    if parsed is None:
        try:
            from aha_cli.store.knowledge_sidecar import split_knowledge_sidecar

            _, parsed, error = split_knowledge_sidecar(text)
        except Exception as exc:  # noqa: BLE001
            return None, str(exc)
        if parsed is None:
            return None, error or json_error or "agent reply is not JSON"
    if isinstance(parsed, dict) and isinstance(parsed.get("candidates"), list):
        parsed = parsed["candidates"]
    if not isinstance(parsed, list):
        return None, "agent reply must be a JSON array"
    candidates = [item for item in parsed if isinstance(item, dict)]
    return candidates, None


def _agent_navigation_candidates(
    root: Path,
    config: dict | None,
    *,
    workspace_path: str,
    project_key_value: str,
    scan: dict | None = None,
    backend: str | None = None,
    model: str | None = None,
    proxy_enabled: bool | None = None,
    agent: NavigationAgent | None = None,
    progress_callback: Callable[[str, dict], None] | None = None,
) -> tuple[list[dict] | None, dict]:
    if not (agent or backend or model):
        return None, {"status": "failed", "error": "backend not selected"}
    prompt = build_navigation_bootstrap_prompt(scan, workspace_path=workspace_path, project_key_value=project_key_value)
    prompt_excerpt = _prompt_excerpt(prompt)
    agent_fn = agent or default_navigation_agent
    try:
        reply = agent_fn({
            "prompt": prompt,
            "backend": backend,
            "model": model,
            "proxy_enabled": proxy_enabled,
            "config": config,
            "cwd": workspace_path,
            "workspace_path": workspace_path,
            "project_key": project_key_value,
            "scan": scan or {},
            "progress_callback": progress_callback,
        })
    except NavigationAgentError as exc:
        return None, {"status": "failed", "error": str(exc), "prompt_excerpt": prompt_excerpt}
    except Exception as exc:  # noqa: BLE001 - record agent failure in the draft
        return None, {"status": "failed", "error": str(exc), "prompt_excerpt": prompt_excerpt}
    raw_candidates, parse_error = _json_candidates_from_agent_reply(reply or "")
    if raw_candidates is None:
        return None, {
            "status": "invalid",
            "error": parse_error or "invalid agent reply",
            "prompt_excerpt": prompt_excerpt,
            "reply_excerpt": _reply_excerpt(reply or ""),
        }

    source = {"source_type": "navigation_agent", "generated_at": utc_now()}
    from aha_cli.services.knowledge_distill import ensure_navigation_parent_entries, normalize_sidecar_candidates

    normalized = normalize_sidecar_candidates({"project_key": project_key_value, "source": source}, raw_candidates)
    candidates = [
        candidate for candidate in normalized
        if str(candidate.get("kind") or "").strip().lower() == "navigation"
    ]
    for candidate in candidates:
        candidate["scope"] = "project"
        candidate["project_key"] = project_key_value
        meta = candidate.setdefault("meta", {})
        meta["type"] = "navigation"
        meta.setdefault("distilled_by", "agent")
        meta.setdefault("update_mode", "bootstrap")
    candidates = ensure_navigation_parent_entries(root, config, candidates, {"source": source, "workspace_path": workspace_path})
    if not candidates:
        return None, {
            "status": "invalid",
            "error": "agent produced no navigation candidates",
            "prompt_excerpt": prompt_excerpt,
            "reply_excerpt": _reply_excerpt(reply or ""),
        }
    return candidates, {
        "status": "used",
        "candidates": len(candidates),
        "prompt_excerpt": prompt_excerpt,
        "reply_excerpt": _reply_excerpt(reply or ""),
    }


def prepare_project_navigation(
    root: Path,
    config: dict | None,
    *,
    workspace_path: str,
    goal: str | None = None,
    project_key_value: str | None = None,
    backend: str | None = None,
    model: str | None = None,
    proxy_enabled: bool | None = None,
    agent: NavigationAgent | None = None,
    progress_callback: Callable[[str, dict], None] | None = None,
) -> dict:
    """Generate and validate the first project navigation batch without writing it."""
    key = project_key_value or project_key(Path(workspace_path), goal=goal)
    existing_index = entry_path_for(root, config, "project", "navigation", key, NAVIGATION_SLUG)
    if existing_index:
        result = {
            "ok": True,
            "skipped": "navigation exists",
            "candidates": 0,
            "project_key": key,
            "index_path": str(existing_index),
        }
        result["event"] = _bootstrap_event_payload(status="skipped", project_key_value=key, reason=result["skipped"])
        return result

    agent_info: dict = {"status": "not_run"}
    candidates, agent_info = _agent_navigation_candidates(
        root,
        config,
        workspace_path=workspace_path,
        project_key_value=key,
        backend=backend,
        model=model,
        proxy_enabled=proxy_enabled,
        agent=agent,
        progress_callback=progress_callback,
    )
    if candidates is None:
        error = agent_info.get("error") or agent_info.get("reason") or "navigation agent did not produce candidates"
        return {
            "ok": False,
            "error": error,
            "candidates": 0,
            "project_key": key,
            "agent": agent_info,
            "event": _bootstrap_event_payload(status="invalid", project_key_value=key, reason=error),
        }
    validation = validate_navigation_candidates(root, config, candidates)
    if not validation["ok"]:
        agent_info = {**agent_info, "status": "invalid", "validation": validation}
        return {
            "ok": False,
            "error": "navigation bootstrap validation failed",
            "candidates": 0,
            "project_key": key,
            "agent": agent_info,
            "validation": validation,
            "event": _bootstrap_event_payload(
                status="invalid",
                project_key_value=key,
                candidates=candidates,
                validation=validation,
            ),
        }
    return {
        "ok": True,
        "candidates": len(candidates),
        "candidate_items": candidates,
        "title": candidates[0]["title"] if candidates else None,
        "project_key": key,
        "validation": validation,
        "agent": agent_info,
        "event": _bootstrap_event_payload(
            status="prepared",
            project_key_value=key,
            candidates=candidates,
            validation=validation,
        ),
    }


def bootstrap_project_navigation(
    root: Path,
    config: dict | None,
    *,
    workspace_path: str,
    goal: str | None = None,
    project_key_value: str | None = None,
    backend: str | None = None,
    model: str | None = None,
    proxy_enabled: bool | None = None,
    agent: NavigationAgent | None = None,
    progress_callback: Callable[[str, dict], None] | None = None,
) -> dict:
    """Create the first project navigation batch if the project has no index."""
    prepared = prepare_project_navigation(
        root,
        config,
        workspace_path=workspace_path,
        goal=goal,
        project_key_value=project_key_value,
        backend=backend,
        model=model,
        proxy_enabled=proxy_enabled,
        agent=agent,
        progress_callback=progress_callback,
    )
    if prepared.get("skipped") or not prepared.get("ok"):
        return prepared
    candidates = prepared.get("candidate_items") or []
    key = str(prepared.get("project_key") or "")

    # Lazy import avoids a services import cycle (distill imports this module for
    # validation).
    from aha_cli.services.knowledge_distill import distill_and_enqueue

    result = distill_and_enqueue(
        root,
        config,
        {"project_key": key, "allow_navigation_bootstrap": True},
        candidates=candidates,
    )
    result["title"] = candidates[0]["title"] if candidates else None
    result["project_key"] = key
    result["validation"] = prepared.get("validation")
    result["agent"] = prepared.get("agent")
    result["event"] = _bootstrap_event_payload(
        status="queued" if result.get("gate") == "manual" else "written",
        project_key_value=key,
        candidates=candidates,
        validation=prepared.get("validation"),
    )
    return result


def generate_navigation_candidate(
    root: Path,
    config: dict | None,
    *,
    workspace_path: str,
    goal: str | None = None,
    project_key_value: str | None = None,
    backend: str | None = None,
    model: str | None = None,
    proxy_enabled: bool | None = None,
    agent: NavigationAgent | None = None,
    progress_callback: Callable[[str, dict], None] | None = None,
) -> dict:
    """Build and route project navigation candidates through the curation gate."""
    return bootstrap_project_navigation(
        root,
        config,
        workspace_path=workspace_path,
        goal=goal,
        project_key_value=project_key_value,
        backend=backend,
        model=model,
        proxy_enabled=proxy_enabled,
        agent=agent,
        progress_callback=progress_callback,
    )
