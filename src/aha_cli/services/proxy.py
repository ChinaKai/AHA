from __future__ import annotations

DEFAULT_NO_PROXY = "localhost,127.0.0.1,::1"
PROXY_ENV_KEYS = (
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "NO_PROXY",
    "ALL_PROXY",
    "http_proxy",
    "https_proxy",
    "no_proxy",
    "all_proxy",
)


def normalize_proxy_value(value: object) -> str | None:
    text = str(value or "").strip()
    return text or None


def normalize_proxy_config(
    proxy_enabled: object = False,
    http_proxy: object = None,
    https_proxy: object = None,
    no_proxy: object = None,
) -> dict[str, object]:
    http_proxy_value = normalize_proxy_value(http_proxy)
    https_proxy_value = normalize_proxy_value(https_proxy)
    no_proxy_value = normalize_proxy_value(no_proxy) or (DEFAULT_NO_PROXY if (http_proxy_value or https_proxy_value) else None)
    return {
        "enabled": bool(proxy_enabled),
        "http_proxy": http_proxy_value,
        "https_proxy": https_proxy_value,
        "no_proxy": no_proxy_value,
    }


def proxy_configured(config: dict[str, object] | None) -> bool:
    config = config or {}
    return bool(config.get("http_proxy") or config.get("https_proxy") or config.get("no_proxy"))


def legacy_task_proxy_config(task: dict | None) -> dict[str, object]:
    task = task or {}
    return normalize_proxy_config(
        task.get("preferred_proxy_enabled"),
        task.get("preferred_http_proxy"),
        task.get("preferred_https_proxy"),
        task.get("preferred_no_proxy"),
    )


def run_proxy_config(run: dict | None, fallback_task: dict | None = None) -> dict[str, object]:
    run = run or {}
    raw = run.get("proxy") if isinstance(run.get("proxy"), dict) else {}
    config = normalize_proxy_config(
        raw.get("enabled", raw.get("proxy_enabled", run.get("proxy_enabled", False))),
        raw.get("http_proxy", run.get("http_proxy")),
        raw.get("https_proxy", run.get("https_proxy")),
        raw.get("no_proxy", run.get("no_proxy")),
    )
    if not (config["http_proxy"] or config["https_proxy"] or config["no_proxy"]) and fallback_task:
        return legacy_task_proxy_config(fallback_task)
    return config


def core_proxy_config(config: dict | None, fallback_run: dict | None = None, fallback_task: dict | None = None) -> dict[str, object]:
    config = config or {}
    raw = config.get("proxy") if isinstance(config.get("proxy"), dict) else {}
    proxy = normalize_proxy_config(
        raw.get("enabled", raw.get("proxy_enabled", False)),
        raw.get("http_proxy", config.get("http_proxy")),
        raw.get("https_proxy", config.get("https_proxy")),
        raw.get("no_proxy", config.get("no_proxy")),
    )
    if not proxy_configured(proxy) and fallback_run:
        return run_proxy_config(fallback_run, fallback_task)
    return proxy


def backend_proxy_config(
    config: dict | None,
    backend: object = None,
    fallback_run: dict | None = None,
    fallback_task: dict | None = None,
) -> dict[str, object]:
    config = config or {}
    legacy = core_proxy_config(config)
    backend_name = str(backend or config.get("backend") or "").strip().lower()
    backend_raw = {}
    if backend_name in {"codex", "claude"} and isinstance(config.get(backend_name), dict):
        backend_config = config.get(backend_name) or {}
        backend_raw = backend_config.get("proxy") if isinstance(backend_config.get("proxy"), dict) else {}
    backend_enabled = backend_raw.get("enabled", backend_raw.get("proxy_enabled", legacy.get("enabled", False)))
    proxy = normalize_proxy_config(
        backend_enabled,
        backend_raw.get("http_proxy", legacy.get("http_proxy")),
        backend_raw.get("https_proxy", legacy.get("https_proxy")),
        backend_raw.get("no_proxy", legacy.get("no_proxy")),
    )
    if proxy_configured(proxy):
        return proxy
    if proxy_configured(legacy):
        return legacy
    if fallback_run:
        return run_proxy_config(fallback_run, fallback_task)
    return proxy


def task_has_proxy_config(task: dict) -> bool:
    return bool(
        normalize_proxy_value(task.get("preferred_http_proxy"))
        or normalize_proxy_value(task.get("preferred_https_proxy"))
        or normalize_proxy_value(task.get("preferred_no_proxy"))
    )


def run_has_proxy_config(run: dict | None, fallback_task: dict | None = None) -> bool:
    config = run_proxy_config(run, fallback_task)
    return proxy_configured(config)


def core_has_proxy_config(config: dict | None, fallback_run: dict | None = None, fallback_task: dict | None = None) -> bool:
    proxy = core_proxy_config(config, fallback_run, fallback_task)
    return proxy_configured(proxy)


def backend_has_proxy_config(config: dict | None, backend: object = None, fallback_run: dict | None = None, fallback_task: dict | None = None) -> bool:
    return proxy_configured(backend_proxy_config(config, backend, fallback_run, fallback_task))


def agent_proxy_enabled(agent: dict, task: dict) -> bool:
    return bool(agent.get("proxy_enabled"))


def proxy_env_for_agent(agent: dict, task: dict, run: dict | None = None, config: dict | None = None) -> dict[str, str]:
    if not agent_proxy_enabled(agent, task):
        return {}
    backend = agent.get("backend") or task.get("preferred_backend")
    config = backend_proxy_config(config, backend, run, task) if config is not None else run_proxy_config(run, task) if run is not None else legacy_task_proxy_config(task)
    values = {
        "HTTP_PROXY": normalize_proxy_value(config.get("http_proxy")),
        "HTTPS_PROXY": normalize_proxy_value(config.get("https_proxy")),
        "NO_PROXY": normalize_proxy_value(config.get("no_proxy")),
    }
    env: dict[str, str] = {}
    for key, value in values.items():
        if value:
            env[key] = value
            env[key.lower()] = value
    return env


def apply_proxy_environment(env: dict[str, str], proxy_env: dict[str, str] | None) -> dict[str, str]:
    if proxy_env is None:
        return env
    for key in PROXY_ENV_KEYS:
        env.pop(key, None)
    env.update(proxy_env)
    return env
