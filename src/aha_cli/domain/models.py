from __future__ import annotations

import datetime as dt
import unicodedata
import uuid

from aha_cli.domain.workflow_templates import (
    WORKFLOW_TEMPLATE_GUIDANCE as TASK_WORKFLOW_TEMPLATE_GUIDANCE,
    WORKFLOW_TEMPLATE_IDS as TASK_WORKFLOW_TEMPLATES,
    normalize_workflow_template,
    workflow_template_guidance,
)
from aha_cli.services.prompt_templates import render_prompt_template

DEFAULT_RETENTION_POLICY_REPORT_INTERVAL_SECONDS = 6 * 60 * 60
HEADROOM_INTEGRATION_MODES = {"token", "cache"}
TOKEN_SAVING_PROVIDERS = {"nav"}
MAX_TASK_RELATED_PROJECTS = 5
SYSTEM_RUN_KIND = "system"
SERVICE_ASSISTANT_TASK_KIND = "service_assistant"
SERVICE_ASSISTANT_PURPOSE = "service_assistant"
FEISHU_GROUP_TASK_KIND = "feishu_group_digital_human"
FEISHU_GROUP_PURPOSE = "feishu_group"


def is_system_managed(value: object) -> bool:
    return bool(isinstance(value, dict) and value.get("system_managed"))


def is_service_assistant_run(value: object) -> bool:
    return bool(
        isinstance(value, dict)
        and value.get("system_managed")
        and str(value.get("kind") or "") == SYSTEM_RUN_KIND
        and str(value.get("system_purpose") or "") == SERVICE_ASSISTANT_PURPOSE
    )


def is_service_assistant_task(value: object) -> bool:
    return bool(
        isinstance(value, dict)
        and value.get("system_managed")
        and str(value.get("kind") or "") == SERVICE_ASSISTANT_TASK_KIND
    )


def is_feishu_group_run(value: object) -> bool:
    return bool(
        isinstance(value, dict)
        and value.get("system_managed")
        and str(value.get("kind") or "") == SYSTEM_RUN_KIND
        and str(value.get("system_purpose") or "") == FEISHU_GROUP_PURPOSE
    )


def is_feishu_group_task(value: object) -> bool:
    return bool(
        isinstance(value, dict)
        and value.get("system_managed")
        and str(value.get("kind") or "") == FEISHU_GROUP_TASK_KIND
    )


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()


def new_run_id() -> str:
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    return f"{stamp}-{uuid.uuid4().hex[:6]}"


def default_retention_policy_config() -> dict:
    return {
        "scheduled_report_enabled": True,
        "report_interval_seconds": DEFAULT_RETENTION_POLICY_REPORT_INTERVAL_SECONDS,
        "max_total_bytes": 0,
        "max_candidate_bytes": 0,
        "min_candidate_files": 0,
        "min_age_seconds": 0,
        "include_chat": False,
    }


def default_knowledge_config() -> dict:
    return {
        "enabled": False,
        "path": None,
        "git": {
            "enabled": False,
            "proxy_enabled": False,
            "remote": None,
            "branch": "main",
            "auto_commit": False,
            "auto_push": False,
            "auto_pull": False,
            "author_name": "AHA",
            "author_email": "aha@local",
        },
        "curation": {
            "gate": "manual",
        },
        "project_nav": {
            "enabled": True,
            "maintain_during_task": True,
        },
        "retrieval": {
            "max_entries": 5,
            "max_chars": 4000,
            "inject_mode": "references",
            "summary_chars": 120,
        },
    }


def default_headroom_integration_config() -> dict:
    return {
        "enabled": False,
        "package": "headroom-ai[proxy]",
        "command": "headroom",
        "port": 8787,
        "mode": "token",
        "ccr_enabled": False,
    }


def default_observe_proxy_integration_config() -> dict:
    return {
        "enabled": False,
        "port": 8797,
    }


def default_feishu_integration_config() -> dict:
    return {
        "enabled": False,
        "app_id": "",
        "app_secret": "",
        "app_id_env": "AHA_FEISHU_APP_ID",
        "app_secret_env": "AHA_FEISHU_APP_SECRET",
        "backend": "",
        "model": "",
        "reasoning_effort": "",
        "proxy_enabled": None,
        "default_run_id": "",
        "owner_open_id": "",
        "owner_chat_id": "",
        "allowed_open_ids": [],
        "allowed_chat_ids": [],
        "group_access_mode": "allowed_users",
        "group_mentions_only": True,
        "notifications_enabled": True,
        "security_mode": "audit",
        "group_permissions": {
            "read_paths": [],
            "allow_common_knowledge": False,
            "allowed_topics": [],
            "handoff_always": [],
        },
    }


def default_weixin_integration_config() -> dict:
    return {
        "enabled": False,
        "visible": False,
    }


def default_integrations_config() -> dict:
    return {
        "headroom": default_headroom_integration_config(),
        "observe_proxy": default_observe_proxy_integration_config(),
        "feishu": default_feishu_integration_config(),
        "weixin": default_weixin_integration_config(),
    }


def default_agents_config() -> dict:
    """Channel-agnostic agent identities (digital humans / stewards).

    Permissions here are identity-level: they describe what a given agent may
    answer directly vs. must hand off. Channel access control (which chats/users
    can reach an agent) stays with each channel integration config.
    """
    return {
        "group_digital_human": {
            "permissions": {
                "read_paths": [],
                "allow_common_knowledge": False,
                "allowed_topics": [],
                "handoff_always": [],
            }
        }
    }


def normalize_agents_config(value: object | None = None) -> dict:
    raw = value if isinstance(value, dict) else {}
    config = default_agents_config()
    raw_group = raw.get("group_digital_human")
    if isinstance(raw_group, dict):
        raw_permissions = raw_group.get("permissions")
        if isinstance(raw_permissions, dict):
            allow_common = raw_permissions.get("allow_common_knowledge")
            config["group_digital_human"]["permissions"] = {
                # Legacy default_scope is dropped: the digital human is an
                # electronic delegate whose knowledge source is read_paths, not
                # a semantic preset.
                "read_paths": _normalize_string_list(raw_permissions.get("read_paths")),
                "allow_common_knowledge": normalize_bool(allow_common, default=False),
                "allowed_topics": _normalize_string_list(raw_permissions.get("allowed_topics")),
                "handoff_always": _normalize_string_list(raw_permissions.get("handoff_always")),
            }
    return config


