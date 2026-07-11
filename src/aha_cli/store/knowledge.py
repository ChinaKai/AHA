"""Knowledge base storage layer (Phase 1).

The knowledge base is a directory that lives independently of runs/tasks so it
survives run deletion and can be migrated between machines. Later phases turn
this directory into an AHA-managed git repository (see
``docs/knowledge-base-plan.md``). This module owns the on-disk layout, the
stable project-key derivation, and the primitive entry read/write operations.

Entries are Markdown files with a dependency-free JSON frontmatter block::

    ---
    {"id": "kb_...", "type": "solution", ...}
    ---

    <markdown body>

JSON frontmatter is used instead of YAML because AHA declares no third-party
dependencies and PyYAML is not guaranteed to be present.
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import json
import re
import shutil
from pathlib import Path
from urllib.parse import unquote

from aha_cli.domain.models import default_knowledge_config, utc_now
from aha_cli.store.io import read_json, write_json
from aha_cli.store.paths import default_knowledge_dir

KNOWLEDGE_SCHEMA_VERSION = 1
KNOWLEDGE_INDEX_FILE = "aha-knowledge.json"
KNOWLEDGE_README_FILE = "README.md"
KNOWLEDGE_GITIGNORE_FILE = ".gitignore"
GENERAL_DIR = "general"
PERSONAL_DIR = "personal"
PROJECTS_DIR = "projects"
PENDING_DIR = ".pending"
NAV_DRAFTS_DIR = ".nav_drafts"
ENTRY_KINDS = ("wiki", "solutions", "navigation", "worklog")
# `personal` is a user scratch scope: stored and searchable like general, but
# deliberately NOT auto-injected into task prompts (see knowledge_retrieval).
SCOPES = ("general", "project", "personal")
ENTRY_KINDS_BY_SCOPE = {
    "general": ("wiki", "solutions"),
    "personal": ("wiki", "solutions"),
    "project": ENTRY_KINDS,
}

# Stable mapping between on-disk kind (directory) and frontmatter ``type``.
_KIND_TO_TYPE = {
    "wiki": "wiki",
    "solutions": "solution",
    "navigation": "navigation",
    "worklog": "task_worklog",
}
_TYPE_TO_KIND = {
    "wiki": "wiki",
    "solution": "solutions",
    "navigation": "navigation",
    "task_worklog": "worklog",
    "worklog": "worklog",
}

# A project's navigation entry point is intentionally small. Detailed project
# orientation lives under nested navigation docs such as modules/<name>.md.
NAVIGATION_SLUG = "index"
NAVIGATION_MODULES_DIR = "modules"
NAVIGATION_FLOWS_DIR = "flows"


def kind_for_type(entry_type: str | None) -> str:
    return _TYPE_TO_KIND.get(str(entry_type or ""), "solutions")


def type_for_kind(kind: str) -> str:
    return _KIND_TO_TYPE.get(str(kind or ""), "solution")


def entry_kinds_for_scope(scope: str) -> tuple[str, ...]:
    return ENTRY_KINDS_BY_SCOPE.get(str(scope or ""), ())


_FRONTMATTER_FENCE = "---"


# --------------------------------------------------------------------------- #
# Location
# --------------------------------------------------------------------------- #
def knowledge_root(root: Path, config: dict | None = None) -> Path:
    """Resolve the knowledge base root, honoring a ``knowledge.path`` override."""
    knowledge_cfg = (config or {}).get("knowledge") if config else None
    if isinstance(knowledge_cfg, dict):
        override = knowledge_cfg.get("path")
        if override:
            return Path(str(override)).expanduser()
    return default_knowledge_dir(root)


def knowledge_config(config: dict | None) -> dict:
    cfg = (config or {}).get("knowledge") if config else None
    return cfg if isinstance(cfg, dict) else default_knowledge_config()


def _index_path(kb_root: Path) -> Path:
    return kb_root / KNOWLEDGE_INDEX_FILE


# --------------------------------------------------------------------------- #
# Identity helpers
# --------------------------------------------------------------------------- #
def slugify(text: str, *, max_length: int = 60) -> str:
    """Turn a title into a filesystem-safe, stable slug."""
    normalized = re.sub(r"[^a-z0-9]+", "-", (text or "").strip().lower()).strip("-")
    if not normalized:
        # Non-ASCII titles (e.g. Chinese) collapse to empty; fall back to a hash.
        normalized = "kb-" + hashlib.sha1((text or "").encode("utf-8")).hexdigest()[:10]
    return normalized[:max_length].strip("-")


def normalize_entry_slug(slug: str) -> str:
    """Normalize a possibly nested entry slug without allowing path traversal."""
    raw = str(slug or "").strip().replace("\\", "/")
    parts: list[str] = []
    for part in raw.split("/"):
        clean = part.strip()
        if not clean or clean in {".", ".."}:
            continue
        parts.append(slugify(clean))
    return "/".join(parts) if parts else slugify(raw)


def normalize_git_remote(remote: str) -> str:
    """Normalize a git remote URL so the same repo maps to one key across hosts.

    ``git@github.com:user/repo.git`` and ``https://github.com/user/repo``
    both normalize to ``github.com/user/repo``.
    """
    value = (remote or "").strip()
    if not value:
        return ""
    value = re.sub(r"\.git$", "", value)
    scp = re.match(r"^[\w.+-]+@([^:]+):(.+)$", value)
    if scp:
        host, path = scp.group(1), scp.group(2)
    else:
        stripped = re.sub(r"^[a-zA-Z][a-zA-Z0-9+.-]*://", "", value)
        stripped = re.sub(r"^[^@/]+@", "", stripped)  # drop userinfo
        host, _, path = stripped.partition("/")
    host = host.lower().strip("/")
    path = path.strip("/").lower()
    return f"{host}/{path}" if path else host


def _git_remote_for(workspace: Path) -> str:
    """Best-effort read of the workspace's origin remote without importing git."""
    config_file = workspace / ".git" / "config"
    if not config_file.is_file():
        return ""
    try:
        text = config_file.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    in_origin = False
    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith("["):
            in_origin = line.replace(" ", "").lower() == '[remote"origin"]'
            continue
        if in_origin and line.lower().startswith("url"):
            _, _, value = line.partition("=")
            return value.strip()
    return ""


