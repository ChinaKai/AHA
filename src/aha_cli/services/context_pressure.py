from __future__ import annotations

import os
import re
from collections.abc import Mapping

DEFAULT_CONTEXT_WINDOWS = {
    ("codex", "gpt-5.5"): 258_000,
    ("claude", "default"): 200_000,
    ("claude", "claude-opus-4-7"): 1_000_000,
    ("claude", "opus-4-7"): 1_000_000,
    ("claude", "claude-opus-4-6"): 1_000_000,
    ("claude", "opus-4-6"): 1_000_000,
    ("claude", "claude-sonnet-4-6"): 1_000_000,
    ("claude", "sonnet-4-6"): 1_000_000,
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
    runtime_context_window: int | None = None,
    cfg: Mapping[str, object] | None = None,
    environ: Mapping[str, str] | None = None,
) -> tuple[int | None, str]:
    normalized_backend = str(backend or "").removesuffix("-chat").strip().lower()
    normalized_model = str(model or "").strip()
    if not normalized_backend:
        return None, "unknown"
    if not normalized_model and (normalized_backend, "default") in DEFAULT_CONTEXT_WINDOWS:
        normalized_model = "default"
    if not normalized_model:
        return None, "unknown"

    env = os.environ if environ is None else environ
    key = _env_key(normalized_backend, normalized_model)
    from_env = _positive_int(env.get(key))
    if from_env:
        return from_env, f"env:{key}"

    from_config = _config_context_window(cfg or {}, normalized_backend, normalized_model)
    if from_config:
        return from_config, "config"

    from_runtime = _positive_int(runtime_context_window)
    if from_runtime:
        return from_runtime, "runtime"

    from_table = DEFAULT_CONTEXT_WINDOWS.get((normalized_backend, normalized_model))
    if from_table:
        return from_table, "table"
    from_default_table = DEFAULT_CONTEXT_WINDOWS.get((normalized_backend, "default"))
    if from_default_table:
        return from_default_table, "table:default"
    return None, "unknown"


def context_pressure(
    backend: str,
    model: str | None,
    prompt_metrics: Mapping[str, object] | None,
    *,
    runtime_context_window: int | None = None,
    runtime_token_usage: Mapping[str, object] | None = None,
    cfg: Mapping[str, object] | None = None,
    environ: Mapping[str, str] | None = None,
) -> dict:
    normalized_backend = str(backend or "").removesuffix("-chat").strip().lower() or None
    metrics = prompt_metrics or {}
    total = metrics.get("total")
    total_metrics = total if isinstance(total, Mapping) else {}
    prompt_tokens = (
        _positive_int(total_metrics.get("tokens"))
        or _positive_int(total_metrics.get("input_tokens"))
        or _positive_int(total_metrics.get("token_count"))
        or _positive_int(metrics.get("prompt_tokens"))
    )
    prompt_chars = _positive_int(total_metrics.get("chars"))
    prompt_bytes = _positive_int(total_metrics.get("bytes"))
    prompt_lines = _positive_int(total_metrics.get("lines"))
    runtime_usage = runtime_token_usage or {}
    runtime_input_tokens = _positive_int(runtime_usage.get("input_tokens"))
    runtime_cached_input_tokens = _positive_int(runtime_usage.get("cached_input_tokens")) or _positive_int(runtime_usage.get("cache_read_input_tokens"))
    runtime_cache_creation_input_tokens = _positive_int(runtime_usage.get("cache_creation_input_tokens"))
    runtime_total_tokens = _positive_int(runtime_usage.get("total_tokens"))
    context_window, source = context_window_for_model(
        backend,
        model,
        runtime_context_window=runtime_context_window,
        cfg=cfg,
        environ=environ,
    )
    runtime_effective_input_tokens = runtime_input_tokens
    if normalized_backend == "claude" and runtime_input_tokens is not None:
        runtime_effective_input_tokens = runtime_input_tokens + int(runtime_cached_input_tokens or 0) + int(runtime_cache_creation_input_tokens or 0)
    input_tokens = runtime_effective_input_tokens or prompt_tokens
    backend_input_tokens = runtime_effective_input_tokens
    estimated_backend_history_tokens = None
    aha_overhead_ratio = None
    if backend_input_tokens is not None and prompt_tokens is not None:
        estimated_backend_history_tokens = max(backend_input_tokens - prompt_tokens, 0)
        if backend_input_tokens > 0:
            aha_overhead_ratio = round(prompt_tokens / backend_input_tokens, 6)
    runtime_ratio = (
        (runtime_effective_input_tokens / context_window)
        if runtime_effective_input_tokens is not None and context_window
        else None
    )
    prompt_estimate_ratio = (
        (prompt_tokens / context_window)
        if prompt_tokens is not None and context_window
        else None
    )
    ratio = runtime_ratio if runtime_ratio is not None else prompt_estimate_ratio

    def level_for_ratio(value: float | None) -> str:
        if value is None:
            return "unknown"
        if value >= 0.85:
            return "high"
        if value >= 0.70:
            return "watch"
        return "ok"

    level = level_for_ratio(ratio)
    if runtime_effective_input_tokens is not None:
        pressure_source = "runtime.last_token_usage.effective_input_tokens" if normalized_backend == "claude" else "runtime.last_token_usage.input_tokens"
    elif prompt_tokens is not None:
        pressure_source = "prompt_metrics.tokens"
    elif prompt_chars or prompt_bytes:
        pressure_source = "prompt_metrics.chars"
    else:
        pressure_source = "unknown"
    return {
        "backend": normalized_backend,
        "model": str(model).strip() if model else None,
        "input_tokens": input_tokens,
        "aha_prompt_tokens": prompt_tokens,
        "backend_input_tokens": backend_input_tokens,
        "estimated_backend_history_tokens": estimated_backend_history_tokens,
        "aha_overhead_ratio": aha_overhead_ratio,
        "prompt_tokens": prompt_tokens,
        "runtime_input_tokens": runtime_input_tokens,
        "runtime_effective_input_tokens": runtime_effective_input_tokens,
        "runtime_cached_input_tokens": runtime_cached_input_tokens,
        "runtime_cache_creation_input_tokens": runtime_cache_creation_input_tokens,
        "runtime_total_tokens": runtime_total_tokens,
        "runtime_ratio": round(runtime_ratio, 6) if runtime_ratio is not None else None,
        "runtime_percent": round(runtime_ratio * 100, 2) if runtime_ratio is not None else None,
        "runtime_level": level_for_ratio(runtime_ratio),
        "prompt_estimate_tokens": prompt_tokens,
        "prompt_estimate_ratio": round(prompt_estimate_ratio, 6) if prompt_estimate_ratio is not None else None,
        "prompt_estimate_percent": round(prompt_estimate_ratio * 100, 2) if prompt_estimate_ratio is not None else None,
        "prompt_estimate_level": level_for_ratio(prompt_estimate_ratio),
        "pressure_is_runtime": runtime_ratio is not None,
        "pressure_is_estimate": runtime_ratio is None and prompt_estimate_ratio is not None,
        "prompt_chars": prompt_chars,
        "prompt_bytes": prompt_bytes,
        "prompt_lines": prompt_lines,
        "context_window": context_window,
        "context_window_source": source,
        "pressure_source": pressure_source,
        "ratio": round(ratio, 6) if ratio is not None else None,
        "percent": round(ratio * 100, 2) if ratio is not None else None,
        "level": level,
    }