def normalize_agents_config_from_loaded(
    value: object | None = None, legacy_permissions: object | None = None
) -> dict:
    """Normalize the ``agents`` section, applying the legacy
    ``integrations.feishu.group_permissions`` location as a fallback when no
    explicit agent permissions are configured yet.

    ``load_config`` always emits a normalized ``agents`` section, so the legacy
    fallback must happen here at load time rather than inside
    :func:`resolve_group_digital_human_permissions` — otherwise the always-
    present default ``agents`` block would shadow the legacy values.
    """
    raw = value if isinstance(value, dict) else {}
    has_explicit_permissions = bool(
        isinstance(raw.get("group_digital_human"), dict)
        and isinstance(raw["group_digital_human"].get("permissions"), dict)
        and raw["group_digital_human"]["permissions"]
    )
    if not has_explicit_permissions and isinstance(legacy_permissions, dict) and legacy_permissions:
        raw = dict(raw)
        raw["group_digital_human"] = {"permissions": legacy_permissions}
    return normalize_agents_config(raw)


def resolve_group_digital_human_permissions(config: dict) -> dict:
    """Resolve the group digital-human identity permissions, preferring the
    canonical ``agents`` section and falling back to the legacy
    ``integrations.feishu.group_permissions`` location.
    """
    agents = config.get("agents") if isinstance(config.get("agents"), dict) else {}
    agents_permissions = agents.get("group_digital_human", {}).get("permissions") if isinstance(agents.get("group_digital_human"), dict) else {}
    if isinstance(agents_permissions, dict) and agents_permissions:
        return normalize_agents_config(agents)["group_digital_human"]["permissions"]
    integrations = config.get("integrations") if isinstance(config.get("integrations"), dict) else {}
    feishu = integrations.get("feishu") if isinstance(integrations.get("feishu"), dict) else {}
    legacy = feishu.get("group_permissions") if isinstance(feishu.get("group_permissions"), dict) else {}
    if legacy:
        return normalize_feishu_integration_config({"group_permissions": legacy})["group_permissions"]
    return dict(default_agents_config()["group_digital_human"]["permissions"])


def default_config() -> dict:
    return {
        "backend": "stub",
        "runner_command": None,
        "default_parallel": 10,
        "default_mode": "research",
        "workspace_roots": [],
        "webgame_workspace": None,
        "proxy": {
            "enabled": False,
            "http_proxy": None,
            "https_proxy": None,
            "no_proxy": None,
        },
        "context_windows": {},
        "providers": [],
        "configured_models": [],
        "integrations": default_integrations_config(),
        "agents": default_agents_config(),
        "retention_policy": default_retention_policy_config(),
        "knowledge": default_knowledge_config(),
        "codex": {
            "bin": "codex",
            "model": None,
            "reasoning_effort": None,
            "sandbox": "auto",
            "approval": "never",
            "json": True,
            "session_policy": "sticky",
            "env_active": None,
            "model_source": "both",
            "env": [],
            "proxy": {
                "enabled": False,
            },
        },
        "claude": {
            "bin": "claude",
            "model": None,
            "reasoning_effort": None,
            "sandbox": "auto",
            "permission_mode": None,
            "session_policy": "sticky",
            "env_active": None,
            "model_source": "both",
            "env": [],
            "proxy": {
                "enabled": False,
            },
        },
    }


TASK_SUPERVISION_MODES = {"manual", "assisted"}
TASK_SUPERVISION_HOST_BACKENDS = {"stub", "codex", "claude"}
TASK_SUPERVISION_ASK_USER_GATES = (
    "real_ui_validation",
    "scope_change",
    "commit_merge_delete",
    "destructive_or_high_risk",
    "permissions_or_external",
    "product_preference",
)
TASK_COLLABORATION_MODES = {"auto", "solo", "pair", "team"}
TASK_COLLABORATION_DEFAULTS = {
    "auto": ("auto", 3),
    "solo": ("disabled", 0),
    "pair": ("auto", 1),
    "team": ("auto", 2),
}
DEFAULT_TASK_SANDBOX = "danger-full-access"
DEFAULT_TASK_SUPERVISION_MAX_ROUNDS = 99
DEFAULT_TASK_CONTEXT_THRESHOLD_PERCENT = 75
TASK_HARDWARE_DEBUG_MODES = ("off", "serial", "network", "both")
TASK_HARDWARE_DEBUG_ACCESS_MODES = ("read_only", "read_write")
# Compatibility-only input vocabulary. New task state uses mode/serial/network/credentials.
TASK_HARDWARE_DEBUG_CHANNEL_TYPES = ("uart", "nfs", "telnet")
TASK_HARDWARE_DEBUG_PERMISSION_KEYS = ("read", "write")
TASK_BROWSER_CONTROL_MODES = ("off", "managed")
TASK_BROWSER_ACCESS_MODES = ("read_only", "read_write")
TASK_BROWSER_RUNTIME_MODES = ("playwright", "user_chrome")
TASK_BROWSER_PROFILE_MODES = ("ephemeral", "task", "named")
TASK_BROWSER_CHANNEL_MODES = ("auto", "chrome", "msedge", "chromium")
TASK_BROWSER_MODE_VALUES = ("privacy", "daily")
TASK_BROWSER_DISPLAY_MODES = ("native", "embedded")
TASK_BROWSER_DEVICE_MODES = ("desktop", "mobile")
TASK_BROWSER_TRANSFER_MODES = ("deny", "allow")
TASK_BROWSER_PROXY_MODES = ("direct", "inherit", "custom")
DEFAULT_TASK_BROWSER_START_URL = "https://www.bing.com/"


def default_task_supervision_ask_user_gates() -> dict:
    return {key: False for key in TASK_SUPERVISION_ASK_USER_GATES}


def normalize_bool(value: object, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"1", "true", "yes", "on"}:
            return True
        if lowered in {"0", "false", "no", "off"}:
            return False
    if value is None:
        return default
    return bool(value)


def normalize_delegation_policy(value: object, default: str = "auto") -> str:
    policy = str(value or default).strip().lower()
    return policy if policy in {"auto", "disabled"} else default


def normalize_collaboration_mode(value: object, default: str = "auto") -> str:
    mode = str(value or default).strip().lower()
    return mode if mode in TASK_COLLABORATION_MODES else default


def non_negative_int(value: object, default: int = 0) -> int:
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return max(0, default)


def _optional_clean_string(value: object) -> str | None:
    text = str(value or "").strip()
    return text or None