def project_key(workspace: Path, goal: str | None = None) -> str:
    """Derive a stable, migratable project key.

    Priority: repo-name + git origin remote hash (stable across machines/paths
    and still human-readable), else a slug built from the run goal and
    workspace directory name. The fallback deliberately excludes the absolute
    path so the same project resolves to the same key after being moved or
    cloned to a different location/machine.
    """
    return project_key_aliases(workspace, goal=goal)[0]


def project_key_aliases(workspace: Path, goal: str | None = None) -> list[str]:
    """Return the preferred project key followed by compatible legacy keys.

    Older AHA versions used ``git-<hash>`` for git projects. Keep that alias so
    existing project knowledge remains readable after the more descriptive key
    format starts writing ``<repo-name>-git-<hash>``.
    """
    workspace = Path(workspace).expanduser()
    remote = normalize_git_remote(_git_remote_for(workspace))
    if remote:
        digest = hashlib.sha1(remote.encode("utf-8")).hexdigest()[:12]
        repo_name = remote.rsplit("/", 1)[-1] or "repo"
        preferred = f"{slugify(repo_name, max_length=40)}-git-{digest}"
        legacy = f"git-{digest}"
        return [preferred, legacy] if preferred != legacy else [preferred]
    basis = "-".join(part for part in [(goal or "").strip(), workspace.name] if part)
    if not basis:
        basis = "workspace"
    # Digest over the migratable basis (goal + dir name), never the absolute path.
    digest = hashlib.sha1(basis.encode("utf-8")).hexdigest()[:8]
    return [f"ws-{slugify(basis)}-{digest}"]


# --------------------------------------------------------------------------- #
# Layout
# --------------------------------------------------------------------------- #
def scope_dir(kb_root: Path, scope: str, project_key_value: str | None) -> Path:
    if scope == "general":
        return kb_root / GENERAL_DIR
    if scope == "personal":
        return kb_root / PERSONAL_DIR
    if scope == "project":
        if not project_key_value:
            raise ValueError("project scope requires a project_key")
        return kb_root / PROJECTS_DIR / project_key_value
    raise ValueError(f"unknown scope: {scope!r}")


def entry_dir(kb_root: Path, scope: str, kind: str, project_key_value: str | None) -> Path:
    root = scope_dir(kb_root, scope, project_key_value)
    if kind not in entry_kinds_for_scope(scope):
        raise ValueError(f"entry kind {kind!r} is not valid for scope {scope!r}")
    return root / kind


def init_knowledge_base(root: Path, config: dict | None = None) -> dict:
    """Create the knowledge base skeleton. Idempotent."""
    kb_root = knowledge_root(root, config)
    created = not kb_root.exists()
    for kind in entry_kinds_for_scope("general"):
        (kb_root / GENERAL_DIR / kind).mkdir(parents=True, exist_ok=True)
    for kind in entry_kinds_for_scope("personal"):
        (kb_root / PERSONAL_DIR / kind).mkdir(parents=True, exist_ok=True)
    (kb_root / PROJECTS_DIR).mkdir(parents=True, exist_ok=True)

    index_path = _index_path(kb_root)
    if index_path.exists():
        index = read_json(index_path)
    else:
        index = {
            "schema_version": KNOWLEDGE_SCHEMA_VERSION,
            "created_at": utc_now(),
            "updated_at": utc_now(),
        }
        write_json(index_path, index)

    readme = kb_root / KNOWLEDGE_README_FILE
    if not readme.exists():
        readme.write_text(_render_readme(), encoding="utf-8")

    # Unreviewed candidates, distill logs, and navigation drafts are
    # review/runtime state. Raw capture notes/assets are user material and stay
    # syncable, so only the generated distill logs are ignored.
    gitignore = kb_root / KNOWLEDGE_GITIGNORE_FILE
    required_ignores = [
        f"{PENDING_DIR}/",
        "capture/distill/",
        ".capture/distill/",
        f"{NAV_DRAFTS_DIR}/",
    ]
    obsolete_ignores = {".capture/", "capture/"}
    existing = gitignore.read_text(encoding="utf-8").splitlines() if gitignore.exists() else []
    existing = [line for line in existing if line.strip() not in obsolete_ignores]
    normalized_existing = {line.strip() for line in existing}
    missing = [line for line in required_ignores if line not in normalized_existing]
    if missing:
        content = "\n".join(existing + missing).strip() + "\n"
        gitignore.write_text(content, encoding="utf-8")

    return {
        "path": str(kb_root),
        "created": created,
        "schema_version": index.get("schema_version", KNOWLEDGE_SCHEMA_VERSION),
    }


def _render_readme() -> str:
    return (
        "# AHA Knowledge Base\n\n"
        "This directory is managed by AHA. It persists distilled knowledge "
        "independently of runs/tasks.\n\n"
        "- `general/wiki` — cross-project tutorials and technical references\n"
        "- `general/solutions` — cross-project reusable playbooks\n"
        "- `personal/wiki` — personal notes that should be searchable but not auto-injected\n"
        "- `personal/solutions` — personal reusable playbooks\n"
        "- `projects/<project-key>/solutions` — rare reusable project playbooks\n"
        "- `projects/<project-key>/worklog/tasks/YYYY/MM/YYYYMMDD-<task-title>.md` — live task plans, progress, requirement changes, decisions, and verification notes\n"
        "- `projects/<project-key>/navigation/index.md` — the project navigation entry point\n"
        "- `projects/<project-key>/navigation/modules/*.md` — on-demand module docs\n"
        "- `projects/<project-key>/navigation/flows/*.md` — on-demand flow docs\n"
        "- `skills/<skill-id>/` — managed AHA task skills "
        "copied from legacy `AHA_HOME/skills` and edited through the Skills UI\n"
        "- `capture/*.json` — raw user notes awaiting distill, kept syncable\n"
        "- `capture/assets/*` — raw note attachments, kept syncable\n\n"
        "Project navigation is incremental: read `navigation/index.md` first, then "
        "only the module/flow docs relevant to the task; each nav doc owns one "
        "link layer, and updates should touch only the docs affected by the task.\n\n"
        "`capture/distill/`, `.pending/`, and `.nav_drafts/` are review/runtime "
        "state and are ignored by git.\n\n"
        "Entries are Markdown files with a JSON frontmatter block. Do not edit "
        "`aha-knowledge.json` by hand.\n"
    )


