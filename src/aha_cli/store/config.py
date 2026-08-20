from __future__ import annotations

import os
from pathlib import Path

from aha_cli import platform
from aha_cli.domain.models import (
    default_config,
    normalize_agents_config_from_loaded,
    normalize_integrations_config,
)
from aha_cli.services.provider_config import normalize_configured_models, normalize_providers, sync_legacy_backend_env
from aha_cli.services.proxy import normalize_proxy_config, proxy_configured
from aha_cli.store.io import read_json
from aha_cli.store.paths import config_path


def _loaded_proxy(value: object) -> dict[str, object]:
    raw = value if isinstance(value, dict) else {}
    return normalize_proxy_config(
        raw.get("enabled", raw.get("proxy_enabled", False)),
        raw.get("http_proxy"),
        raw.get("https_proxy"),
        raw.get("no_proxy"),
    )


def _legacy_group_digital_human_permissions(loaded: dict) -> dict:
    """Return the legacy ``integrations.feishu.group_permissions`` block if any.

    The ``agents`` section is the canonical location for identity permissions;
    this helper supplies the pre-migration fallback so existing configurations
    keep working until they are written through the new location.
    """
    integrations = loaded.get("integrations")
    if not isinstance(integrations, dict):
        return {}
    feishu = integrations.get("feishu")
    if not isinstance(feishu, dict):
        return {}
    permissions = feishu.get("group_permissions")
    return permissions if isinstance(permissions, dict) else {}


def _shared_proxy_config(defaults: dict, loaded: dict) -> dict:
    shared = defaults | _loaded_proxy(loaded.get("proxy"))
    if proxy_configured(shared):
        return shared
    selected = str(loaded.get("backend") or "").strip().lower()
    backend_order = [selected] if selected in {"codex", "claude"} else []
    backend_order.extend(name for name in ("codex", "claude") if name not in backend_order)
    for name in backend_order:
        section = loaded.get(name) if isinstance(loaded.get(name), dict) else {}
        candidate = _loaded_proxy(section.get("proxy"))
        if proxy_configured(candidate):
            return defaults | candidate
    return shared


def _merge_backend_config(defaults: dict, loaded: dict, shared_proxy: dict) -> dict:
    cfg = defaults | loaded
    loaded_proxy = loaded.get("proxy") if isinstance(loaded.get("proxy"), dict) else {}
    fallback_enabled = shared_proxy.get("enabled", defaults["proxy"].get("enabled", False))
    enabled = loaded_proxy.get(
        "enabled",
        loaded_proxy.get("proxy_enabled", fallback_enabled),
    )
    cfg["proxy"] = {"enabled": normalize_proxy_config(enabled).get("enabled", False)}
    return cfg


def _merge_knowledge_config(defaults: dict, loaded: dict) -> dict:
    if not isinstance(loaded, dict):
        return {key: (dict(value) if isinstance(value, dict) else value) for key, value in defaults.items()}
    nested_keys = ("git", "curation", "project_nav", "agent", "retrieval")
    cfg = defaults | {key: value for key, value in loaded.items() if key not in set(nested_keys)}
    for nested in nested_keys:
        loaded_nested = loaded.get(nested, {})
        cfg[nested] = defaults[nested] | (loaded_nested if isinstance(loaded_nested, dict) else {})
    return cfg


def load_config(root: Path) -> dict:
    defaults = default_config()
    path = config_path(root)
    if not path.exists():
        return defaults
    loaded = read_json(path)
    cfg = defaults | {key: value for key, value in loaded.items() if key not in {"codex", "claude", "integrations"}}
    cfg["proxy"] = _shared_proxy_config(defaults["proxy"], loaded)
    cfg["codex"] = _merge_backend_config(defaults["codex"], loaded.get("codex", {}), cfg["proxy"])
    cfg["claude"] = _merge_backend_config(defaults["claude"], loaded.get("claude", {}), cfg["proxy"])
    cfg["providers"] = normalize_providers(loaded.get("providers", []))
    cfg["configured_models"] = normalize_configured_models(
        loaded.get("configured_models", []),
        (str(item.get("id") or "") for item in cfg["providers"]),
    )
    sync_legacy_backend_env(cfg)
    loaded_retention_policy = loaded.get("retention_policy", {})
    cfg["retention_policy"] = defaults["retention_policy"] | (loaded_retention_policy if isinstance(loaded_retention_policy, dict) else {})
    cfg["knowledge"] = _merge_knowledge_config(defaults["knowledge"], loaded.get("knowledge", {}))
    cfg["integrations"] = normalize_integrations_config(loaded.get("integrations", {}))
    cfg["agents"] = normalize_agents_config_from_loaded(
        loaded.get("agents", {}),
        legacy_permissions=_legacy_group_digital_human_permissions(loaded),
    )
    if cfg.get("runner_command") and cfg.get("backend") == "stub":
        cfg["backend"] = "command"
    _apply_portable_overrides(cfg)
    return cfg


def _apply_portable_overrides(cfg: dict) -> None:
    """Resolve ``~``/``$VAR`` tokens in configured paths and apply env overrides.

    Machine-local paths (workspace roots, backend binaries) may be written
    portably (e.g. ``$HOME/proj``) and/or overridden via ``AHA_WORKSPACE_ROOTS``,
    ``AHA_CODEX_BIN``, ``AHA_CLAUDE_BIN`` so a single AHA home works across
    machines. Resolution happens only on the in-memory config returned by
    ``load_config``; disk contents are never rewritten by this function.
    """
    env_roots = os.environ.get("AHA_WORKSPACE_ROOTS")
    if env_roots:
        roots = [platform.expand_path(part) for part in env_roots.split(os.pathsep) if part.strip()]
        if roots:
            cfg["workspace_roots"] = roots
    else:
        raw_roots = cfg.get("workspace_roots") or []
        if isinstance(raw_roots, str):
            raw_roots = [raw_roots]
        if isinstance(raw_roots, list):
            cfg["workspace_roots"] = [platform.expand_path(str(item)) for item in raw_roots if str(item).strip()]
    for backend, env_name in (("codex", "AHA_CODEX_BIN"), ("claude", "AHA_CLAUDE_BIN")):
        section = cfg.get(backend)
        if not isinstance(section, dict):
            continue
        override = os.environ.get(env_name)
        if override:
            section["bin"] = platform.expand_path(override)
        elif isinstance(section.get("bin"), str) and section["bin"]:
            section["bin"] = platform.expand_path(section["bin"])
