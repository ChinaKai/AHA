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
from pathlib import Path

from aha_cli.domain.models import default_knowledge_config, utc_now
from aha_cli.store.io import read_json, write_json
from aha_cli.store.paths import default_knowledge_dir

KNOWLEDGE_SCHEMA_VERSION = 1
KNOWLEDGE_INDEX_FILE = "aha-knowledge.json"
KNOWLEDGE_README_FILE = "README.md"
KNOWLEDGE_GITIGNORE_FILE = ".gitignore"
GENERAL_DIR = "general"
PROJECTS_DIR = "projects"
PENDING_DIR = ".pending"
ENTRY_KINDS = ("wiki", "solutions")
SCOPES = ("general", "project")

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

    Priority: git origin remote hash (stable across machines/paths), else a
    slug built from the run goal and workspace directory name. The fallback
    deliberately excludes the absolute path so the same project resolves to the
    same key after being moved or cloned to a different location/machine.
    """
    workspace = Path(workspace).expanduser()
    remote = normalize_git_remote(_git_remote_for(workspace))
    if remote:
        digest = hashlib.sha1(remote.encode("utf-8")).hexdigest()[:12]
        return f"git-{digest}"
    basis = "-".join(part for part in [(goal or "").strip(), workspace.name] if part)
    if not basis:
        basis = "workspace"
    # Digest over the migratable basis (goal + dir name), never the absolute path.
    digest = hashlib.sha1(basis.encode("utf-8")).hexdigest()[:8]
    return f"ws-{slugify(basis)}-{digest}"


# --------------------------------------------------------------------------- #
# Layout
# --------------------------------------------------------------------------- #
def scope_dir(kb_root: Path, scope: str, project_key_value: str | None) -> Path:
    if scope == "general":
        return kb_root / GENERAL_DIR
    if scope == "project":
        if not project_key_value:
            raise ValueError("project scope requires a project_key")
        return kb_root / PROJECTS_DIR / project_key_value
    raise ValueError(f"unknown scope: {scope!r}")


def entry_dir(kb_root: Path, scope: str, kind: str, project_key_value: str | None) -> Path:
    if kind not in ENTRY_KINDS:
        raise ValueError(f"unknown entry kind: {kind!r}")
    return scope_dir(kb_root, scope, project_key_value) / kind


def init_knowledge_base(root: Path, config: dict | None = None) -> dict:
    """Create the knowledge base skeleton. Idempotent."""
    kb_root = knowledge_root(root, config)
    created = not kb_root.exists()
    for kind in ENTRY_KINDS:
        (kb_root / GENERAL_DIR / kind).mkdir(parents=True, exist_ok=True)
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

    # Unreviewed candidates live in .pending/ and must never be committed/pushed.
    gitignore = kb_root / KNOWLEDGE_GITIGNORE_FILE
    if not gitignore.exists():
        gitignore.write_text(f"{PENDING_DIR}/\n", encoding="utf-8")

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
        "- `general/wiki`, `general/solutions` — cross-project knowledge\n"
        "- `projects/<project-key>/wiki`, `.../solutions` — per-project knowledge\n\n"
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
    entry_slug = slug or slugify(title)
    path = target_dir / f"{entry_slug}.md"

    now = utc_now()
    full_meta = dict(meta or {})
    full_meta.setdefault("type", "wiki" if kind == "wiki" else "solution")
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
    path = target_dir / f"{slug}.md"
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
    meta, body = parse_entry(Path(path).read_text(encoding="utf-8"))
    return {"meta": meta, "body": body, "path": str(path)}


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
    for path in sorted(target_dir.glob("*.md")):
        try:
            entries.append(read_entry(path))
        except (OSError, ValueError):
            continue
    return entries


def iter_all_entries(root: Path, config: dict | None = None) -> list[dict]:
    """Return every tracked entry across general + all projects, both kinds."""
    kb_root = knowledge_root(root, config)
    results: list[dict] = []
    for kind in ENTRY_KINDS:
        results.extend(list_entries(root, config=config, scope="general", kind=kind))
    projects_root = kb_root / PROJECTS_DIR
    if projects_root.is_dir():
        for proj in sorted(p for p in projects_root.iterdir() if p.is_dir()):
            for kind in ENTRY_KINDS:
                results.extend(
                    list_entries(
                        root, config=config, scope="project", kind=kind,
                        project_key_value=proj.name,
                    )
                )
    return results


def find_entry(root: Path, config: dict | None, identifier: str) -> dict | None:
    """Find a tracked entry by its meta id or slug."""
    for entry in iter_all_entries(root, config):
        meta = entry.get("meta", {})
        if identifier in (meta.get("id"), meta.get("slug")):
            return entry
    return None


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


# --------------------------------------------------------------------------- #
# Status
# --------------------------------------------------------------------------- #
def _count_md(directory: Path) -> int:
    return sum(1 for _ in directory.glob("*.md")) if directory.is_dir() else 0


def knowledge_status(root: Path, config: dict | None = None) -> dict:
    kb_root = knowledge_root(root, config)
    cfg = knowledge_config(config)
    git_cfg = cfg.get("git", {}) if isinstance(cfg.get("git"), dict) else {}
    exists = kb_root.exists()
    index_path = _index_path(kb_root)
    schema_version = None
    if index_path.exists():
        try:
            schema_version = read_json(index_path).get("schema_version")
        except (OSError, ValueError):
            schema_version = None

    general = {kind: _count_md(kb_root / GENERAL_DIR / kind) for kind in ENTRY_KINDS}
    projects: list[dict] = []
    projects_root = kb_root / PROJECTS_DIR
    if projects_root.is_dir():
        for proj in sorted(p for p in projects_root.iterdir() if p.is_dir()):
            projects.append(
                {
                    "project_key": proj.name,
                    "counts": {kind: _count_md(proj / kind) for kind in ENTRY_KINDS},
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
        "general": general,
        "projects": projects,
        "pending": len(list_pending(root, config)),
        "stale": len(list_stale_entries(root, config)),
        "total_entries": sum(general.values())
        + sum(sum(p["counts"].values()) for p in projects),
    }


# --------------------------------------------------------------------------- #
# Curation queue (candidates awaiting manual approval)
# --------------------------------------------------------------------------- #
def pending_dir(root: Path, config: dict | None = None) -> Path:
    return knowledge_root(root, config) / PENDING_DIR


def candidate_id(title: str, body: str, source: dict | None = None) -> str:
    basis = f"{title}\n{body}\n{json.dumps(source or {}, sort_keys=True)}"
    return "cand_" + hashlib.sha1(basis.encode("utf-8")).hexdigest()[:12]


def enqueue_candidate(root: Path, config: dict | None, candidate: dict) -> Path:
    """Persist a distilled candidate to the manual-review queue. Idempotent by id."""
    target = pending_dir(root, config)
    target.mkdir(parents=True, exist_ok=True)
    record = dict(candidate)
    record.setdefault("kind", "solutions")
    record.setdefault("scope", "project")
    record.setdefault("meta", {})
    cid = record.get("id") or candidate_id(
        record.get("title", ""), record.get("body", ""), record.get("source")
    )
    record["id"] = cid
    record["status"] = "pending"
    record.setdefault("created_at", utc_now())
    path = target / f"{cid}.json"
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
    return records


def auto_commit_message_for(candidates: list[dict]) -> str:
    if len(candidates) == 1:
        return f"chore(knowledge): add solution '{candidates[0].get('title', 'entry')}'"
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


def approve_candidate(root: Path, config: dict | None, candidate_id_value: str) -> Path:
    """Promote a pending candidate into a tracked knowledge entry, then dequeue it."""
    path = pending_dir(root, config) / f"{candidate_id_value}.json"
    if not path.exists():
        raise FileNotFoundError(f"no pending candidate: {candidate_id_value}")
    record = read_json(path)
    entry_path = write_entry(
        root,
        config=config,
        scope=record.get("scope", "project"),
        kind=record.get("kind", "solutions"),
        project_key_value=record.get("project_key"),
        title=record["title"],
        body=record.get("body", ""),
        meta=record.get("meta", {}),
    )
    path.unlink()
    return entry_path
