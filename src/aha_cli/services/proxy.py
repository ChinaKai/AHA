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


def task_has_proxy_config(task: dict) -> bool:
    return bool(
        normalize_proxy_value(task.get("preferred_http_proxy"))
        or normalize_proxy_value(task.get("preferred_https_proxy"))
        or normalize_proxy_value(task.get("preferred_no_proxy"))
    )


def agent_proxy_enabled(agent: dict, task: dict) -> bool:
    return bool(agent.get("proxy_enabled"))


def proxy_env_for_agent(agent: dict, task: dict) -> dict[str, str]:
    if not agent_proxy_enabled(agent, task):
        return {}
    values = {
        "HTTP_PROXY": normalize_proxy_value(task.get("preferred_http_proxy")),
        "HTTPS_PROXY": normalize_proxy_value(task.get("preferred_https_proxy")),
        "NO_PROXY": normalize_proxy_value(task.get("preferred_no_proxy")),
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