# --------------------------------------------------------------------------- #
# Frontmatter codec
# --------------------------------------------------------------------------- #
def serialize_entry(meta: dict, body: str) -> str:
    front = json.dumps(meta, ensure_ascii=False, indent=2, sort_keys=True)
    body = (body or "").strip("\n")
    return f"{_FRONTMATTER_FENCE}\n{front}\n{_FRONTMATTER_FENCE}\n\n{body}\n"


def parse_entry(text: str) -> tuple[dict, str]:
    lines = text.splitlines()
    if not lines or lines[0].strip() != _FRONTMATTER_FENCE:
        raise ValueError("entry is missing a frontmatter block")
    closing = None
    for idx in range(1, len(lines)):
        if lines[idx].strip() == _FRONTMATTER_FENCE:
            closing = idx
            break
    if closing is None:
        raise ValueError("entry frontmatter is not terminated")
    meta = json.loads("\n".join(lines[1:closing]))
    body = "\n".join(lines[closing + 1 :]).strip("\n")
    return meta, body


# --------------------------------------------------------------------------- #
# Entry primitives
# --------------------------------------------------------------------------- #
def write_entry(
    root: Path,
    *,
    config: dict | None,
    scope: str,
    kind: str,
    title: str,
    body: str,
    project_key_value: str | None = None,
    meta: dict | None = None,
    slug: str | None = None,
) -> Path:
    """Write (or overwrite) a knowledge entry, returning its path."""
    kb_root = knowledge_root(root, config)
    target_dir = entry_dir(kb_root, scope, kind, project_key_value)
    target_dir.mkdir(parents=True, exist_ok=True)
    entry_slug = normalize_entry_slug(slug) if slug else slugify(title)
    path = target_dir / f"{entry_slug}.md"
    path.parent.mkdir(parents=True, exist_ok=True)

    now = utc_now()
    full_meta = dict(meta or {})
    full_meta.setdefault("type", type_for_kind(kind))
    full_meta["scope"] = scope
    full_meta["project_key"] = project_key_value if scope == "project" else None
    full_meta["title"] = title
    full_meta["slug"] = entry_slug
    full_meta["id"] = entry_id(scope, kind, full_meta["project_key"], entry_slug)
    full_meta.setdefault("created_at", now)
    full_meta["updated_at"] = now
    if path.exists():
        try:
            existing_meta, _ = parse_entry(path.read_text(encoding="utf-8"))
            full_meta["created_at"] = existing_meta.get("created_at", full_meta["created_at"])
            # Preserve the original stable id across rewrites of the same entry.
            full_meta["id"] = existing_meta.get("id", full_meta["id"])
        except (OSError, ValueError):
            pass

    path.write_text(serialize_entry(full_meta, body), encoding="utf-8")
    return path


_NAVIGATION_MERGE_LIST_SECTIONS = {
    "关键源文件",
    "入口 / 调用方",
    "入口 / 关键流程",
    "常用排查路径",
    "修改注意",
    "相关测试",
    "盲区 / 待补充",
    "模块索引",
    "下级入口",
}


def _merge_unique_items(existing: object, additions: object) -> list:
    merged = list(existing or []) if isinstance(existing, list) else []
    seen = {json.dumps(item, ensure_ascii=False, sort_keys=True) for item in merged}
    if isinstance(additions, list):
        for item in additions:
            key = json.dumps(item, ensure_ascii=False, sort_keys=True)
            if key not in seen:
                merged.append(item)
                seen.add(key)
    return merged


def _markdown_h1(body: str, fallback: str) -> str:
    for line in str(body or "").splitlines():
        clean = line.strip()
        if clean.startswith("# "):
            return clean[2:].strip() or fallback
    return fallback


def _split_h2_sections(body: str) -> tuple[list[str], list[tuple[str, list[str]]]]:
    preamble: list[str] = []
    sections: list[tuple[str, list[str]]] = []
    current_title: str | None = None
    current_lines: list[str] = []
    for line in str(body or "").splitlines():
        match = re.match(r"^##\s+(.+?)\s*$", line)
        if match:
            if current_title is None:
                preamble = current_lines
            else:
                sections.append((current_title, current_lines))
            current_title = match.group(1).strip()
            current_lines = []
            continue
        current_lines.append(line)
    if current_title is None:
        preamble = current_lines
    else:
        sections.append((current_title, current_lines))
    return preamble, sections


def _trim_blank_lines(lines: list[str]) -> list[str]:
    start = 0
    end = len(lines)
    while start < end and not lines[start].strip():
        start += 1
    while end > start and not lines[end - 1].strip():
        end -= 1
    return lines[start:end]


def _section_text(lines: list[str]) -> str:
    return "\n".join(_trim_blank_lines(lines)).strip()


def _merge_navigation_list_lines(existing: list[str], additions: list[str]) -> list[str]:
    merged: list[str] = []
    seen: set[str] = set()
    for line in _trim_blank_lines(existing) + _trim_blank_lines(additions):
        clean = line.strip()
        if not clean or clean == "-":
            continue
        key = clean.casefold()
        if key not in seen:
            merged.append(line)
            seen.add(key)
    return merged or ["-"]


