from __future__ import annotations

import os
from pathlib import Path

from aha_cli import platform
from aha_cli.domain.models import default_config, normalize_integrations_config
from aha_cli.services.proxy import proxy_configured
from aha_cli.store.io import read_json
from aha_cli.store.paths import config_path


def _merge_backend_config(defaults: dict, loaded: dict, legacy_proxy: dict) -> dict:
    cfg = defaults | loaded
    loaded_proxy = loaded.get("proxy", {})
    cfg["proxy"] = defaults["proxy"] | (loaded_proxy if isinstance(loaded_proxy, dict) else {})
    if isinstance(loaded_proxy, dict) and "enabled" not in loaded_proxy and "proxy_enabled" not in loaded_proxy and legacy_proxy.get("enabled") is not None:
        cfg["proxy"]["enabled"] = bool(legacy_proxy.get("enabled"))
    if not proxy_configured(cfg["proxy"]) and proxy_configured(legacy_proxy):
        cfg["proxy"] = defaults["proxy"] | {
            key: legacy_proxy.get(key)
            for key in ("enabled", "http_proxy", "https_proxy", "no_proxy")
            if legacy_proxy.get(key) is not None
        }
    return cfg


def _merge_knowledge_config(defaults: dict, loaded: dict) -> dict:
    if not isinstance(loaded, dict):
        return {key: (dict(value) if isinstance(value, dict) else value) for key, value in defaults.items()}
    nested_keys = ("git", "curation", "project_nav", "retrieval")
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
    loaded_proxy = loaded.get("proxy", {})
    cfg["proxy"] = defaults["proxy"] | (loaded_proxy if isinstance(loaded_proxy, dict) else {})
    cfg["codex"] = _merge_backend_config(defaults["codex"], loaded.get("codex", {}), cfg["proxy"])
    cfg["claude"] = _merge_backend_config(defaults["claude"], loaded.get("claude", {}), cfg["proxy"])
    loaded_retention_policy = loaded.get("retention_policy", {})
    cfg["retention_policy"] = defaults["retention_policy"] | (loaded_retention_policy if isinstance(loaded_retention_policy, dict) else {})
    cfg["knowledge"] = _merge_knowledge_config(defaults["knowledge"], loaded.get("knowledge", {}))
    cfg["integrations"] = normalize_integrations_config(loaded.get("integrations", {}))
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
