from __future__ import annotations

from collections import Counter, deque
from pathlib import Path

from aha_cli.domain.models import is_feishu_group_task, resolve_group_digital_human_permissions
from aha_cli.services.prompt_templates import render_prompt_template
from aha_cli.store.config import load_config
from aha_cli.store.event_views import event_agent_refs
from aha_cli.store.filesystem import iter_jsonl_reverse
from aha_cli.store.knowledge import iter_all_entry_summary_records, knowledge_config, knowledge_root
from aha_cli.store.paths import event_path
from aha_cli.store.workspaces import list_workspaces

MAX_KB_ENTRY_LINES = 40
MAX_WORKSPACE_ROOTS = 8
MAX_PROJECT_CANDIDATES_PER_ROOT = 35
MAX_PROJECT_SCAN_DIRS_PER_ROOT = 300
MAX_RECENT_GROUP_MESSAGES = 8
PROJECT_SCAN_MAX_DEPTH = 2
MAX_LINE_CHARS = 260

EXCLUDED_DIR_NAMES = {
    ".aha",
    ".cache",
    ".git",
    ".hg",
    ".idea",
    ".mypy_cache",
    ".next",
    ".pytest_cache",
    ".svn",
    ".tox",
    ".venv",
    ".vscode",
    "__pycache__",
    "build",
    "dist",
    "node_modules",
    "out",
    "target",
    "venv",
}
PROJECT_MARKER_FILES = (
    "README.md",
    "readme.md",
    "README",
    "pyproject.toml",
    "package.json",
    "pnpm-workspace.yaml",
    "yarn.lock",
    "Cargo.toml",
    "go.mod",
    "pom.xml",
    "build.gradle",
    "settings.gradle",
    "CMakeLists.txt",
    "Makefile",
    "setup.py",
    "requirements.txt",
)
PROJECT_MARKER_DIRS = ("docs", "src", "tests", "app", "apps", "packages")


def _clip(value: object, limit: int = MAX_LINE_CHARS) -> str:
    text = " ".join(str(value or "").split())
    if len(text) <= limit:
        return text
    return text[: limit - 3].rstrip() + "..."


def _safe_tags(value: object) -> str:
    if not isinstance(value, list):
        return ""
    tags = [_clip(item, 32) for item in value if str(item or "").strip()]
    return ", ".join(tags[:6])


def _relative_or_absolute(path: Path, base: Path | None) -> str:
    if base is not None:
        try:
            return path.relative_to(base).as_posix()
        except ValueError:
            pass
    return str(path)


def _kb_index_lines(root: Path, config: dict) -> list[str]:
    cfg = knowledge_config(config)
    kb_root = knowledge_root(root, config)
    if not cfg.get("enabled"):
        return ["AHA Knowledge Base index: disabled"]
    entries = list(iter_all_entry_summary_records(root, config))
    entries.sort(
        key=lambda item: str((item.get("meta") or {}).get("updated_at") or ""),
        reverse=True,
    )
    counts = Counter(
        (
            str((entry.get("meta") or {}).get("scope") or "-"),
            str((entry.get("meta") or {}).get("type") or "-"),
        )
        for entry in entries
    )
    lines = [
        "AHA Knowledge Base index:",
        f"- kb_root: {kb_root}",
        f"- entry_count: {len(entries)}",
    ]
    if counts:
        count_text = ", ".join(f"{scope}/{kind}={count}" for (scope, kind), count in sorted(counts.items()))
        lines.append(f"- counts: {count_text}")
    for index, entry in enumerate(entries[:MAX_KB_ENTRY_LINES], start=1):
        meta = entry.get("meta") if isinstance(entry.get("meta"), dict) else {}
        entry_path = _relative_or_absolute(Path(str(entry.get("path") or "")), kb_root)
        parts = [
            f"{index}. [{meta.get('scope') or '-'}:{meta.get('type') or '-'}]",
            _clip(meta.get("title") or meta.get("slug") or "(untitled)", 96),
            f"path={entry_path}",
        ]
        if meta.get("project_key"):
            parts.append(f"project={_clip(meta.get('project_key'), 48)}")
        if meta.get("slug"):
            parts.append(f"slug={_clip(meta.get('slug'), 72)}")
        tags = _safe_tags(meta.get("tags"))
        if tags:
            parts.append(f"tags={tags}")
        lines.append("- " + " | ".join(parts))
    if len(entries) > MAX_KB_ENTRY_LINES:
        lines.append(f"- truncated: showing {MAX_KB_ENTRY_LINES} of {len(entries)} entries; inspect kb_root for the full index.")
    return lines