def normalize_headroom_integration_config(value: object | None = None) -> dict:
    raw = value if isinstance(value, dict) else {}
    config = default_headroom_integration_config()
    if "enabled" in raw:
        config["enabled"] = normalize_bool(raw.get("enabled"))
    if _optional_clean_string(raw.get("package")):
        config["package"] = _optional_clean_string(raw.get("package"))
    if _optional_clean_string(raw.get("command")):
        config["command"] = _optional_clean_string(raw.get("command"))
    try:
        config["port"] = max(1, min(65535, int(raw.get("port") or config["port"])))
    except (TypeError, ValueError):
        pass
    mode = str(raw.get("mode") or config["mode"]).strip().lower()
    config["mode"] = mode if mode in HEADROOM_INTEGRATION_MODES else "token"
    if "ccr_enabled" in raw:
        config["ccr_enabled"] = normalize_bool(raw.get("ccr_enabled"))
    return config


def normalize_observe_proxy_integration_config(value: object | None = None) -> dict:
    raw = value if isinstance(value, dict) else {}
    config = default_observe_proxy_integration_config()
    if "enabled" in raw:
        config["enabled"] = normalize_bool(raw.get("enabled"))
    try:
        config["port"] = max(1, min(65535, int(raw.get("port") or config["port"])))
    except (TypeError, ValueError):
        pass
    return config


def normalize_feishu_integration_config(value: object | None = None) -> dict:
    raw = value if isinstance(value, dict) else {}
    config = default_feishu_integration_config()
    for key in ("enabled", "group_mentions_only", "notifications_enabled"):
        if key in raw:
            config[key] = normalize_bool(raw.get(key))
    for key in (
        "app_id",
        "app_secret",
        "app_id_env",
        "app_secret_env",
        "backend",
        "model",
        "reasoning_effort",
        "default_run_id",
    ):
        if key in raw:
            config[key] = str(raw.get(key) or "").strip()
    for key in ("owner_open_id", "owner_chat_id"):
        if key not in raw:
            continue
        value = raw.get(key)
        if isinstance(value, list):
            value = next((item for item in value if str(item or "").strip()), "")
        config[key] = str(value or "").strip()
    if "proxy_enabled" in raw and raw.get("proxy_enabled") is not None:
        config["proxy_enabled"] = normalize_bool(raw.get("proxy_enabled"))
    for key in ("allowed_open_ids", "allowed_chat_ids"):
        allowed = raw.get(key)
        if isinstance(allowed, str):
            allowed = [item.strip() for item in allowed.replace("\n", ",").split(",")]
        if isinstance(allowed, list):
            config[key] = list(
                dict.fromkeys(str(item or "").strip() for item in allowed if str(item or "").strip())
            )
    group_access_mode = str(raw.get("group_access_mode") or config["group_access_mode"]).strip().lower()
    config["group_access_mode"] = (
        group_access_mode if group_access_mode in {"allowed_users", "all_members"} else "allowed_users"
    )
    security_mode = str(raw.get("security_mode") or config["security_mode"]).strip().lower()
    config["security_mode"] = security_mode if security_mode in {"compat", "audit", "strict"} else "audit"
    raw_permissions = raw.get("group_permissions")
    if isinstance(raw_permissions, dict):
        allow_common = raw_permissions.get("allow_common_knowledge")
        config["group_permissions"] = {
            "read_paths": _normalize_string_list(raw_permissions.get("read_paths")),
            "allow_common_knowledge": normalize_bool(allow_common, default=False),
            "allowed_topics": _normalize_string_list(raw_permissions.get("allowed_topics")),
            "handoff_always": _normalize_string_list(raw_permissions.get("handoff_always")),
        }
    return config


def normalize_weixin_integration_config(value: object | None = None) -> dict:
    raw = value if isinstance(value, dict) else {}
    config = default_weixin_integration_config()
    for key in ("enabled", "visible"):
        if key in raw:
            config[key] = normalize_bool(raw.get(key))
    return config


def normalize_integrations_config(value: object | None = None) -> dict:
    raw = value if isinstance(value, dict) else {}
    config = default_integrations_config()
    config["headroom"] = normalize_headroom_integration_config(raw.get("headroom"))
    config["observe_proxy"] = normalize_observe_proxy_integration_config(raw.get("observe_proxy"))
    config["feishu"] = normalize_feishu_integration_config(raw.get("feishu"))
    config["weixin"] = normalize_weixin_integration_config(raw.get("weixin"))
    return config


def infer_collaboration_mode(delegation_policy: object, max_sub_agents: object) -> str:
    policy = normalize_delegation_policy(delegation_policy)
    limit = non_negative_int(max_sub_agents)
    if policy == "disabled" or limit == 0:
        return "solo"
    if limit == 1:
        return "pair"
    if limit == 2:
        return "team"
    return "auto"


def resolve_task_collaboration(
    collaboration_mode: object | None = None,
    delegation_policy: object | None = None,
    max_sub_agents: object | None = None,
) -> tuple[str, str, int]:
    explicit_mode = collaboration_mode is not None and str(collaboration_mode).strip() != ""
    if explicit_mode:
        mode = normalize_collaboration_mode(collaboration_mode)
        default_policy, default_limit = TASK_COLLABORATION_DEFAULTS[mode]
        if mode == "auto":
            policy = normalize_delegation_policy(delegation_policy, default_policy)
            if policy == "disabled":
                return "solo", "disabled", 0
            limit = non_negative_int(max_sub_agents, default_limit) if max_sub_agents is not None else default_limit
            return "auto", "auto", limit
        return mode, default_policy, default_limit

    policy = normalize_delegation_policy(delegation_policy)
    if policy == "disabled":
        return "solo", "disabled", 0
    limit = non_negative_int(max_sub_agents, 3) if max_sub_agents is not None else 3
    return infer_collaboration_mode(policy, limit), "auto", limit


def default_task_supervision() -> dict:
    return {
        "mode": "manual",
        "scope": "task",
        "host_backend": "stub",
        "host_model": None,
        "host_proxy_enabled": False,
        "host_agent_id": None,
        "real_agent_enabled": False,
        "channel": "main_only",
        "max_rounds": DEFAULT_TASK_SUPERVISION_MAX_ROUNDS,
        "ask_user_gates": default_task_supervision_ask_user_gates(),
    }


