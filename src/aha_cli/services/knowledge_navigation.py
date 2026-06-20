"""Project navigation ("memory palace") map generation.

A navigation entry is a single per-project "项目地图": what the project is, its
architecture at a glance, and a module → responsibility → key-files index. An
agent reads it before working so it can jump straight to the relevant module
instead of re-reading the whole codebase, then refines it after the work.

This module provides a deterministic, dependency-free *scan* that turns a
workspace into a skeleton map candidate. It deliberately does not invent
content: it lists what exists (top-level packages, sub-packages, entry points)
and pulls one-line module docstrings where present, leaving everything else as
``(待补充)`` for a human or a richer sidecar map to fill in. The candidate flows
through the normal curation gate (``.pending`` by default).
"""

from __future__ import annotations

import re
from pathlib import Path

from aha_cli.domain.models import utc_now
from aha_cli.store.knowledge import (
    NAVIGATION_SLUG,
    knowledge_config,
    project_key,
)

# Directories that are never project modules worth mapping.
_IGNORE_DIRS = {
    ".git", ".hg", ".svn", "__pycache__", ".pytest_cache", ".mypy_cache",
    ".ruff_cache", "node_modules", ".venv", "venv", "env", ".aha", ".idea",
    ".vscode", "dist", "build", ".eggs", "htmlcov", ".tox", "site-packages",
}
_DOCSTRING_RE = re.compile(r'^\s*(?:[rRbBuU]{0,2})("""|\'\'\')(.*?)\1', re.DOTALL)


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
    """Deterministically extract a project map skeleton from a workspace tree."""
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


def render_navigation_body(scan: dict) -> str:
    """Render a scan dict into the navigation entry body (markdown)."""
    overview = (scan.get("overview") or "").strip()
    module_lines = []
    for mod in scan.get("modules") or []:
        name = str(mod.get("name") or "?")
        role = str(mod.get("role") or "").strip()
        files = str(mod.get("files") or "").strip()
        line = f"- **{name}**"
        if role:
            line += f" — {role}"
        if files:
            line += f" (`{files}`)"
        module_lines.append(line)
    entry_lines = [f"- `{item}`" for item in (scan.get("entry_points") or [])]
    parts = [
        "## 项目定位",
        overview or "(待补充：这个项目是干什么的)",
        "",
        "## 架构概览",
        "(待补充：分层 / 核心组件 / 数据流)",
        "",
        "## 模块索引",
        "\n".join(module_lines) if module_lines else "- (未发现可索引模块)",
        "",
        "## 入口 / 关键流程",
        "\n".join(entry_lines) if entry_lines else "- (待补充)",
        "",
        "## 盲区 / 待补充",
        "- 由 scan 生成的骨架，职责/架构/流程需人工或后续任务补全。",
    ]
    return "\n".join(parts).strip() + "\n"


def build_navigation_candidate(
    workspace_path: str | Path,
    project_key_value: str,
    *,
    source: dict | None = None,
) -> dict:
    """Build a navigation map candidate (pending-queue shape) from a scan."""
    scan = scan_workspace(workspace_path)
    title = f"{scan.get('project_name') or '项目'} 项目地图"
    related = [m["files"] for m in scan.get("modules") or [] if m.get("files")]
    return {
        "kind": "navigation",
        "scope": "project",
        "project_key": project_key_value,
        "slug": NAVIGATION_SLUG,
        "title": title,
        "body": render_navigation_body(scan),
        "meta": {
            "type": "navigation",
            "outcome": "success",
            "confidence": 0.4,  # scan skeleton, unreviewed
            "tags": ["navigation", "map"],
            "related_files": related,
            "distilled_by": "scan",
        },
        "source": source or {},
    }


def generate_navigation_candidate(
    root: Path,
    config: dict | None,
    *,
    workspace_path: str,
    goal: str | None = None,
    project_key_value: str | None = None,
) -> dict:
    """Build and route a project map candidate through the curation gate."""
    cfg = knowledge_config(config)
    if not cfg.get("enabled"):
        return {"ok": True, "skipped": "knowledge disabled", "candidates": 0}
    key = project_key_value or project_key(Path(workspace_path), goal=goal)
    candidate = build_navigation_candidate(
        workspace_path, key, source={"source_type": "navigation_scan", "generated_at": utc_now()}
    )
    # Lazy import avoids a services import cycle (distill imports the store).
    from aha_cli.services.knowledge_distill import distill_and_enqueue

    result = distill_and_enqueue(root, config, {"project_key": key}, candidates=[candidate])
    result["title"] = candidate["title"]
    result["project_key"] = key
    return result
