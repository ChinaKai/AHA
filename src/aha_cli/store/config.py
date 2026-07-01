from __future__ import annotations

from pathlib import Path

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
    nested_keys = ("git", "curation", "project_nav", "retrieval", "project_context_index")
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
    return cfg