def normalize_task_supervision(value: object | None = None) -> dict:
    raw = value if isinstance(value, dict) else {}
    supervision = default_task_supervision()
    mode = str(raw.get("mode") or supervision["mode"]).strip().lower()
    supervision["mode"] = mode if mode in TASK_SUPERVISION_MODES else "manual"
    host_backend = str(raw.get("host_backend") or supervision["host_backend"]).strip().lower()
    supervision["host_backend"] = host_backend if host_backend in TASK_SUPERVISION_HOST_BACKENDS else "stub"
    host_model = raw.get("host_model", raw.get("model"))
    supervision["host_model"] = str(host_model).strip() if host_model not in (None, "") else None
    supervision["host_proxy_enabled"] = normalize_bool(raw.get("host_proxy_enabled", raw.get("proxy_enabled")), default=False)
    host_agent_id = raw.get("host_agent_id")
    supervision["host_agent_id"] = str(host_agent_id).strip() if host_agent_id else None
    if "real_agent_enabled" in raw:
        supervision["real_agent_enabled"] = normalize_bool(raw.get("real_agent_enabled"))
    raw_gates = raw.get("ask_user_gates") if isinstance(raw.get("ask_user_gates"), dict) else raw.get("ask_user")
    if isinstance(raw_gates, dict):
        gates = default_task_supervision_ask_user_gates()
        for key in TASK_SUPERVISION_ASK_USER_GATES:
            if key in raw_gates:
                gates[key] = normalize_bool(raw_gates.get(key), default=False)
        supervision["ask_user_gates"] = gates
    try:
        supervision["max_rounds"] = max(1, min(100, int(raw.get("max_rounds") or supervision["max_rounds"])))
    except (TypeError, ValueError):
        pass

    if supervision["mode"] == "manual":
        supervision["host_backend"] = "stub"
        supervision["host_agent_id"] = None
        supervision["real_agent_enabled"] = False
    elif supervision["host_backend"] == "stub":
        supervision["host_agent_id"] = None
        supervision["real_agent_enabled"] = False
    else:
        supervision["real_agent_enabled"] = True
    return supervision


def default_task_context_management() -> dict:
    return {
        "auto_compact_enabled": False,
        "auto_compact_threshold_percent": DEFAULT_TASK_CONTEXT_THRESHOLD_PERCENT,
    }


def default_task_token_saving() -> dict:
    return {
        "enabled": False,
        "provider": "nav",
        "related_project_keys": [],
    }


def default_task_observe_proxy() -> dict:
    return {
        "enabled": False,
    }


def default_task_browser_control() -> dict:
    return {
        "mode": "off",
        "start_url": DEFAULT_TASK_BROWSER_START_URL,
        "agent_access": "read_only",
        "runtime": "playwright",
        "profile": "ephemeral",
        "profile_name": "",
        "channel": "auto",
        "browser_mode": "privacy",
        "display": "native",
        "device_mode": "desktop",
        "allowed_hosts": [],
        "downloads": "deny",
        "uploads": "deny",
        "proxy_mode": "direct",
        "proxy_server": "",
        "proxy_bypass": "",
        "proxy_username": "",
        "proxy_password": "",
    }


def _normalize_browser_host(value: object) -> str:
    host = str(value or "").strip().lower()
    if not host or len(host) > 253:
        return ""
    if "://" in host or "/" in host or any(char.isspace() for char in host):
        return ""
    if host.startswith("*."):
        suffix = host[2:]
        return host if suffix and all(part for part in suffix.split(".")) else ""
    return host if all(part for part in host.split(".")) else ""


def normalize_browser_profile_name(value: object) -> str:
    name = unicodedata.normalize("NFKC", str(value or "")).strip()
    if not name or len(name) > 80 or any(ord(char) < 32 for char in name):
        return ""
    return name


def normalize_task_browser_control(value: object | None = None) -> dict:
    raw = value if isinstance(value, dict) else {}
    config = default_task_browser_control()
    mode = str(raw.get("mode") or ("managed" if normalize_bool(raw.get("enabled")) else "off")).strip().lower()
    config["mode"] = mode if mode in TASK_BROWSER_CONTROL_MODES else "off"
    config["start_url"] = str(
        raw.get("start_url") or raw.get("url") or config["start_url"]
    ).strip()
    access = str(raw.get("agent_access") or raw.get("access") or "read_only").strip().lower().replace("-", "_")
    config["agent_access"] = access if access in TASK_BROWSER_ACCESS_MODES else "read_only"
    runtime = str(raw.get("runtime") or "playwright").strip().lower()
    config["runtime"] = runtime if runtime in TASK_BROWSER_RUNTIME_MODES else "playwright"
    profile = str(raw.get("profile") or "ephemeral").strip().lower()
    config["profile"] = profile if profile in TASK_BROWSER_PROFILE_MODES else "ephemeral"
    config["profile_name"] = normalize_browser_profile_name(raw.get("profile_name"))
    channel = str(raw.get("channel") or "auto").strip().lower()
    config["channel"] = channel if channel in TASK_BROWSER_CHANNEL_MODES else "auto"
    if config["profile"] != "named":
        config["profile_name"] = ""
    elif not config["profile_name"]:
        config["profile"] = "ephemeral"
    display = str(raw.get("display") or "native").strip().lower()
    config["display"] = display if display in TASK_BROWSER_DISPLAY_MODES else "native"
    device_mode = str(raw.get("device_mode") or "desktop").strip().lower()
    config["device_mode"] = device_mode if device_mode in TASK_BROWSER_DEVICE_MODES else "desktop"
    hosts: list[str] = []
    seen: set[str] = set()
    raw_hosts = raw.get("allowed_hosts")
    if isinstance(raw_hosts, str):
        raw_hosts = raw_hosts.replace(",", "\n").splitlines()
    if isinstance(raw_hosts, (list, tuple, set)):
        for item in raw_hosts:
            host = _normalize_browser_host(item)
            if not host or host in seen:
                continue
            hosts.append(host)
            seen.add(host)
            if len(hosts) >= 100:
                break
    config["allowed_hosts"] = hosts
    for key in ("downloads", "uploads"):
        mode_value = str(raw.get(key) or "deny").strip().lower()
        config[key] = mode_value if mode_value in TASK_BROWSER_TRANSFER_MODES else "deny"
    proxy_mode = str(raw.get("proxy_mode") or "direct").strip().lower()
    config["proxy_mode"] = proxy_mode if proxy_mode in TASK_BROWSER_PROXY_MODES else "direct"
    config["proxy_server"] = str(raw.get("proxy_server") or "").strip()[:2048]
    config["proxy_bypass"] = str(raw.get("proxy_bypass") or "").strip()[:4096]
    config["proxy_username"] = str(raw.get("proxy_username") or "").strip()[:512]
    config["proxy_password"] = str(raw.get("proxy_password") or "")[:4096]
    # Simplified browser model: a single privacy/daily choice drives the launch
    # fields, and enabling a browser always grants full read/write + transfers.
    # Legacy runtime/profile/display/permission values are derived, not exposed.
    browser_mode = str(raw.get("browser_mode") or "privacy").strip().lower()
    config["browser_mode"] = browser_mode if browser_mode in TASK_BROWSER_MODE_VALUES else "privacy"
    if config["browser_mode"] == "daily":
        config["runtime"] = "user_chrome"
        # Persistent (not ephemeral) so logins/sync survive browser restarts;
        # it is a dedicated per-task profile, not the host desktop profile.
        config["profile"] = "task"
    else:
        config["runtime"] = "playwright"
        config["profile"] = "task"
    config["profile_name"] = ""
    config["display"] = "native"  # real window + embedded panel both active
    config["agent_access"] = "read_write"
    config["downloads"] = "allow"
    config["uploads"] = "allow"
    return config