def _merge_navigation_section(title: str, existing: list[str], additions: list[str]) -> list[str]:
    existing_text = _section_text(existing)
    addition_text = _section_text(additions)
    if not existing_text or existing_text == "-":
        return _trim_blank_lines(additions) or ["-"]
    if not addition_text or addition_text == "-":
        return _trim_blank_lines(existing) or ["-"]
    if title in _NAVIGATION_MERGE_LIST_SECTIONS:
        return _merge_navigation_list_lines(existing, additions)
    if addition_text in existing_text:
        return _trim_blank_lines(existing)
    if existing_text in addition_text:
        return _trim_blank_lines(additions)
    return _trim_blank_lines(existing) + [""] + _trim_blank_lines(additions)


def _merge_navigation_body(existing_body: str, update_body: str, *, title: str) -> str:
    existing_preamble, existing_sections = _split_h2_sections(existing_body)
    update_preamble, update_sections = _split_h2_sections(update_body)
    h1 = _markdown_h1(update_body, _markdown_h1(existing_body, title))
    result: list[str] = [f"# {h1}", ""]
    existing_by_title = {section_title: lines for section_title, lines in existing_sections}
    update_by_title = {section_title: lines for section_title, lines in update_sections}
    ordered_titles = [section_title for section_title, _ in existing_sections]
    for section_title, _ in update_sections:
        if section_title not in ordered_titles:
            ordered_titles.append(section_title)

    if not ordered_titles:
        merged_preamble = _merge_navigation_section("", existing_preamble, update_preamble)
        return "\n".join(result + merged_preamble).strip() + "\n"

    for section_title in ordered_titles:
        result.append(f"## {section_title}")
        result.extend(_merge_navigation_section(
            section_title,
            existing_by_title.get(section_title, []),
            update_by_title.get(section_title, []),
        ))
        result.append("")
    return "\n".join(result).strip() + "\n"


def _merge_navigation_meta(existing_meta: dict, update_meta: dict) -> dict:
    merged = dict(existing_meta or {})
    merged.update(update_meta or {})
    for key in ("tags", "related_files", "diagnostic_paths", "source_tasks", "source_memos", "assets"):
        merged[key] = _merge_unique_items(existing_meta.get(key), update_meta.get(key))
    return merged


def _merge_existing_navigation_update(
    root: Path,
    config: dict | None,
    *,
    scope: str,
    kind: str,
    project_key_value: str | None,
    slug: str,
    title: str,
    body: str,
    meta: dict,
) -> tuple[str, str, dict]:
    if scope != "project" or kind != "navigation" or not slug:
        return title, body, meta
    existing_path = entry_path_for(root, config, scope, kind, project_key_value, slug)
    if not existing_path:
        return title, body, meta
    try:
        existing = read_entry(existing_path)
    except (OSError, ValueError):
        return title, body, meta
    existing_meta = dict(existing.get("meta") or {})
    merged_meta = _merge_navigation_meta(existing_meta, meta)
    merged_title = str(title or merged_meta.get("title") or existing_meta.get("title") or "").strip()
    merged_body = _merge_navigation_body(existing.get("body", ""), body, title=merged_title)
    return merged_title, merged_body, merged_meta


def write_entry_preserving_navigation(
    root: Path,
    *,
    config: dict | None,
    scope: str,
    kind: str,
    title: str,
    body: str,
    project_key_value: str | None = None,
    meta: dict | None = None,
    slug: str | None = None,
) -> Path:
    """Write an entry while merging project navigation updates by slug.

    Navigation docs are routers that accumulate module/file/flow knowledge over
    time. A candidate focused on one new topic must not erase older routing
    details for the same slug.
    """
    normalized_slug = normalize_entry_slug(slug) if slug else slugify(title)
    title, body, merged_meta = _merge_existing_navigation_update(
        root,
        config,
        scope=scope,
        kind=kind,
        project_key_value=project_key_value,
        slug=normalized_slug,
        title=title,
        body=body,
        meta=dict(meta or {}),
    )
    return write_entry(
        root,
        config=config,
        scope=scope,
        kind=kind,
        title=title,
        body=body,
        project_key_value=project_key_value,
        meta=merged_meta,
        slug=normalized_slug,
    )


def entry_id(scope: str, kind: str, project_key_value: str | None, slug: str) -> str:
    """Stable entry id derived from its identity (scope/kind/project/slug)."""
    basis = f"{scope}/{kind}/{project_key_value or ''}/{slug}"
    return "kb_" + hashlib.sha1(basis.encode("utf-8")).hexdigest()[:12]


def entry_exists(
    root: Path,
    config: dict | None,
    scope: str,
    kind: str,
    project_key_value: str | None,
    slug: str,
) -> bool:
    """Whether a tracked entry already exists at this exact identity."""
    return entry_path_for(root, config, scope, kind, project_key_value, slug) is not None


def entry_path_for(
    root: Path,
    config: dict | None,
    scope: str,
    kind: str,
    project_key_value: str | None,
    slug: str,
) -> Path | None:
    """Path of an existing entry at this exact identity, or None."""
    try:
        target_dir = entry_dir(knowledge_root(root, config), scope, kind, project_key_value)
    except ValueError:
        return None
    path = target_dir / f"{normalize_entry_slug(slug)}.md"
    return path if path.exists() else None


def future_iso(days: int) -> str:
    """An ISO-8601 UTC timestamp ``days`` from now (matches utc_now format)."""
    now = _dt.datetime.now(_dt.timezone.utc).replace(microsecond=0)
    return (now + _dt.timedelta(days=max(0, int(days)))).isoformat()


def list_stale_entries(root: Path, config: dict | None = None, now: str | None = None) -> list[dict]:
    """Entries whose ``review_after`` is at or before now (need re-review).

    ISO-8601 UTC strings in a single format sort lexicographically, so a string
    comparison is a correct chronological comparison here.
    """
    now = now or utc_now()
    stale: list[dict] = []
    for entry in iter_all_entries(root, config):
        review_after = entry.get("meta", {}).get("review_after")
        if review_after and str(review_after) <= now:
            stale.append(entry)
    return stale


