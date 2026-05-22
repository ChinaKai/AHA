from __future__ import annotations

from pathlib import Path

from aha_cli.domain.models import default_config
from aha_cli.store.io import read_json
from aha_cli.store.paths import config_path


def load_config(root: Path) -> dict:
    defaults = default_config()
    path = config_path(root)
    if not path.exists():
        return defaults
    loaded = read_json(path)
    cfg = defaults | {key: value for key, value in loaded.items() if key not in {"codex", "claude"}}
    cfg["codex"] = defaults["codex"] | loaded.get("codex", {})
    cfg["claude"] = defaults["claude"] | loaded.get("claude", {})
    if cfg.get("runner_command") and cfg.get("backend") == "stub":
        cfg["backend"] = "command"
    return cfg