def task_browser_agent_can_write(task: dict) -> bool:
    config = normalize_task_browser_control(task.get("browser_control"))
    return config.get("mode") == "managed" and config.get("agent_access") == "read_write"


def normalize_task_context_management(value: object | None = None, *, default_enabled: bool = False) -> dict:
    raw = value if isinstance(value, dict) else {}
    context = default_task_context_management()
    if default_enabled:
        context["auto_compact_enabled"] = True
    if "auto_compact_enabled" in raw:
        context["auto_compact_enabled"] = normalize_bool(raw.get("auto_compact_enabled"))
    elif "enabled" in raw:
        context["auto_compact_enabled"] = normalize_bool(raw.get("enabled"))
    raw_threshold = raw.get("auto_compact_threshold_percent", raw.get("threshold_percent"))
    if raw_threshold is not None:
        try:
            context["auto_compact_threshold_percent"] = max(1, min(99, int(raw_threshold)))
        except (TypeError, ValueError):
            pass
    return context


def normalize_task_token_saving(value: object | None = None, legacy_context: object | None = None) -> dict:
    raw = value if isinstance(value, dict) else {}
    token_saving = default_task_token_saving()
    if "enabled" in raw:
        token_saving["enabled"] = normalize_bool(raw.get("enabled"))
    elif "token_saving_enabled" in raw:
        token_saving["enabled"] = normalize_bool(raw.get("token_saving_enabled"))
    elif value is None and legacy_context is not None:
        token_saving["enabled"] = bool(normalize_task_context_management(legacy_context).get("auto_compact_enabled"))
    provider = str(raw.get("provider") or token_saving["provider"]).strip().lower()
    if provider == "map":
        provider = "nav"
    token_saving["provider"] = provider if provider in TOKEN_SAVING_PROVIDERS else "nav"
    related_keys: list[str] = []
    seen: set[str] = set()
    raw_related = raw.get("related_project_keys")
    if isinstance(raw_related, (list, tuple, set)):
        for item in raw_related:
            key = str(item or "").strip()
            if (
                not key
                or key in seen
                or len(key) > 128
                or not key[0].isalnum()
                or any(not (char.isalnum() or char in "._-") for char in key)
            ):
                continue
            related_keys.append(key)
            seen.add(key)
            if len(related_keys) >= MAX_TASK_RELATED_PROJECTS:
                break
    token_saving["related_project_keys"] = related_keys
    return token_saving


def normalize_task_observe_proxy(value: object | None = None) -> dict:
    raw = value if isinstance(value, dict) else {}
    observe_proxy = default_task_observe_proxy()
    if "enabled" in raw:
        observe_proxy["enabled"] = normalize_bool(raw.get("enabled"))
    elif "observe_proxy_enabled" in raw:
        observe_proxy["enabled"] = normalize_bool(raw.get("observe_proxy_enabled"))
    return observe_proxy


def default_task_hardware_debug() -> dict:
    return {
        "mode": "off",
        "serial": {
            "device": "",
            "baudrate": 115200,
        },
        "network": {
            "device_ip": "",
        },
        "credentials": {
            "username": "",
            "password": "",
        },
        "permissions": {
            "access": "read_only",
        },
    }


def default_task_skills() -> dict:
    return {
        "enabled_paths": [],
    }


def _normalize_string_list(value: object) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        items = value.replace("\r\n", "\n").replace(",", "\n").split("\n")
    elif isinstance(value, (list, tuple, set)):
        items = list(value)
    else:
        items = [value]
    normalized: list[str] = []
    seen: set[str] = set()
    for item in items:
        text = str(item or "").strip()
        if not text or text in seen:
            continue
        normalized.append(text)
        seen.add(text)
    return normalized


def normalize_task_hardware_debug_permissions(value: object | None = None) -> dict:
    raw = value if isinstance(value, dict) else {}
    permissions = {"read": True, "write": False}
    legacy_map = {
        "serial_read": "read",
        "serial_write": "write",
    }
    for old_key, new_key in legacy_map.items():
        if old_key in raw:
            permissions[new_key] = normalize_bool(raw.get(old_key), default=permissions[new_key])
    for key in TASK_HARDWARE_DEBUG_PERMISSION_KEYS:
        if key in raw:
            permissions[key] = normalize_bool(raw.get(key), default=permissions[key])
    return permissions


def normalize_task_hardware_debug_access(value: object | None, *, default: str = "read_only") -> dict:
    access = ""
    if isinstance(value, str):
        access = value.strip().lower().replace("-", "_")
    elif isinstance(value, dict):
        access = str(value.get("access") or value.get("mode") or "").strip().lower().replace("-", "_")
        if not access:
            for key in ("write", "serial_write", "terminal_write"):
                if key in value:
                    access = "read_write" if normalize_bool(value.get(key)) else "read_only"
                    break
    if access not in TASK_HARDWARE_DEBUG_ACCESS_MODES:
        access = default if default in TASK_HARDWARE_DEBUG_ACCESS_MODES else "read_only"
    return {"access": access}


def task_hardware_debug_can_write(task: dict) -> bool:
    hardware = normalize_task_hardware_debug(task.get("hardware_debug"))
    return hardware.get("permissions", {}).get("access") == "read_write"


def normalize_task_hardware_debug_uart_settings(value: object) -> dict:
    raw = value if isinstance(value, dict) else {}
    port = str(raw.get("port") or raw.get("path") or "").strip()
    baudrate_raw = raw.get("baudrate", raw.get("baud"))
    try:
        baudrate = int(baudrate_raw) if baudrate_raw not in (None, "") else 115200
    except (TypeError, ValueError):
        baudrate = 115200
    return {
        "port": port,
        "baudrate": max(1, baudrate),
        "username": str(raw.get("username") or raw.get("user") or "").strip(),
        "password": str(raw.get("password") or ""),
    }


