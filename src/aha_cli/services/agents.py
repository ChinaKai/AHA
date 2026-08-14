"""Channel-agnostic agent identity configuration.

Agent identities (group digital human, service steward) are configured in the
top-level ``agents`` section of ``config.json``. Permissions stored here are
identity-level: what an agent may answer directly vs. must hand off. Channel
access control stays with each channel integration config.
"""
from __future__ import annotations

import threading
from pathlib import Path

from aha_cli.domain.models import (
    default_agents_config,
    normalize_agents_config,
    resolve_group_digital_human_permissions,
)
from aha_cli.store.io import read_json, write_json
from aha_cli.store.knowledge import knowledge_root
from aha_cli.store.paths import aha_home_path, config_path
from aha_cli.store.workspaces import list_workspaces

_agents_lock = threading.Lock()

PERMISSION_FIELDS = {"read_paths", "allow_common_knowledge", "allowed_topics", "handoff_always"}

_GROUP_KEY = "group_digital_human"


def read_path_candidates(root: Path) -> list[dict]:
    """Auto-detected candidate paths the digital human may read.

    Returned to the permissions UI so it can offer known paths (AHA KB root,
    configured workspace roots, registered workspaces, digital-human
    workspace) as selectable options alongside custom paths.
    """
    from aha_cli.store.config import load_config

    config = load_config(root)
    candidates: list[dict] = []
    try:
        kb_root = knowledge_root(root, config)
        resolved_kb = str(kb_root.resolve())
        candidates.append({"type": "kb", "path": resolved_kb, "label": f"AHA Knowledge Base · {resolved_kb}"})
    except (Exception, SystemExit):
        pass
    from aha_cli.store.ws_target import host_native_path

    seen: set[str] = set()
    for raw in config.get("workspace_roots") or []:
        text = str(raw or "").strip()
        if not text:
            continue
        try:
            resolved = str(Path(host_native_path(text, aha_home=root)).resolve())
        except OSError:
            continue
        if resolved not in seen:
            seen.add(resolved)
            candidates.append({"type": "workspace", "path": resolved, "label": f"Workspace root · {resolved}"})
    try:
        registered = list_workspaces(root)
    except (Exception, SystemExit):
        registered = []
    for item in registered:
        raw = str(item.get("path") or "").strip()
        if not raw:
            continue
        try:
            resolved = str(Path(host_native_path(raw, aha_home=root)).resolve())
        except OSError:
            continue
        if resolved not in seen:
            seen.add(resolved)
            candidates.append({"type": "workspace", "path": resolved, "label": f"Registered workspace · {resolved}"})
    dh_dir = (aha_home_path(root) / "feishu_group_state").resolve()
    resolved_dh = str(dh_dir)
    candidates.append({"type": "digital_human", "path": resolved_dh, "label": f"Digital human workspace · {resolved_dh}"})
    return candidates


def group_digital_human_permissions(root: Path) -> dict:
    """Resolved identity permissions for the group digital human."""
    return resolve_group_digital_human_permissions(__load_config(root))


def update_group_digital_human_permissions(root: Path, payload: dict) -> dict:
    """Persist identity permissions for the group digital human.

    Writes the canonical ``agents.group_digital_human.permissions`` block and
    mirrors it to the legacy ``integrations.feishu.group_permissions`` location
    so older readers stay consistent during the migration.
    """
    if not isinstance(payload, dict):
        raise ValueError("Digital human permissions must be a JSON object")
    accepted = {key: value for key, value in payload.items() if key in PERMISSION_FIELDS}
    if not accepted:
        raise ValueError("No supported permission fields provided")
    path = config_path(root)
    with _agents_lock:
        try:
            config = read_json(path)
        except (FileNotFoundError, OSError, ValueError):
            config = {}
        if not isinstance(config, dict):
            raise ValueError("AHA config must be a JSON object")
        current = resolve_group_digital_human_permissions(config)
        merged = {**current, **accepted}
        merged["read_paths"] = merged.get("read_paths") or []
        merged["allow_common_knowledge"] = bool(merged.get("allow_common_knowledge"))
        merged["allowed_topics"] = merged.get("allowed_topics") or []
        merged["handoff_always"] = merged.get("handoff_always") or []
        normalized = normalize_agents_config(
            {"group_digital_human": {"permissions": merged}}
        )["group_digital_human"]["permissions"]

        agents = config.get("agents")
        agents = dict(agents) if isinstance(agents, dict) else {}
        group = agents.get(_GROUP_KEY)
        group = dict(group) if isinstance(group, dict) else {}
        group["permissions"] = normalized
        agents[_GROUP_KEY] = group
        config["agents"] = agents

        # Mirror to the legacy location so pre-migration readers stay consistent.
        integrations = config.get("integrations")
        integrations = dict(integrations) if isinstance(integrations, dict) else {}
        feishu = integrations.get("feishu")
        feishu = dict(feishu) if isinstance(feishu, dict) else {}
        feishu["group_permissions"] = dict(normalized)
        integrations["feishu"] = feishu
        config["integrations"] = integrations

        write_json(path, config)
    return dict(normalized)


def __load_config(root: Path) -> dict:
    from aha_cli.store.config import load_config

    return load_config(root)


__all__ = [
    "PERMISSION_FIELDS",
    "group_digital_human_permissions",
    "update_group_digital_human_permissions",
    "default_agents_config",
]
