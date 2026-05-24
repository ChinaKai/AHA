from __future__ import annotations

import os
import re
from collections.abc import Mapping

DEFAULT_CONTEXT_WINDOWS = {
    ("codex", "gpt-5.5"): 1_050_000,
}


def _positive_int(value: object) -> int | None:
    try:
        parsed = int(str(value).replace("_", "").replace(",", ""))
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _env_key(backend: str, model: str) -> str:
    raw = f"{backend}_{model}".upper()
    safe = re.sub(r"[^A-Z0-9]+", "_", raw).strip("_")
    return f"AHA_CONTEXT_WINDOW_{safe}"


def _config_context_window(cfg: Mapping[str, object], backend: str, model: str) -> int | None:
    windows = cfg.get("context_windows")
    if not isinstance(windows, Mapping):
        return None
    flat = _positive_int(windows.get(f"{backend}/{model}"))
    if flat:
        return flat
    by_backend = windows.get(backend)
    if isinstance(by_backend, Mapping):
        return _positive_int(by_backend.get(model))
    return None


def context_window_for_model(
    backend: str,
    model: str | None,
    *,
    cfg: Mapping[str, object] | None = None,
    environ: Mapping[str, str] | None = None,
) -> tuple[int | None, str]:
    normalized_backend = str(backend or "").removesuffix("-chat").strip().lower()
    normalized_model = str(model or "").strip()
    if not normalized_backend or not normalized_model:
        return None, "unknown"

    env = os.environ if environ is None else environ
    key = _env_key(normalized_backend, normalized_model)
    from_env = _positive_int(env.get(key))
    if from_env:
        return from_env, f"env:{key}"

    from_config = _config_context_window(cfg or {}, normalized_backend, normalized_model)
    if from_config:
        return from_config, "config"

    from_table = DEFAULT_CONTEXT_WINDOWS.get((normalized_backend, normalized_model))
    if from_table:
        return from_table, "table"
    return None, "unknown"


def context_pressure(
    backend: str,
    model: str | None,
    usage: Mapping[str, object] | None,
    *,
    cfg: Mapping[str, object] | None = None,
    environ: Mapping[str, str] | None = None,
) -> dict:
    input_tokens = _positive_int((usage or {}).get("input_tokens"))
    context_window, source = context_window_for_model(backend, model, cfg=cfg, environ=environ)
    ratio = (input_tokens / context_window) if input_tokens is not None and context_window else None
    level = "unknown"
    if ratio is not None:
        if ratio >= 0.85:
            level = "high"
        elif ratio >= 0.70:
            level = "watch"
        else:
            level = "ok"
    return {
        "backend": str(backend or "").removesuffix("-chat").strip().lower() or None,
        "model": str(model).strip() if model else None,
        "input_tokens": input_tokens,
        "context_window": context_window,
        "context_window_source": source,
        "ratio": round(ratio, 6) if ratio is not None else None,
        "percent": round(ratio * 100, 2) if ratio is not None else None,
        "level": level,
    }