def normalize_task_hardware_debug_serial(value: object) -> dict:
    raw = value if isinstance(value, dict) else {}
    port = str(raw.get("device") or raw.get("port") or raw.get("path") or "").strip()
    baudrate_raw = raw.get("baudrate", raw.get("baud"))
    try:
        baudrate = int(baudrate_raw) if baudrate_raw not in (None, "") else 115200
    except (TypeError, ValueError):
        baudrate = 115200
    return {
        "device": port,
        "baudrate": max(1, baudrate),
    }


def normalize_task_hardware_debug_network(value: object) -> dict:
    raw = value if isinstance(value, dict) else {}
    return {
        "device_ip": str(raw.get("device_ip") or raw.get("host") or raw.get("server") or raw.get("ip") or "").strip(),
    }


def normalize_task_hardware_debug_credentials(value: object) -> dict:
    raw = value if isinstance(value, dict) else {}
    return {
        "username": str(raw.get("username") or raw.get("user") or "").strip(),
        "password": str(raw.get("password") or ""),
    }


def normalize_task_hardware_debug_nfs_settings(value: object) -> dict:
    raw = value if isinstance(value, dict) else {}
    return {
        "server": str(raw.get("server") or raw.get("host") or "").strip(),
        "remote_path": str(raw.get("remote_path") or raw.get("export_path") or raw.get("path") or "").strip(),
        "mount_path": str(raw.get("mount_path") or raw.get("target_path") or "").strip(),
    }


def normalize_task_hardware_debug_telnet_settings(value: object) -> dict:
    raw = value if isinstance(value, dict) else {}
    port_raw = raw.get("port")
    try:
        port = int(port_raw) if port_raw not in (None, "") else 23
    except (TypeError, ValueError):
        port = 23
    return {
        "host": str(raw.get("host") or raw.get("server") or "").strip(),
        "port": max(1, port),
        "username": str(raw.get("username") or raw.get("user") or "").strip(),
        "password": str(raw.get("password") or ""),
    }


def normalize_task_hardware_debug_channel(value: object) -> dict | None:
    if not isinstance(value, dict):
        return None
    channel_type = str(value.get("type") or value.get("kind") or "").strip().lower()
    if channel_type in {"", "none"}:
        return None
    if channel_type not in TASK_HARDWARE_DEBUG_CHANNEL_TYPES:
        return None
    settings_raw = value.get("settings") if isinstance(value.get("settings"), dict) else value
    if channel_type == "uart":
        settings = normalize_task_hardware_debug_uart_settings(settings_raw)
    elif channel_type == "nfs":
        settings = normalize_task_hardware_debug_nfs_settings(settings_raw)
    else:
        settings = normalize_task_hardware_debug_telnet_settings(settings_raw)
    return {
        "type": channel_type,
        "settings": settings,
        "permissions": normalize_task_hardware_debug_permissions(value.get("permissions")),
    }


def normalize_task_hardware_debug(value: object | None = None) -> dict:
    raw = value if isinstance(value, dict) else {}
    # Canonical v2 state: connection facts only. Terminal protocol details and tools
    # (Telnet port, NFS exports, board-specific workflows) live in runtime/skills.
    legacy_shape = any(key in raw for key in ("enabled", "hardware_debug_enabled", "devices", "channels"))
    canonical_shape = any(key in raw for key in ("mode", "serial", "network", "credentials")) or (
        "permissions" in raw and not legacy_shape
    )
    if canonical_shape:
        mode = str(raw.get("mode") or "off").strip().lower()
        if mode not in TASK_HARDWARE_DEBUG_MODES:
            mode = "off"
        hardware_debug = default_task_hardware_debug()
        hardware_debug["mode"] = mode
        hardware_debug["serial"] = normalize_task_hardware_debug_serial(raw.get("serial"))
        hardware_debug["network"] = normalize_task_hardware_debug_network(raw.get("network"))
        hardware_debug["credentials"] = normalize_task_hardware_debug_credentials(raw.get("credentials"))
        compatibility_default = "read_write" if mode != "off" and "permissions" not in raw else "read_only"
        hardware_debug["permissions"] = normalize_task_hardware_debug_access(
            raw.get("permissions"),
            default=compatibility_default,
        )
        return hardware_debug

    # Compatibility upgrade for the previous UART/NFS/Telnet channel schema.
    channels_raw = raw.get("channels")
    channels: list[dict] = []
    if isinstance(channels_raw, list):
        channels = [channel for item in channels_raw if (channel := normalize_task_hardware_debug_channel(item))]
    elif isinstance(channels_raw, dict):
        channel = normalize_task_hardware_debug_channel(channels_raw)
        channels = [channel] if channel else []
    else:
        enabled = normalize_bool(raw.get("enabled", raw.get("hardware_debug_enabled")), default=False)
        devices_raw = raw.get("devices")
        legacy_devices = devices_raw if isinstance(devices_raw, list) else ([devices_raw] if isinstance(devices_raw, dict) else [])
        if enabled:
            if legacy_devices:
                for device in legacy_devices:
                    channel = normalize_task_hardware_debug_channel(
                        {
                            "type": "uart",
                            "settings": device,
                            "permissions": raw.get("permissions"),
                        }
                    )
                    if channel:
                        channels.append(channel)
            else:
                channel = normalize_task_hardware_debug_channel(
                    {
                        "type": "uart",
                        "settings": {},
                        "permissions": raw.get("permissions"),
                    }
                )
                if channel:
                    channels.append(channel)
    enabled = normalize_bool(raw.get("enabled", raw.get("hardware_debug_enabled")), default=bool(channels))
    uart = next((item for item in channels if item.get("type") == "uart"), None)
    telnet = next((item for item in channels if item.get("type") == "telnet"), None)
    nfs = next((item for item in channels if item.get("type") == "nfs"), None)
    uart_settings = uart.get("settings") if isinstance(uart, dict) and isinstance(uart.get("settings"), dict) else {}
    telnet_settings = telnet.get("settings") if isinstance(telnet, dict) and isinstance(telnet.get("settings"), dict) else {}
    nfs_settings = nfs.get("settings") if isinstance(nfs, dict) and isinstance(nfs.get("settings"), dict) else {}

    serial = normalize_task_hardware_debug_serial(uart_settings)
    network = normalize_task_hardware_debug_network(
        {"device_ip": telnet_settings.get("host") or nfs_settings.get("server") or ""}
    )
    credential_source = telnet_settings if telnet_settings.get("username") or telnet_settings.get("password") else uart_settings
    credentials = normalize_task_hardware_debug_credentials(credential_source)
    access = "read_write" if any(
        bool(item.get("permissions", {}).get("write")) for item in channels
    ) else "read_only"
    has_serial = bool(uart is not None)
    has_network = bool(telnet is not None or nfs is not None)
    if not enabled:
        mode = "off"
    elif has_serial and has_network:
        mode = "both"
    elif has_network:
        mode = "network"
    else:
        mode = "serial"
    return {
        "mode": mode,
        "serial": serial,
        "network": network,
        "credentials": credentials,
        "permissions": {"access": access},
    }