def read_entry(path: Path) -> dict:
    path = Path(path)
    meta, body = parse_entry(path.read_text(encoding="utf-8"))
    return {"meta": meta, "body": body, "path": str(path), "size_bytes": _entry_size_bytes(path)}


def read_entry_meta(path: Path) -> dict:
    path = Path(path)
    with path.open("r", encoding="utf-8") as handle:
        first = handle.readline()
        if first.strip() != _FRONTMATTER_FENCE:
            raise ValueError("entry is missing a frontmatter block")
        lines: list[str] = []
        for line in handle:
            if line.strip() == _FRONTMATTER_FENCE:
                return json.loads("".join(lines))
            lines.append(line)
    raise ValueError("entry frontmatter is not terminated")


def read_entry_summary(path: Path) -> dict:
    path = Path(path)
    return {"meta": read_entry_meta(path), "path": str(path), "size_bytes": _entry_size_bytes(path)}


def _entry_size_bytes(path: Path) -> int | None:
    try:
        return Path(path).stat().st_size
    except OSError:
        return None


def list_entries(
    root: Path,
    *,
    config: dict | None,
    scope: str,
    kind: str,
    project_key_value: str | None = None,
) -> list[dict]:
    kb_root = knowledge_root(root, config)
    try:
        target_dir = entry_dir(kb_root, scope, kind, project_key_value)
    except ValueError:
        return []
    if not target_dir.is_dir():
        return []
    entries: list[dict] = []
    for path in _iter_entry_markdown(target_dir):
        try:
            entries.append(read_entry(path))
        except (OSError, ValueError):
            continue
    return entries


def list_entry_summaries(
    root: Path,
    *,
    config: dict | None,
    scope: str,
    kind: str,
    project_key_value: str | None = None,
) -> list[dict]:
    kb_root = knowledge_root(root, config)
    try:
        target_dir = entry_dir(kb_root, scope, kind, project_key_value)
    except ValueError:
        return []
    if not target_dir.is_dir():
        return []
    entries: list[dict] = []
    for path in _iter_entry_markdown(target_dir):
        try:
            entries.append(read_entry_summary(path))
        except (OSError, ValueError):
            continue
    return entries


def iter_entry_summaries(
    root: Path,
    *,
    config: dict | None,
    scope: str,
    kind: str,
    project_key_value: str | None = None,
):
    """Yield tracked entry summaries without reading markdown bodies."""
    kb_root = knowledge_root(root, config)
    try:
        target_dir = entry_dir(kb_root, scope, kind, project_key_value)
    except ValueError:
        return
    if not target_dir.is_dir():
        return
    for path in _iter_entry_markdown(target_dir):
        try:
            yield read_entry_summary(path)
        except (OSError, ValueError):
            continue


def iter_all_entries(root: Path, config: dict | None = None) -> list[dict]:
    """Return every tracked entry across all scopes and valid kinds."""
    kb_root = knowledge_root(root, config)
    results: list[dict] = []
    for kind in entry_kinds_for_scope("general"):
        results.extend(list_entries(root, config=config, scope="general", kind=kind))
    for kind in entry_kinds_for_scope("personal"):
        results.extend(list_entries(root, config=config, scope="personal", kind=kind))
    projects_root = kb_root / PROJECTS_DIR
    if projects_root.is_dir():
        for proj in sorted(p for p in projects_root.iterdir() if p.is_dir()):
            for kind in entry_kinds_for_scope("project"):
                results.extend(
                    list_entries(
                        root, config=config, scope="project", kind=kind,
                        project_key_value=proj.name,
                    )
                )
    return results


def iter_all_entry_summary_records(root: Path, config: dict | None = None):
    """Yield every tracked entry summary without reading markdown bodies."""
    kb_root = knowledge_root(root, config)
    for kind in entry_kinds_for_scope("general"):
        yield from iter_entry_summaries(root, config=config, scope="general", kind=kind)
    for kind in entry_kinds_for_scope("personal"):
        yield from iter_entry_summaries(root, config=config, scope="personal", kind=kind)
    projects_root = kb_root / PROJECTS_DIR
    if projects_root.is_dir():
        for proj in sorted(p for p in projects_root.iterdir() if p.is_dir()):
            for kind in entry_kinds_for_scope("project"):
                yield from iter_entry_summaries(
                    root,
                    config=config,
                    scope="project",
                    kind=kind,
                    project_key_value=proj.name,
                )


def iter_all_entry_summaries(root: Path, config: dict | None = None) -> list[dict]:
    """Return every tracked entry summary without reading markdown bodies."""
    return list(iter_all_entry_summary_records(root, config))


def find_entry(root: Path, config: dict | None, identifier: str) -> dict | None:
    """Find a tracked entry by its meta id or slug."""
    for entry in iter_all_entries(root, config):
        meta = entry.get("meta", {})
        if identifier in (meta.get("id"), meta.get("slug")):
            return entry
    return None


def update_entry(
    root: Path,
    config: dict | None,
    identifier: str,
    *,
    title: str | None = None,
    body: str | None = None,
    tags: list[str] | None = None,
    related_files: list[str] | None = None,
    status: str | None = None,
    review_after: str | None = None,
    invalid_when: str | None = None,
) -> dict:
    """Update a tracked entry while preserving its stable slug/path/id."""
    entry = find_entry(root, config, identifier)
    if entry is None:
        raise FileNotFoundError(f"entry not found: {identifier}")
    meta = dict(entry.get("meta") or {})
    new_title = (title if title is not None else meta.get("title") or "").strip()
    new_body = body if body is not None else entry.get("body", "")
    if tags is not None:
        meta["tags"] = [str(item).strip() for item in tags if str(item).strip()]
    if related_files is not None:
        meta["related_files"] = [str(item).strip() for item in related_files if str(item).strip()]
    if status is not None:
        meta["status"] = str(status).strip() or "active"
    if review_after is not None:
        meta["review_after"] = str(review_after).strip() or None
    if invalid_when is not None:
        meta["invalid_when"] = str(invalid_when).strip() or None
    scope = str(meta.get("scope") or "project")
    kind = kind_for_type(meta.get("type"))
    path = write_entry(
        root,
        config=config,
        scope=scope,
        kind=kind,
        project_key_value=meta.get("project_key"),
        title=new_title,
        body=new_body,
        meta=meta,
        slug=meta.get("slug") or slugify(new_title),
    )
    return read_entry(path)


