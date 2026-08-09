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
from aha_cli.store.paths import config_path

_agents_lock = threading.Lock()

PERMISSION_FIELDS = {"default_scope", "allowed_topics", "handoff_always", "read_paths"}

_GROUP_KEY = "group_digital_human"


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
        default_scope = str(merged.get("default_scope") or "").strip() or "public_knowledge"
        merged["default_scope"] = default_scope
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