def normalize_task_skills(value: object | None = None) -> dict:
    if isinstance(value, dict):
        raw_paths = value.get("enabled_paths", value.get("skill_paths", value.get("paths", value.get("skills"))))
    else:
        raw_paths = value
    task_skills = default_task_skills()
    task_skills["enabled_paths"] = _normalize_string_list(raw_paths)
    return task_skills


def task_metadata_projection(task: dict, default_backend: str = "codex") -> dict:
    preferred_backend = task.get("preferred_backend") or default_backend
    preferred_model = task.get("preferred_model")
    collaboration_mode, delegation_policy, max_sub_agents = resolve_task_collaboration(
        task.get("collaboration_mode"),
        task.get("delegation_policy"),
        task.get("max_sub_agents"),
    )
    preferred_sub_model = task.get("preferred_sub_model")
    if preferred_sub_model is None:
        preferred_sub_model = preferred_model
    return {
        "workspace_id": task.get("workspace_id"),
        "workspace_path": task.get("workspace_path"),
        "preferred_backend": preferred_backend,
        "preferred_model": preferred_model,
        "preferred_reasoning_effort": task.get("preferred_reasoning_effort"),
        "preferred_sandbox": task.get("preferred_sandbox"),
        "preferred_approval": task.get("preferred_approval"),
        "preferred_proxy_enabled": bool(task.get("preferred_proxy_enabled")),
        "preferred_http_proxy": task.get("preferred_http_proxy"),
        "preferred_https_proxy": task.get("preferred_https_proxy"),
        "preferred_no_proxy": task.get("preferred_no_proxy"),
        "preferred_sub_backend": task.get("preferred_sub_backend") or preferred_backend,
        "preferred_sub_model": preferred_sub_model,
        "collaboration_mode": collaboration_mode,
        "workflow_template": normalize_workflow_template(task.get("workflow_template")),
        "delegation_policy": delegation_policy,
        "max_sub_agents": max_sub_agents,
        "supervision": normalize_task_supervision(task.get("supervision")),
        "context_management": normalize_task_context_management(task.get("context_management")),
        "token_saving": normalize_task_token_saving(task.get("token_saving"), task.get("context_management")),
        "observe_proxy": normalize_task_observe_proxy(task.get("observe_proxy")),
        "browser_control": normalize_task_browser_control(task.get("browser_control")),
        "task_skills": normalize_task_skills(task.get("task_skills")),
        "hardware_debug": normalize_task_hardware_debug(task.get("hardware_debug")),
    }


def default_tasks(goal: str, agents: int, mode: str) -> list[str]:
    research = [
        "Map the relevant files, concepts, and terminology for the goal.",
        "Trace the main execution flow and identify important data inputs and outputs.",
        "Analyze edge cases, risks, unclear assumptions, and missing context.",
        "Produce a concise module-level report with recommended next steps.",
    ]
    implementation = [
        "Inspect the current code and identify the minimal implementation scope.",
        "Implement a bounded change in an isolated write scope.",
        "Add or update focused verification for the changed behavior.",
        "Summarize changed files, verification results, and remaining risks.",
    ]
    base = implementation if mode == "implementation" else research
    tasks: list[str] = []
    for idx in range(max(1, agents)):
        tasks.append(base[idx] if idx < len(base) else f"Handle additional independent slice {idx + 1} for: {goal}")
    return tasks


def make_agent(
    agent_id: str,
    role: str,
    backend: str = "codex",
    status: str = "pending",
    model: str | None = None,
    reasoning_effort: str | None = None,
    workspace_path: str | None = None,
    sandbox: str | None = None,
    approval: str | None = None,
    proxy_enabled: bool = False,
    created_by: str = "system",
    created_reason: str = "",
) -> dict:
    return {
        "id": agent_id,
        "role": role,
        "backend": backend,
        "model": model,
        "reasoning_effort": reasoning_effort,
        "sandbox": sandbox,
        "approval": approval,
        "proxy_enabled": bool(proxy_enabled),
        "status": status,
        "session_policy": "sticky",
        "session_id": None,
        "backend_session_id": None,
        "workspace_path": workspace_path,
        "created_by": created_by,
        "created_reason": created_reason,
        "assignment_id": None,
        "scope_id": None,
        "scope_explicit": False,
        "generation": 0,
        "status_started_at": None,
        "last_active_at": None,
        "last_usage": None,
    }