def delete_entry(root: Path, config: dict | None, identifier: str) -> Path:
    """Delete a tracked entry by id or slug."""
    entry = find_entry(root, config, identifier)
    if entry is None:
        raise FileNotFoundError(f"entry not found: {identifier}")
    path = Path(entry["path"])
    path.unlink()
    return path


def delete_project_navigation(root: Path, config: dict | None, project_key_value: str) -> list[Path]:
    """Delete an entire project's navigation subtree."""
    project_key_value = str(project_key_value or "").strip()
    if not project_key_value:
        raise ValueError("project_key is required")
    nav_dir = entry_dir(knowledge_root(root, config), "project", "navigation", project_key_value)
    if not nav_dir.exists():
        return []
    if not nav_dir.is_dir():
        nav_dir.unlink()
        return [nav_dir]
    deleted = _iter_entry_markdown(nav_dir)
    shutil.rmtree(nav_dir)
    return deleted


def search_entries(root: Path, config: dict | None, query: str) -> list[dict]:
    """Case-insensitive substring search over title, tags, and body."""
    needle = (query or "").strip().lower()
    if not needle:
        return []
    hits: list[dict] = []
    for entry in iter_all_entries(root, config):
        meta = entry.get("meta", {})
        haystack = " ".join(
            [
                str(meta.get("title", "")),
                " ".join(str(t) for t in (meta.get("tags") or [])),
                entry.get("body", ""),
            ]
        ).lower()
        if needle in haystack:
            hits.append(entry)
    return hits


def count_stale_entries(root: Path, config: dict | None = None, now: str | None = None) -> int:
    now = now or utc_now()
    total = 0
    for entry in iter_all_entry_summaries(root, config):
        review_after = entry.get("meta", {}).get("review_after")
        if review_after and str(review_after) <= now:
            total += 1
    return total


# --------------------------------------------------------------------------- #
# Status
# --------------------------------------------------------------------------- #
def _iter_entry_markdown(directory: Path) -> list[Path]:
    if not directory.is_dir():
        return []
    paths: list[Path] = []
    for path in directory.rglob("*.md"):
        try:
            rel = path.relative_to(directory)
        except ValueError:
            continue
        if rel.parts and rel.parts[0] == "assets":
            continue
        paths.append(path)
    return sorted(paths)


def _count_md(directory: Path) -> int:
    return len(_iter_entry_markdown(directory))


def knowledge_status(root: Path, config: dict | None = None) -> dict:
    kb_root = knowledge_root(root, config)
    cfg = knowledge_config(config)
    git_cfg = cfg.get("git", {}) if isinstance(cfg.get("git"), dict) else {}
    project_nav_cfg = cfg.get("project_nav", {}) if isinstance(cfg.get("project_nav"), dict) else {}
    exists = kb_root.exists()
    index_path = _index_path(kb_root)
    schema_version = None
    if index_path.exists():
        try:
            schema_version = read_json(index_path).get("schema_version")
        except (OSError, ValueError):
            schema_version = None

    general = {
        kind: _count_md(kb_root / GENERAL_DIR / kind)
        for kind in entry_kinds_for_scope("general")
    }
    personal = {
        kind: _count_md(kb_root / PERSONAL_DIR / kind)
        for kind in entry_kinds_for_scope("personal")
    }
    projects: list[dict] = []
    projects_root = kb_root / PROJECTS_DIR
    if projects_root.is_dir():
        for proj in sorted(p for p in projects_root.iterdir() if p.is_dir()):
            projects.append(
                {
                    "project_key": proj.name,
                    "counts": {
                        kind: _count_md(proj / kind)
                        for kind in entry_kinds_for_scope("project")
                    },
                }
            )

    return {
        "path": str(kb_root),
        "exists": exists,
        "initialized": index_path.exists(),
        "enabled": bool(cfg.get("enabled")),
        "schema_version": schema_version,
        "git": {
            "enabled": bool(git_cfg.get("enabled")),
            "remote": git_cfg.get("remote"),
            "branch": git_cfg.get("branch"),
            "is_repo": (kb_root / ".git").is_dir(),
        },
        "curation_gate": (cfg.get("curation") or {}).get("gate"),
        "project_nav": {
            "enabled": bool(project_nav_cfg.get("enabled", True)),
            "maintain_during_task": bool(project_nav_cfg.get("maintain_during_task", True)),
        },
        "general": general,
        "personal": personal,
        "projects": projects,
        "pending": len(list_pending(root, config)),
        "stale": count_stale_entries(root, config),
        "total_entries": sum(general.values())
        + sum(personal.values())
        + sum(sum(p["counts"].values()) for p in projects),
    }


# --------------------------------------------------------------------------- #
# Curation queue (candidates awaiting manual approval)
# --------------------------------------------------------------------------- #
def pending_dir(root: Path, config: dict | None = None) -> Path:
    return knowledge_root(root, config) / PENDING_DIR


def _source_group(source: dict | None) -> str:
    source = source or {}
    run_id = str(source.get("run_id") or "").strip()
    task_id = str(source.get("task_id") or "").strip()
    memo_id = str(source.get("memo_id") or "").strip()
    if run_id and task_id:
        return f"{run_id}/task/{task_id}"
    if run_id and memo_id:
        return f"{run_id}/memo/{memo_id}"
    return json.dumps(source, ensure_ascii=False, sort_keys=True)


def _candidate_title_identity_key(title: object) -> str:
    text = " ".join(str(title or "").split()).strip()
    slug = slugify(text)
    if not text:
        return slug
    if any(ord(char) > 127 for char in text):
        digest = hashlib.sha1(text.casefold().encode("utf-8")).hexdigest()[:8]
        return f"{slug or 'title'}-{digest}"
    return slug