def _project_markers(path: Path) -> list[str]:
    markers: list[str] = []
    for name in PROJECT_MARKER_FILES:
        if (path / name).is_file():
            markers.append(name)
    for name in PROJECT_MARKER_DIRS:
        if (path / name).is_dir():
            markers.append(f"{name}/")
    return markers[:8]


def _excluded_dir(path: Path) -> bool:
    name = path.name
    return name in EXCLUDED_DIR_NAMES or (name.startswith(".") and name not in {".github"})


def _project_candidates(root_path: Path) -> tuple[list[dict], bool]:
    candidates: list[dict] = []
    if not root_path.is_dir():
        return candidates, False
    scanned = 0
    truncated = False
    queue: deque[tuple[Path, int]] = deque([(root_path, 0)])
    seen: set[Path] = set()
    while queue and len(candidates) < MAX_PROJECT_CANDIDATES_PER_ROOT:
        current, depth = queue.popleft()
        try:
            resolved = current.resolve()
        except OSError:
            continue
        if resolved in seen:
            continue
        seen.add(resolved)
        if current != root_path and _excluded_dir(current):
            continue
        scanned += 1
        if scanned > MAX_PROJECT_SCAN_DIRS_PER_ROOT:
            truncated = True
            break
        markers = _project_markers(current)
        if current == root_path or markers or depth == 1:
            candidates.append(
                {
                    "path": current,
                    "markers": markers,
                    "depth": depth,
                }
            )
        if depth >= PROJECT_SCAN_MAX_DEPTH:
            continue
        try:
            children = sorted(
                [child for child in current.iterdir() if child.is_dir() and not _excluded_dir(child)],
                key=lambda item: item.name.lower(),
            )
        except OSError:
            children = []
        for child in children:
            queue.append((child, depth + 1))
    if queue:
        truncated = True
    return candidates, truncated


def _workspace_root_lines(root: Path, config: dict) -> list[str]:
    configured_roots = [str(item).strip() for item in (config.get("workspace_roots") or []) if str(item).strip()]
    registered = []
    try:
        registered = list_workspaces(root)
    except (Exception, SystemExit):
        registered = []
    registered_paths = [str(item.get("path") or "").strip() for item in registered if str(item.get("path") or "").strip()]
    all_roots: list[str] = []
    for value in [*configured_roots, *registered_paths]:
        if value and value not in all_roots:
            all_roots.append(value)
    if not all_roots:
        return ["Workspace source index: no workspace_roots or registered workspaces configured"]
    lines = [
        "Workspace source index:",
        "- Roots are internal read-only lookup entrypoints. Do not reveal raw absolute paths in public group replies.",
        f"- root_count: {len(all_roots)}",
    ]
    for root_index, raw_path in enumerate(all_roots[:MAX_WORKSPACE_ROOTS], start=1):
        path = Path(raw_path).expanduser()
        exists = path.exists()
        lines.append(f"{root_index}. root={path} ({'exists' if exists else 'missing'})")
        if not exists:
            continue
        candidates, truncated = _project_candidates(path)
        for candidate in candidates:
            markers = ", ".join(candidate["markers"]) if candidate["markers"] else "-"
            label = "root" if candidate["depth"] == 0 else "project"
            lines.append(f"   - {label}: {candidate['path']} | markers={markers}")
        if truncated:
            lines.append(f"   - truncated: showing up to {MAX_PROJECT_CANDIDATES_PER_ROOT} candidate paths from this root")
    if len(all_roots) > MAX_WORKSPACE_ROOTS:
        lines.append(f"- truncated_roots: showing {MAX_WORKSPACE_ROOTS} of {len(all_roots)} roots")
    return lines