def make_task(
    task_id: str,
    title: str,
    created: str,
    backend: str = "codex",
    model: str | None = None,
    reasoning_effort: str | None = None,
    workspace_path: str | None = None,
    workspace_id: str | None = None,
    sandbox: str | None = None,
    approval: str | None = None,
    proxy_enabled: bool = False,
    http_proxy: str | None = None,
    https_proxy: str | None = None,
    no_proxy: str | None = None,
    collaboration_mode: str | None = None,
    workflow_template: str | None = None,
    delegation_policy: str | None = "auto",
    max_sub_agents: int | None = 3,
    preferred_sub_backend: str | None = None,
    preferred_sub_model: str | None = None,
    description: str | None = None,
    supervision: dict | None = None,
    context_management: dict | None = None,
    token_saving: dict | None = None,
    observe_proxy: dict | None = None,
    browser_control: dict | None = None,
    task_skills: dict | None = None,
    hardware_debug: dict | None = None,
) -> dict:
    resolved_collaboration_mode, resolved_delegation_policy, resolved_max_sub_agents = resolve_task_collaboration(
        collaboration_mode,
        delegation_policy,
        max_sub_agents,
    )
    return {
        "id": task_id,
        "title": title,
        "description": description or "",
        "workspace_id": workspace_id,
        "workspace_path": workspace_path,
        "preferred_backend": backend,
        "preferred_model": model,
        "preferred_reasoning_effort": reasoning_effort,
        "preferred_sandbox": sandbox,
        "preferred_approval": approval,
        "preferred_proxy_enabled": bool(proxy_enabled),
        "preferred_http_proxy": http_proxy,
        "preferred_https_proxy": https_proxy,
        "preferred_no_proxy": no_proxy,
        "preferred_sub_backend": preferred_sub_backend or backend,
        "preferred_sub_model": preferred_sub_model if preferred_sub_model is not None else model,
        "collaboration_mode": resolved_collaboration_mode,
        "workflow_template": normalize_workflow_template(workflow_template),
        "delegation_policy": resolved_delegation_policy,
        "max_sub_agents": resolved_max_sub_agents,
        "supervision": normalize_task_supervision(supervision),
        "context_management": normalize_task_context_management(context_management),
        "token_saving": normalize_task_token_saving(token_saving, context_management),
        "observe_proxy": normalize_task_observe_proxy(observe_proxy),
        "browser_control": normalize_task_browser_control(browser_control),
        "task_skills": normalize_task_skills(task_skills),
        "hardware_debug": normalize_task_hardware_debug(hardware_debug),
        "status": "pending",
        "prompt_file": f"prompts/{task_id}.md",
        "output_file": f"results/{task_id}.md",
        "log_file": f"logs/{task_id}.log",
        "inbox_file": f"inbox/{task_id}.jsonl",
        "created_at": created,
        "started_at": None,
        "finished_at": None,
        "exit_code": None,
        "current_round_id": "round-001",
        "round_sequence": 1,
        "last_final_round_id": None,
        "last_final_at": None,
        "hidden": False,
        "hidden_at": None,
        "deleted_at": None,
        "agents": [
            make_agent(
                "main",
                "task-main",
                backend,
                status="active",
                model=model,
                reasoning_effort=reasoning_effort,
                workspace_path=workspace_path,
                sandbox=sandbox,
                approval=approval,
                proxy_enabled=proxy_enabled,
                created_by="system",
                created_reason="task creation",
            )
        ],
    }


def make_task_round(
    task_id: str,
    sequence: int,
    started_at: str,
    reopened_from_round_id: str | None = None,
    status: str = "active",
) -> dict:
    round_id = f"round-{max(1, sequence):03d}"
    return {
        "task_id": task_id,
        "round_id": round_id,
        "sequence": max(1, sequence),
        "status": status,
        "started_at": started_at,
        "finalized_at": None,
        "final_path": None,
        "final_meta_path": None,
        "reopened_from_round_id": reopened_from_round_id,
    }


def ensure_task_agents(task: dict, backend: str = "codex") -> list[dict]:
    agents = task.setdefault("agents", [])
    if not any(agent.get("id") == "main" for agent in agents):
        agents.insert(
            0,
            make_agent(
                "main",
                "task-main",
                task.get("preferred_backend") or backend,
                status="active",
                model=task.get("preferred_model"),
                workspace_path=task.get("workspace_path"),
                created_by="system",
                created_reason="compatibility upgrade",
            ),
        )
    for agent in agents:
        agent.setdefault("model", task.get("preferred_model"))
        agent.setdefault("reasoning_effort", task.get("preferred_reasoning_effort"))
        agent.setdefault("sandbox", task.get("preferred_sandbox"))
        agent.setdefault("approval", task.get("preferred_approval"))
        agent.setdefault("proxy_enabled", bool(task.get("preferred_proxy_enabled")))
        agent.setdefault("backend_session_id", None)
        agent.setdefault("workspace_path", task.get("workspace_path"))
        agent.setdefault("created_by", "system")
        agent.setdefault("created_reason", "")
        agent.setdefault("status_started_at", None)
        agent.setdefault("last_active_at", None)
        agent.setdefault("last_usage", None)
    return agents


def next_task_id(tasks: list[dict]) -> str:
    nums = []
    for task in tasks:
        raw = str(task.get("id", ""))
        if raw.startswith("task-"):
            try:
                nums.append(int(raw.split("-", 1)[1]))
            except ValueError:
                pass
    return f"task-{(max(nums) if nums else 0) + 1:03d}"


def next_sub_id(task: dict) -> str:
    nums = []
    for agent in task.get("agents", []):
        raw = str(agent.get("id", ""))
        if raw.startswith("sub-"):
            try:
                nums.append(int(raw.split("-", 1)[1]))
            except ValueError:
                pass
    return f"sub-{(max(nums) if nums else 0) + 1:03d}"


def make_session(
    run_id: str,
    task_id: str | None,
    agent_id: str,
    backend: str,
    policy: str = "sticky",
    model: str | None = None,
    workspace_path: str | None = None,
) -> dict:
    scope = f"run:{run_id}:agent:{agent_id}" if task_id is None else f"run:{run_id}:task:{task_id}:agent:{agent_id}"
    return {
        "id": f"{task_id or 'run'}:{agent_id}",
        "run_id": run_id,
        "task_id": task_id,
        "agent_id": agent_id,
        "backend": backend,
        "model": model,
        "policy": policy,
        "scope": scope,
        "backend_session_id": None,
        "history_backend_sessions": [],
        "compact_summary": None,
        "delivered_context_fingerprints": {},
        "workspace_path": workspace_path,
        "status": "active",
        "created_at": utc_now(),
        "updated_at": utc_now(),
    }


def task_prompt(goal: str, mode: str, task: dict, write_scopes: list[str]) -> str:
    scope_text = "\n".join(f"- {scope}" for scope in write_scopes) or "- none"
    mutability_template = "subtask_mutability_implementation.md" if mode == "implementation" else "subtask_mutability_research.md"
    return render_prompt_template(
        "subtask.md",
        goal=goal,
        task_title=task["title"],
        task_description=task.get("description", ""),
        mode=mode,
        mutability=render_prompt_template(mutability_template).strip(),
        write_scope=scope_text,
    )


def enrich_plan(plan: dict, backend: str = "codex") -> dict:
    for task in plan.get("tasks", []):
        task.setdefault("current_round_id", "round-001")
        task.setdefault("round_sequence", 1)
        task.setdefault("last_final_round_id", None)
        task.setdefault("last_final_at", None)
        task.setdefault("hidden", False)
        task.setdefault("hidden_at", None)
        task.setdefault("deleted_at", None)
        task.update(task_metadata_projection(task, backend))
        ensure_task_agents(task, backend)
    plan.setdefault("main_agent", make_agent("main", "run-main", backend, status="active"))
    return plan