def candidate_identity(candidate: dict) -> str:
    """Stable review identity for final/report re-runs and source-order merge."""
    source_group = str(candidate.get("source_group") or _source_group(candidate.get("source")))
    basis = "/".join(
        [
            str(candidate.get("scope") or "project"),
            str(candidate.get("kind") or "solutions"),
            str(candidate.get("project_key") or ""),
            _candidate_title_identity_key(candidate.get("title")),
            source_group,
        ]
    )
    return "cand_" + hashlib.sha1(basis.encode("utf-8")).hexdigest()[:12]


def _legacy_candidate_identity(candidate: dict) -> str:
    source_group = str(candidate.get("source_group") or _source_group(candidate.get("source")))
    basis = "/".join(
        [
            str(candidate.get("scope") or "project"),
            str(candidate.get("kind") or "solutions"),
            str(candidate.get("project_key") or ""),
            slugify(str(candidate.get("title") or "")),
            source_group,
        ]
    )
    return "cand_" + hashlib.sha1(basis.encode("utf-8")).hexdigest()[:12]


def _candidate_fingerprint(record: dict) -> str:
    meta = dict(record.get("meta") or {})
    for volatile in ("source_tasks", "source_memos", "created_at", "updated_at"):
        meta.pop(volatile, None)
    basis = {
        "scope": record.get("scope"),
        "kind": record.get("kind"),
        "project_key": record.get("project_key"),
        "title": record.get("title"),
        "body": record.get("body"),
        "meta": meta,
    }
    return hashlib.sha1(json.dumps(basis, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()[:12]


def _merge_sources(existing: list[dict], source: dict | None) -> list[dict]:
    merged = list(existing or [])
    if not isinstance(source, dict) or not source:
        return merged
    source_key = json.dumps(source, ensure_ascii=False, sort_keys=True)
    known = {json.dumps(item, ensure_ascii=False, sort_keys=True) for item in merged if isinstance(item, dict)}
    if source_key not in known:
        merged.append(source)
    return merged


def candidate_id(title: str, body: str, source: dict | None = None) -> str:
    basis = f"{title}\n{body}\n{json.dumps(source or {}, sort_keys=True)}"
    return "cand_" + hashlib.sha1(basis.encode("utf-8")).hexdigest()[:12]


def enqueue_candidate(root: Path, config: dict | None, candidate: dict) -> Path:
    """Persist a distilled candidate to the manual-review queue.

    Candidates are idempotent by source group + normalized title, not by body.
    That lets task final and linked memo report update one review item even when
    they run in different orders or the same final/report is regenerated.
    """
    target = pending_dir(root, config)
    target.mkdir(parents=True, exist_ok=True)
    record = dict(candidate)
    record.setdefault("kind", "solutions")
    record.setdefault("scope", "project")
    record.setdefault("meta", {})
    record["source_group"] = str(record.get("source_group") or _source_group(record.get("source")))
    explicit_cid = str(record.get("id") or "").strip()
    cid = explicit_cid or candidate_identity(record)
    now = utc_now()
    # Honor an explicit slug (e.g. navigation/index or modules/<name>) so an
    # update is matched against the right on-disk entry, not a title-derived slug.
    entry_slug = str(record.get("slug") or "").strip() or slugify(str(record.get("title") or ""))
    existing_entry = entry_path_for(
        root,
        config,
        str(record.get("scope") or "project"),
        str(record.get("kind") or "solutions"),
        record.get("project_key"),
        entry_slug,
    )
    if existing_entry:
        try:
            existing = read_entry(existing_entry)
            record["action"] = "update"
            record["updates_entry_id"] = existing.get("meta", {}).get("id")
        except (OSError, ValueError):
            pass
    path = target / f"{cid}.json"
    if not explicit_cid and not path.exists():
        legacy_cid = _legacy_candidate_identity(record)
        legacy_path = target / f"{legacy_cid}.json"
        if legacy_cid != cid and legacy_path.exists():
            try:
                legacy_record = read_json(legacy_path)
                if str(legacy_record.get("title") or "") == str(record.get("title") or ""):
                    cid = legacy_cid
                    path = legacy_path
            except (OSError, ValueError):
                pass
    record["id"] = cid
    record["identity"] = cid
    record["fingerprint"] = _candidate_fingerprint(record)
    record["status"] = "pending"
    previous: dict | None = None
    if path.exists():
        try:
            previous = read_json(path)
            record["created_at"] = previous.get("created_at", record.get("created_at") or now)
            record["sources"] = _merge_sources(previous.get("sources") or [], record.get("source"))
            record["updated_from_fingerprint"] = previous.get("fingerprint")
        except (OSError, ValueError):
            pass
    else:
        record["created_at"] = record.get("created_at") or now
        record["sources"] = _merge_sources(record.get("sources") or [], record.get("source"))
    previous_fingerprint = previous.get("fingerprint") if previous else None
    if previous and previous_fingerprint == record.get("fingerprint"):
        record["updated_at"] = previous.get("updated_at") or previous.get("created_at") or now
    else:
        record["updated_at"] = now
    record["last_seen_at"] = now
    write_json(path, record)
    return path


def list_pending(root: Path, config: dict | None = None) -> list[dict]:
    target = pending_dir(root, config)
    if not target.is_dir():
        return []
    records: list[dict] = []
    for path in sorted(target.glob("*.json")):
        try:
            record = read_json(path)
            record["_path"] = str(path)
            records.append(record)
        except (OSError, ValueError):
            continue
    records.sort(
        key=lambda item: str(item.get("updated_at") or item.get("last_seen_at") or item.get("created_at") or ""),
        reverse=True,
    )
    return records


def auto_commit_message_for(candidates: list[dict]) -> str:
    if len(candidates) == 1:
        kind = candidates[0].get("kind") or "entry"
        return f"chore(knowledge): add {kind} '{candidates[0].get('title', 'entry')}'"
    return f"chore(knowledge): add {len(candidates)} distilled entries"


def read_pending(path: Path) -> dict:
    record = read_json(Path(path))
    record["_path"] = str(path)
    return record


def remove_pending(root: Path, config: dict | None, candidate_id_value: str) -> bool:
    path = pending_dir(root, config) / f"{candidate_id_value}.json"
    if path.exists():
        path.unlink()
        return True
    return False


_TASK_MEMO_IMAGE_LINK_RE = re.compile(r"!\[([^\]\n]*)\]\(([^)\s]+)(?:\s+['\"][^)]*['\"])?\)")


def _task_memo_asset_name_from_markdown_path(path_text: str) -> str:
    clean = str(path_text or "").strip().split("#", 1)[0].split("?", 1)[0]
    if clean.startswith("task_memo_assets/"):
        return clean.removeprefix("task_memo_assets/")
    if clean.startswith("/api/task-memo-assets/"):
        return unquote(clean.removeprefix("/api/task-memo-assets/").strip("/"))
    return ""


def _merge_entry_assets(existing: object, additions: list[dict]) -> list[dict]:
    assets = [dict(item) for item in existing or [] if isinstance(item, dict)]
    seen = {str(item.get("path") or "") for item in assets}
    for item in additions:
        path = str(item.get("path") or "")
        if path and path not in seen:
            assets.append(dict(item))
            seen.add(path)
    return assets


def _entry_asset_target_name(dest: Path, source_name: str, data: bytes) -> str:
    leaf = Path(source_name).name.strip() or "image"
    target = dest / leaf
    if not target.exists():
        return leaf
    try:
        if target.read_bytes() == data:
            return leaf
    except OSError:
        pass
    return f"{hashlib.sha256(data).hexdigest()[:8]}-{leaf}"


def _promote_task_memo_assets_for_entry(
    root: Path,
    config: dict | None,
    candidate: dict,
    body: str,
    *,
    scope: str,
    kind: str,
    project_key: str | None,
    slug: str,
) -> dict | None:
    source = candidate.get("source") if isinstance(candidate.get("source"), dict) else {}
    run_id = str(source.get("run_id") or candidate.get("run_id") or "").strip()
    if not run_id or ("task_memo_assets/" not in body and "/api/task-memo-assets/" not in body):
        return None

    try:
        from aha_cli.store.task_memo_assets import read_task_memo_asset

        dest = entry_dir(knowledge_root(root, config), scope, kind, project_key) / "assets" / slug
    except (ImportError, ValueError):
        return None
    dest.mkdir(parents=True, exist_ok=True)

    assets: list[dict] = []
    promoted: dict[str, str] = {}

    def replace(match: re.Match[str]) -> str:
        alt = match.group(1)
        raw_path = match.group(2)
        asset_name = _task_memo_asset_name_from_markdown_path(raw_path)
        if not asset_name:
            return match.group(0)
        if asset_name in promoted:
            return f"![{alt}]({promoted[asset_name]})"
        try:
            data, mime, safe_name = read_task_memo_asset(root, run_id, asset_name)
        except (FileNotFoundError, OSError, ValueError):
            return match.group(0)
        if not str(mime or "").startswith("image/"):
            return match.group(0)
        name = _entry_asset_target_name(dest, safe_name, data)
        target = dest / name
        if not target.exists():
            target.write_bytes(data)
        rel = f"assets/{slug}/{name}"
        promoted[asset_name] = rel
        assets.append({
            "name": name,
            "original": Path(safe_name).name,
            "mime": mime,
            "size": len(data),
            "path": rel,
        })
        return f"![{alt}]({rel})"

    updated_body = _TASK_MEMO_IMAGE_LINK_RE.sub(replace, body)
    if not assets:
        return None
    return {"body": updated_body, "assets": assets}


def approve_candidate(root: Path, config: dict | None, candidate_id_value: str) -> Path:
    """Promote a pending candidate into a tracked knowledge entry, then dequeue it."""
    path = pending_dir(root, config) / f"{candidate_id_value}.json"
    if not path.exists():
        raise FileNotFoundError(f"no pending candidate: {candidate_id_value}")
    record = read_json(path)
    scope = record.get("scope", "project")
    kind = record.get("kind", "solutions")
    project_key_value = record.get("project_key")
    body = record.get("body", "")
    meta = dict(record.get("meta") or {})
    slug = normalize_entry_slug(record.get("slug")) if record.get("slug") else slugify(str(record.get("title") or ""))
    source = record.get("source") if isinstance(record.get("source"), dict) else {}
    source_note_id = str(record.get("source_note_id") or source.get("note_id") or "").strip()
    if source_note_id:
        meta.setdefault("source_note_id", source_note_id)

    # Phase 5b: if this candidate came from a capture note, copy that note's
    # image assets into the entry's assets dir and leave a traceable reference.
    # Copy-only here — committing/pushing stays with the existing sync mechanism.
    try:
        from aha_cli.store.knowledge_capture import promote_assets_for_entry

        promo = promote_assets_for_entry(
            root, config, record, scope=scope, kind=kind, project_key=project_key_value, slug=slug,
        )
    except Exception:  # noqa: BLE001 - asset promotion must never block approval
        promo = None
    if promo:
        if promo["body_suffix"] not in body:
            body = body.rstrip() + promo["body_suffix"]
        meta["source_note_id"] = promo["source_note_id"]
        meta["assets"] = _merge_entry_assets(meta.get("assets"), promo["assets"])

    task_memo_promo = _promote_task_memo_assets_for_entry(
        root, config, record, body, scope=scope, kind=kind, project_key=project_key_value, slug=slug,
    )
    if task_memo_promo:
        body = task_memo_promo["body"]
        meta["assets"] = _merge_entry_assets(meta.get("assets"), task_memo_promo["assets"])

    entry_path = write_entry_preserving_navigation(
        root,
        config=config,
        scope=scope,
        kind=kind,
        project_key_value=project_key_value,
        title=record["title"],
        body=body,
        meta=meta,
        slug=slug,
    )
    path.unlink()
    return entry_path