def _recent_group_context_lines(root: Path, run_id: str, task_id: str) -> list[str]:
    messages: list[dict] = []
    try:
        iterator = iter_jsonl_reverse(event_path(root, run_id))
    except (Exception, SystemExit):
        iterator = []
    for _offset, event in iterator or ():
        data = event.get("data") if isinstance(event.get("data"), dict) else {}
        if event.get("type") != "message":
            continue
        if str(data.get("task_id") or "") != str(task_id or ""):
            continue
        if "main" not in event_agent_refs(event):
            continue
        if str(data.get("feishu_channel") or "") != "group_digital_human":
            continue
        text = str(data.get("feishu_original_text") or data.get("message") or "").strip()
        if not text:
            continue
        messages.append(
            {
                "ts": str(data.get("ts") or event.get("ts") or ""),
                "chat_type": str(data.get("feishu_chat_type") or ""),
                "text": _clip(text, 180),
                "attachments": data.get("feishu_attachments") if isinstance(data.get("feishu_attachments"), list) else [],
            }
        )
        if len(messages) >= MAX_RECENT_GROUP_MESSAGES:
            break
    if not messages:
        return ["Recent group @ context: none available"]
    lines = [
        "Recent group @ context for this digital-human user:",
        "- Only messages delivered to AHA are listed. Non-@ group messages are not available unless Feishu history fetching is added.",
    ]
    for index, message in enumerate(reversed(messages), start=1):
        attachment_count = len(message["attachments"])
        suffix = f" | attachments={attachment_count}" if attachment_count else ""
        lines.append(f"- {index}. {message['ts'] or '-'} {message['chat_type'] or '-'}: {message['text']}{suffix}")
    return lines


def feishu_group_source_index_context(root: Path, run_id: str, task: dict | None) -> str:
    if not is_feishu_group_task(task):
        return ""
    try:
        config = load_config(root)
        task_id = str((task or {}).get("id") or "")
        permissions = resolve_group_digital_human_permissions(config)
        allowed_topics = ", ".join(str(item) for item in permissions.get("allowed_topics") or []) or "none"
        # An explicitly empty handoff_always list means "no additional topics
        # beyond the baseline" — the baseline handoff triggers (execution,
        # commitment, permissions, private/secrets) live in the identity
        # template and still apply. Do not re-inject them here, otherwise an
        # empty list could not express "turn off extra forced handoffs".
        handoff_always = ", ".join(str(item) for item in permissions.get("handoff_always") or []) or "none (baseline handoff rules from the identity template still apply)"
        permission_context = render_prompt_template(
            "feishu_group_digital_human_permission.md",
            default_scope=str(permissions.get("default_scope") or "public_knowledge"),
            allowed_topics=allowed_topics,
            handoff_always=handoff_always,
        ).rstrip()
        read_paths = [str(item) for item in permissions.get("read_paths") or [] if str(item or "").strip()]
        if read_paths:
            # read_paths allowlist: give only the paths, not enumerated content.
            # Everything under each path is readable; do not enumerate specific
            # KB entries or workspace projects.
            path_lines = [f"- {item}" for item in read_paths]
            lines = [
                "Digital-human information source index:",
                "- Readable paths allowlist (read_paths): you may read all files and subdirectories under these paths.",
                *path_lines,
                "- Only read files under the listed paths. Do not attempt to read or reference paths outside this allowlist.",
                "- If an answer can be supported by common knowledge or files under the readable paths, answer publicly and concisely.",
                "- If the answer depends on private source details, secrets, owner judgment, execution, authorization, or falls outside the readable paths, hand off to the owner.",
                "",
                permission_context,
                "",
                *_recent_group_context_lines(root, run_id, task_id),
            ]
            return "\n".join(lines).strip()
        lines = [
            "Digital-human information source index:",
            "- This is an index, not full source content. Use it to choose minimal KB entries or workspace files to inspect before answering.",
            "- The digital human may read all indexed sources, but must not directly publish secrets, credentials, irreversible-operation guidance, private task content, or uncertain internal details to the group.",
            "- If an answer can be supported by common knowledge, KB, docs, README, or clearly public project material within the permission scope, answer publicly and concisely.",
            "- If the answer depends on private source details, secrets, owner judgment, execution, authorization, or falls outside the permission scope, hand off to the owner.",
            "",
            permission_context,
            "",
            *_kb_index_lines(root, config),
            "",
            *_workspace_root_lines(root, config),
            "",
            *_recent_group_context_lines(root, run_id, task_id),
        ]
        return "\n".join(lines).strip()
    except (Exception, SystemExit):
        return "Digital-human information source index: unavailable"


__all__ = ["feishu_group_source_index_context"]
