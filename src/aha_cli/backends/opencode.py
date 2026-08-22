"""Experimental OpenCode backend adapter.

The adapter uses a short-lived loopback OpenCode Server for each AHA turn and
reuses OpenCode's persistent session id across turns.  The synchronous message
API is treated as the authoritative final result; this avoids relying on the
CLI JSON stream's currently inconsistent resume/completion behavior.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
from pathlib import Path
import secrets
import shutil
import socket
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from aha_cli import platform
from aha_cli.backends.plugin import (
    BackendResolvedTurn,
    BackendTurnRequest,
    BackendTurnResult,
)
from aha_cli.domain.models import utc_now
from aha_cli.process_control import assign_parent_death
from aha_cli.services.backend_paths import add_user_backend_paths
from aha_cli.services.proxy import apply_proxy_environment
from aha_cli.store.filesystem import append_event_to_file
from aha_cli.store.io import read_json, write_json


OPENCODE_MODEL_CACHE_TTL_SECONDS = 300.0
OPENCODE_START_TIMEOUT_SECONDS = 15.0
OPENCODE_TURN_TIMEOUT_SECONDS = 3600.0
OPENCODE_REASONING_EFFORTS = ("low", "medium", "high", "max")
OPENCODE_SESSION_STORE_VERSION = 1

_MODEL_CACHE: dict[str, tuple[float, list[dict]]] = {}
_MODEL_LOADING: set[str] = set()
_MODEL_LOCK = threading.Lock()


def _config(config: dict | None) -> dict:
    if not isinstance(config, dict):
        return {}
    section = config.get("opencode")
    return section if isinstance(section, dict) else {}


def opencode_binary(config: dict | None = None) -> str:
    return str(_config(config).get("bin") or "opencode").strip() or "opencode"


def _load_opencode_model_options(binary: str) -> list[dict]:
    env = os.environ.copy()
    add_user_backend_paths(env)
    try:
        completed = subprocess.run(
            platform.spawn_command([binary, "models"]),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            timeout=10,
            env=env,
            **platform.hidden_subprocess_kwargs(),
        )
    except (OSError, subprocess.SubprocessError):
        completed = None
    options = [{"name": "", "label": "default"}]
    seen: set[str] = set()
    if completed is not None and completed.returncode == 0:
        for raw in completed.stdout.splitlines():
            name = raw.strip()
            if not name or "/" not in name or name in seen:
                continue
            seen.add(name)
            options.append({"name": name, "label": name})
    with _MODEL_LOCK:
        _MODEL_CACHE[binary] = (
            time.monotonic(),
            [dict(item) for item in options],
        )
        _MODEL_LOADING.discard(binary)
    return options


def opencode_model_options(config: dict | None = None) -> list[dict]:
    binary = opencode_binary(config)
    now = time.monotonic()
    with _MODEL_LOCK:
        cached = _MODEL_CACHE.get(binary)
        if cached and now - cached[0] < OPENCODE_MODEL_CACHE_TTL_SECONDS:
            return [dict(item) for item in cached[1]]
        if binary not in _MODEL_LOADING:
            _MODEL_LOADING.add(binary)
            threading.Thread(
                target=_load_opencode_model_options,
                args=(binary,),
                name="aha-opencode-model-catalog",
                daemon=True,
            ).start()
    # Native catalog discovery must never block bootstrap/task APIs. AHA
    # Provider-backed env selectors are added independently by the registry.
    return [{"name": "", "label": "default"}]


def normalize_opencode_reasoning_effort(value: object) -> str | None:
    effort = str(value or "").strip().lower()
    if not effort or effort in {"default", "none", "null"}:
        return None
    if effort not in OPENCODE_REASONING_EFFORTS:
        raise ValueError(f"unknown reasoning effort: {value}")
    return effort


def resolve_opencode_turn(
    *,
    config: dict,
    model: str | None,
    reasoning_effort: str | None,
    task_scoped: bool,
    session: dict | None = None,
    requested_model_override: str | None = None,
    requested_model_override_set: bool = False,
) -> BackendResolvedTurn:
    del task_scoped
    section = _config(config)
    configured_model = str(model or section.get("model") or "").strip() or None
    normalized_model = configured_model or str((session or {}).get("model") or "").strip() or None
    requested_model = requested_model_override
    if not requested_model_override_set:
        requested_model = configured_model or (session or {}).get("requested_model") or normalized_model
    backend_config = dict(section)
    command_model = normalized_model
    if normalized_model and normalized_model.startswith("env:"):
        group_name = normalized_model.split(":", 1)[1].strip()
        groups = section.get("env") if isinstance(section.get("env"), list) else []
        group = next(
            (
                item
                for item in groups
                if isinstance(item, dict)
                and str(item.get("name") or "").strip() == group_name
            ),
            {},
        )
        provider_id = str(group.get("AHA_PROVIDER_ID") or "").strip()
        model_id = str(group.get("OPENCODE_MODEL") or "").strip()
        provider = next(
            (
                item
                for item in config.get("providers", [])
                if isinstance(item, dict)
                and str(item.get("id") or "").strip() == provider_id
            ),
            {},
        )
        binding = next(
            (
                item
                for item in config.get("configured_models", [])
                if isinstance(item, dict)
                and str(item.get("backend") or "").strip() == "opencode"
                and str(item.get("provider_id") or "").strip() == provider_id
                and str(item.get("model_id") or "").strip() == model_id
            ),
            {},
        )
        if provider_id and model_id and provider:
            command_model = (
                f"opencode/{model_id}"
                if is_opencode_zen_url(provider.get("base_url"))
                else f"{provider_id}/{model_id}"
            )
            backend_config["_aha_provider"] = dict(provider)
            backend_config["_aha_binding"] = {
                **dict(binding),
                "provider_id": provider_id,
                "model_id": model_id,
                "wire_api": str(
                    binding.get("wire_api")
                    or group.get("OPENCODE_WIRE_API")
                    or "chat_completions"
                ),
            }
    return BackendResolvedTurn(
        requested_model=requested_model,
        command_model=command_model,
        resolved_model=command_model,
        reasoning_effort=normalize_opencode_reasoning_effort(reasoning_effort),
        backend_config=backend_config,
        extras={
            "configured_model": configured_model,
            "normalized_model": normalized_model,
            "agent": str(section.get("agent") or "build").strip() or "build",
        },
    )


def _permission_config(sandbox: str) -> dict:
    permissions: dict[str, object] = {
        "*": "allow",
        "task": "deny",
        "question": "deny",
        "doom_loop": "deny",
    }
    if sandbox == "read-only":
        permissions.update({
            "edit": "deny",
            "bash": "deny",
            "external_directory": "deny",
        })
    elif sandbox == "workspace-write":
        permissions["external_directory"] = "deny"
    return permissions


def _server_config_content(request: BackendTurnRequest) -> str:
    payload: dict[str, object] = {
        "default_agent": str(request.resolved.extras.get("agent") or "build")
    }
    provider = request.resolved.backend_config.get("_aha_provider")
    binding = request.resolved.backend_config.get("_aha_binding")
    if (
        isinstance(provider, dict)
        and isinstance(binding, dict)
        and not is_opencode_zen_url(provider.get("base_url"))
    ):
        provider_id = str(binding.get("provider_id") or provider.get("id") or "").strip()
        model_id = str(binding.get("model_id") or "").strip()
        wire_api = str(binding.get("wire_api") or "chat_completions").strip()
        npm = {
            "responses": "@ai-sdk/openai",
            "chat_completions": "@ai-sdk/openai-compatible",
            "anthropic_messages": "@ai-sdk/anthropic",
        }.get(wire_api, "@ai-sdk/openai-compatible")
        base_url = str(
            (
                provider.get("anthropic_base_url")
                if wire_api == "anthropic_messages"
                else provider.get("base_url")
            )
            or ""
        ).strip().rstrip("/")
        env_key = _provider_env_key(provider_id)
        options: dict[str, object] = {"baseURL": base_url}
        if str(provider.get("credential") or "").strip():
            options["apiKey"] = f"{{env:{env_key}}}"
        model_config: dict[str, object] = {
            "name": str(binding.get("name") or model_id).strip() or model_id,
        }
        try:
            context_window = int(str(binding.get("context_window") or "0"))
        except ValueError:
            context_window = 0
        try:
            output_limit = int(str(binding.get("max_output_tokens") or "0"))
        except ValueError:
            output_limit = 0
        if context_window > 0 and output_limit > 0:
            model_config["limit"] = {
                "context": context_window,
                "output": output_limit,
            }
        if provider_id and model_id and base_url:
            payload["provider"] = {
                provider_id: {
                    "npm": npm,
                    "name": str(provider.get("name") or provider_id),
                    "options": options,
                    "models": {model_id: model_config},
                }
            }
    return json.dumps(payload, ensure_ascii=False)


def _provider_env_key(provider_id: str) -> str:
    digest = hashlib.sha256(str(provider_id or "").encode("utf-8")).hexdigest()[:12]
    return f"AHA_OPENCODE_PROVIDER_KEY_{digest}".upper()


def _provider_environment(request: BackendTurnRequest) -> dict[str, str]:
    provider = request.resolved.backend_config.get("_aha_provider")
    binding = request.resolved.backend_config.get("_aha_binding")
    if not isinstance(provider, dict) or not isinstance(binding, dict):
        return {}
    if is_opencode_zen_url(provider.get("base_url")):
        return {}
    credential = str(provider.get("credential") or "").strip()
    provider_id = str(binding.get("provider_id") or provider.get("id") or "").strip()
    if not credential or not provider_id:
        return {}
    return {_provider_env_key(provider_id): credential}


def _usage_value(value: object) -> int | float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return value
    try:
        return int(str(value))
    except (TypeError, ValueError):
        return None


def normalize_opencode_usage(usage: dict) -> dict:
    normalized = dict(usage) if isinstance(usage, dict) else {}
    cache = normalized.get("cache") if isinstance(normalized.get("cache"), dict) else {}
    aliases = {
        "input_tokens": normalized.get("input"),
        "output_tokens": normalized.get("output"),
        "reasoning_output_tokens": normalized.get("reasoning"),
        "cache_read_input_tokens": cache.get("read"),
        "cache_creation_input_tokens": cache.get("write"),
        "total_tokens": normalized.get("total"),
    }
    for key, value in aliases.items():
        if key in normalized:
            continue
        parsed = _usage_value(value)
        if parsed is not None:
            normalized[key] = parsed
    return normalized


def _opencode_session_store_root(
    aha_home: Path,
    run_id: str,
    task_id: str | None,
    target: str | None,
) -> Path:
    scope = "\0".join(
        (
            str(run_id or ""),
            str(task_id or "run"),
            str(target or "main"),
        )
    )
    digest = hashlib.sha256(scope.encode("utf-8")).hexdigest()[:20]
    return (
        Path(aha_home)
        / "runs"
        / str(run_id)
        / "runtime"
        / "opencode"
        / digest
    )


def opencode_session_store_root(request: BackendTurnRequest) -> Path:
    return _opencode_session_store_root(
        request.aha_home,
        request.run_id,
        request.task_id,
        request.target,
    )


def _opencode_session_database(store_root: Path) -> Path:
    return store_root / "data" / "opencode" / "opencode.db"


def opencode_session_database_path(
    aha_home: Path,
    run_id: str,
    task_id: str | None,
    target: str | None,
) -> Path:
    return _opencode_session_database(
        _opencode_session_store_root(
            aha_home,
            run_id,
            task_id,
            target,
        )
    )


def _opencode_session_marker(store_root: Path) -> Path:
    return store_root / "session.json"


def write_opencode_session_marker(
    request: BackendTurnRequest,
    session_id: str,
) -> None:
    store_root = opencode_session_store_root(request)
    store_root.mkdir(parents=True, exist_ok=True)
    try:
        store_root.chmod(0o700)
    except OSError:
        pass
    write_json(
        _opencode_session_marker(store_root),
        {
            "version": OPENCODE_SESSION_STORE_VERSION,
            "backend_session_id": str(session_id),
            "updated_at": utc_now(),
        },
    )


def opencode_session_resume_id(
    aha_home: Path,
    run_id: str,
    task_id: str | None,
    target: str | None,
    session: dict | None,
) -> str | None:
    session_id = str((session or {}).get("backend_session_id") or "").strip()
    if not session_id:
        return None
    store_root = _opencode_session_store_root(
        aha_home,
        run_id,
        task_id,
        target,
    )
    if not _opencode_session_database(store_root).is_file():
        return None
    try:
        marker = read_json(_opencode_session_marker(store_root))
    except (OSError, ValueError):
        return None
    if int(marker.get("version") or 0) != OPENCODE_SESSION_STORE_VERSION:
        return None
    return (
        session_id
        if str(marker.get("backend_session_id") or "").strip() == session_id
        else None
    )


def _sqlite_model(value: object) -> dict | str | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        return text
    return parsed if isinstance(parsed, dict) else text


def opencode_session_artifact_info(
    *,
    aha_home: Path,
    run_id: str,
    task_id: str | None,
    target: str | None,
    session_id: str,
) -> dict:
    database = opencode_session_database_path(
        aha_home,
        run_id,
        task_id,
        target,
    )
    base = {
        "id": str(session_id or ""),
        "backend": "opencode",
        "artifact_type": "sqlite",
        "path": str(database),
        "size_bytes": None,
        "exists": False,
        "analysis": {},
    }
    if not session_id or not database.is_file():
        return base
    try:
        stat = database.stat()
    except OSError:
        return base
    base.update({
        "size_bytes": stat.st_size,
        "modified_at": stat.st_mtime,
    })
    try:
        uri = f"file:{database.as_posix()}?mode=ro"
        with sqlite3.connect(uri, uri=True, timeout=1.0) as connection:
            row = connection.execute(
                """
                select
                    model,
                    tokens_input,
                    tokens_output,
                    tokens_reasoning,
                    tokens_cache_read,
                    tokens_cache_write,
                    cost,
                    time_created,
                    time_updated
                from session
                where id = ?
                """,
                (session_id,),
            ).fetchone()
            if row is None:
                return base
            message_count = int(
                connection.execute(
                    "select count(*) from message where session_id = ?",
                    (session_id,),
                ).fetchone()[0]
            )
            part_count = int(
                connection.execute(
                    "select count(*) from part where session_id = ?",
                    (session_id,),
                ).fetchone()[0]
            )
    except (OSError, sqlite3.Error) as exc:
        base["exists"] = True
        base["analysis"] = {"error": str(exc), "artifact_type": "sqlite"}
        return base
    base["exists"] = True
    base["analysis"] = {
        "artifact_type": "sqlite",
        "session_found": True,
        "message_count": message_count,
        "part_count": part_count,
        "model": _sqlite_model(row[0]),
        "cumulative_usage": {
            "input_tokens": int(row[1] or 0),
            "output_tokens": int(row[2] or 0),
            "reasoning_output_tokens": int(row[3] or 0),
            "cache_read_input_tokens": int(row[4] or 0),
            "cache_creation_input_tokens": int(row[5] or 0),
            "cost": float(row[6] or 0.0),
        },
        "time_created": row[7],
        "time_updated": row[8],
    }
    return base


def is_opencode_zen_url(value: object) -> bool:
    text = str(value or "").strip().lower().rstrip("/")
    return text in {
        "https://opencode.ai/zen",
        "https://opencode.ai/zen/v1",
    }


def _catalog_model_mode(model: dict) -> str:
    api = model.get("api") if isinstance(model.get("api"), dict) else {}
    npm = str(api.get("npm") or "")
    if npm == "@ai-sdk/openai":
        return "responses"
    if npm == "@ai-sdk/anthropic":
        return "anthropic_messages"
    return "chat_completions"


def detect_opencode_zen_models(
    binary: str,
    credential: str,
) -> list[dict[str, object]]:
    """Return account-visible OpenCode Zen models via an isolated server.

    The Zen gateway does not expose the generic ``GET /v1/models`` endpoint
    used by AHA's ordinary Provider detector. OpenCode itself owns the model
    catalog, so connect the supplied key to an isolated temporary OpenCode data
    directory, then query the Server provider catalog. The key never reaches
    the user's global OpenCode auth store.
    """
    key = str(credential or "").strip()
    if not key:
        raise ValueError("OpenCode Zen credential is not configured")
    with tempfile.TemporaryDirectory(prefix="aha-opencode-detect-") as tmp:
        temp_root = Path(tmp)
        port = _free_loopback_port()
        base_url = f"http://127.0.0.1:{port}"
        username = "opencode"
        password = secrets.token_urlsafe(24)
        env = os.environ.copy()
        add_user_backend_paths(env)
        env.update({
            "XDG_DATA_HOME": str(temp_root / "data"),
            "XDG_CONFIG_HOME": str(temp_root / "config"),
            "XDG_CACHE_HOME": str(temp_root / "cache"),
            "XDG_STATE_HOME": str(temp_root / "state"),
            "OPENCODE_SERVER_USERNAME": username,
            "OPENCODE_SERVER_PASSWORD": password,
            "OPENCODE_DISABLE_AUTOUPDATE": "true",
            "OPENCODE_DISABLE_DEFAULT_PLUGINS": "true",
            "OPENCODE_DISABLE_CLAUDE_CODE": "true",
            "OPENCODE_CONFIG_CONTENT": json.dumps({"default_agent": "build"}),
        })
        process = subprocess.Popen(
            platform.spawn_command([
                binary,
                "serve",
                "--hostname",
                "127.0.0.1",
                "--port",
                str(port),
                "--pure",
            ]),
            cwd=temp_root,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=False,
            **platform.hidden_subprocess_kwargs(),
        )
        assign_parent_death(process)
        try:
            deadline = time.monotonic() + OPENCODE_START_TIMEOUT_SECONDS
            while time.monotonic() < deadline:
                if process.poll() is not None:
                    raise RuntimeError(
                        f"OpenCode detector exited with code {process.returncode}"
                    )
                try:
                    health = _json_request(
                        base_url,
                        "/global/health",
                        username=username,
                        password=password,
                        timeout=1,
                    )
                    if isinstance(health, dict) and health.get("healthy"):
                        break
                except (RuntimeError, URLError, OSError, ValueError):
                    time.sleep(0.1)
            else:
                raise RuntimeError("timed out starting OpenCode model detector")
            _json_request(
                base_url,
                "/auth/opencode",
                username=username,
                password=password,
                method="PUT",
                body={"type": "api", "key": key},
                timeout=10,
            )
            payload = _json_request(
                base_url,
                "/provider",
                username=username,
                password=password,
                timeout=15,
            )
            providers = (
                payload.get("all")
                if isinstance(payload, dict) and isinstance(payload.get("all"), list)
                else []
            )
            provider = next(
                (
                    item
                    for item in providers
                    if isinstance(item, dict)
                    and str(item.get("id") or "") == "opencode"
                ),
                {},
            )
            raw_models = (
                provider.get("models")
                if isinstance(provider.get("models"), dict)
                else {}
            )
            models: list[dict[str, object]] = []
            for model_id, raw in raw_models.items():
                model = raw if isinstance(raw, dict) else {}
                if str(model.get("status") or "active") != "active":
                    continue
                limit = model.get("limit") if isinstance(model.get("limit"), dict) else {}
                entry: dict[str, object] = {
                    "id": str(model_id),
                    "mode": _catalog_model_mode(model),
                }
                context = limit.get("context")
                output = limit.get("output")
                if isinstance(context, int) and context > 0:
                    entry["max_input_tokens"] = context
                if isinstance(output, int) and output > 0:
                    entry["max_output_tokens"] = output
                models.append(entry)
            return sorted(models, key=lambda item: str(item["id"]))
        finally:
            _stop_server(process)


def _windows_opencode_available(binary: str) -> bool:
    candidate = Path(str(binary or "")).expanduser()
    if candidate.is_absolute() and candidate.is_file():
        return True
    return bool(shutil.which(str(binary or "")))


def _wsl_distro_candidates(root: Path, config: dict) -> list[str]:
    section = _config(config)
    candidates: list[str] = []
    preferred = str(section.get("wsl_distro") or "").strip()
    if preferred:
        candidates.append(preferred)
    env_distro = str(
        os.environ.get("AHA_WSL_DISTRO")
        or os.environ.get("WSL_DISTRO_NAME")
        or ""
    ).strip()
    if env_distro and env_distro not in candidates:
        candidates.append(env_distro)
    try:
        from aha_cli.services.wsl_backend import wsl_backends_cache_path

        path = wsl_backends_cache_path(root)
        payload = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        if isinstance(payload, dict):
            ordered = sorted(
                (
                    (str(name), item)
                    for name, item in payload.items()
                    if isinstance(item, dict)
                ),
                key=lambda pair: float(pair[1].get("detected_at") or 0),
                reverse=True,
            )
            for name, item in ordered:
                backends = item.get("backends")
                if (
                    isinstance(backends, dict)
                    and backends.get("opencode")
                    and name not in candidates
                ):
                    candidates.append(name)
    except (OSError, ValueError, TypeError):
        pass
    try:
        from aha_cli.services.wsl_backend import _wsl_executable

        completed = subprocess.run(
            [_wsl_executable(), "-l", "-q"],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            check=False,
            timeout=10,
            **platform.hidden_subprocess_kwargs(),
        )
        text = (completed.stdout or b"").replace(b"\0", b"").decode(
            "utf-8",
            errors="replace",
        )
        for raw in text.splitlines():
            name = raw.strip()
            if name and name not in candidates:
                candidates.append(name)
    except (OSError, subprocess.SubprocessError):
        pass
    return candidates


def _detect_opencode_zen_models_in_wsl(
    root: Path,
    config: dict,
    credential: str,
) -> list[dict[str, object]]:
    from aha_cli.services.onebin import authoritative_onebin_path
    from aha_cli.services.wsl_backend import _wsl_executable, wsl_backends_for_workspace
    from aha_cli.store.ws_target import (
        windows_path_to_wsl,
        wsl_native_home,
        wsl_workspace_native_path,
    )

    onebin = authoritative_onebin_path()
    if onebin is None:
        raise RuntimeError(
            "AHA is not running from an authoritative onebin; WSL OpenCode detection is unavailable"
        )
    aha_bin = windows_path_to_wsl(onebin) or wsl_workspace_native_path(onebin)
    aha_home = wsl_native_home(root)
    if not aha_bin or not aha_home:
        raise RuntimeError("failed to map the Windows AHA onebin/home into WSL")
    errors: list[str] = []
    for distro in _wsl_distro_candidates(root, config):
        backends = wsl_backends_for_workspace(root, distro)
        opencode_bin = str(backends.get("opencode") or "").strip()
        python_bin = str(backends.get("python3") or "").strip()
        if not opencode_bin or not python_bin:
            continue
        command = [
            _wsl_executable(),
            "-d",
            distro,
            "--",
            python_bin,
            aha_bin,
            "--home",
            aha_home,
            "opencode-detect-models",
            "--opencode-bin",
            opencode_bin,
        ]
        env = {"SystemRoot": str(
            os.environ.get("SystemRoot")
            or (str(os.environ.get("SystemDrive") or r"C:").rstrip("\\") + r"\Windows")
        )}
        for key in ("SystemDrive", "WINDIR", "COMSPEC", "TEMP", "TMP"):
            value = os.environ.get(key)
            if value:
                env[key] = value
        try:
            completed = subprocess.run(
                command,
                input=json.dumps({"credential": credential}),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                check=False,
                timeout=90,
                env=env,
                **platform.hidden_subprocess_kwargs(),
            )
        except (OSError, subprocess.SubprocessError) as exc:
            errors.append(f"{distro}: {exc}")
            continue
        if completed.returncode != 0:
            errors.append(
                f"{distro}: {completed.stderr.strip() or completed.stdout.strip() or 'helper failed'}"
            )
            continue
        try:
            payload = json.loads(completed.stdout)
        except json.JSONDecodeError as exc:
            errors.append(f"{distro}: invalid helper JSON: {exc}")
            continue
        models = payload.get("models") if isinstance(payload, dict) else None
        if isinstance(models, list):
            return [item for item in models if isinstance(item, dict)]
    detail = "; ".join(errors) if errors else "no WSL distro with native OpenCode was found"
    raise RuntimeError(f"WSL OpenCode model detection failed: {detail}")


def detect_opencode_zen_models_for_runtime(
    root: Path,
    config: dict,
    credential: str,
) -> list[dict[str, object]]:
    binary = opencode_binary(config)
    if sys.platform != "win32" or _windows_opencode_available(binary):
        return detect_opencode_zen_models(binary, credential)
    return _detect_opencode_zen_models_in_wsl(root, config, credential)


def _permission_rules(sandbox: str) -> list[dict]:
    return [
        {"permission": permission, "pattern": "*", "action": action}
        for permission, action in _permission_config(sandbox).items()
    ]


def _free_loopback_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _auth_header(username: str, password: str) -> str:
    token = base64.b64encode(f"{username}:{password}".encode("utf-8")).decode("ascii")
    return f"Basic {token}"


def _json_request(
    base_url: str,
    path: str,
    *,
    username: str,
    password: str,
    method: str = "GET",
    body: dict | None = None,
    timeout: float = 30.0,
) -> object:
    data = None if body is None else json.dumps(body, ensure_ascii=False).encode("utf-8")
    request = Request(
        f"{base_url}{path}",
        data=data,
        method=method,
        headers={
            "Authorization": _auth_header(username, password),
            **({"Content-Type": "application/json"} if data is not None else {}),
        },
    )
    try:
        with urlopen(request, timeout=timeout) as response:
            raw = response.read().decode("utf-8", errors="replace")
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace").strip()
        raise RuntimeError(
            f"OpenCode HTTP {exc.code} for {path}: {detail or exc.reason}"
        ) from exc
    return json.loads(raw) if raw.strip() else {}


def _start_server(request: BackendTurnRequest) -> tuple[subprocess.Popen, str, str, str, object]:
    port = _free_loopback_port()
    base_url = f"http://127.0.0.1:{port}"
    username = "opencode"
    password = secrets.token_urlsafe(24)
    log_path = (
        request.aha_home
        / "runs"
        / request.run_id
        / "logs"
        / f"opencode-{request.task_id or 'run'}-{request.target or 'main'}.log"
    )
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_file = log_path.open("ab")
    env = os.environ.copy()
    add_user_backend_paths(env)
    apply_proxy_environment(env, request.resolved.proxy_env)
    env.update({
        "OPENCODE_SERVER_USERNAME": username,
        "OPENCODE_SERVER_PASSWORD": password,
        "OPENCODE_DISABLE_AUTOUPDATE": "true",
        "OPENCODE_DISABLE_DEFAULT_PLUGINS": "true",
        "OPENCODE_DISABLE_CLAUDE_CODE": "true",
        "OPENCODE_CONFIG_CONTENT": _server_config_content(request),
    })
    provider = request.resolved.backend_config.get("_aha_provider")
    runtime_dir = None
    auth_file = None
    if isinstance(provider, dict):
        store_root = opencode_session_store_root(request)
        data_home = store_root / "data"
        data_home.mkdir(parents=True, exist_ok=True)
        try:
            store_root.chmod(0o700)
            data_home.chmod(0o700)
        except OSError:
            pass
        runtime_dir = tempfile.TemporaryDirectory(prefix="aha-opencode-runtime-")
        runtime_root = Path(runtime_dir.name)
        auth_file = data_home / "opencode" / "auth.json"
        try:
            auth_file.unlink(missing_ok=True)
        except OSError:
            pass
        env.update({
            "XDG_DATA_HOME": str(data_home),
            "XDG_CONFIG_HOME": str(runtime_root / "config"),
            "XDG_CACHE_HOME": str(runtime_root / "cache"),
            "XDG_STATE_HOME": str(runtime_root / "state"),
        })
    env.update(_provider_environment(request))
    command = platform.spawn_command([
        request.binary,
        "serve",
        "--hostname",
        "127.0.0.1",
        "--port",
        str(port),
        "--pure",
    ])
    try:
        process = subprocess.Popen(
            command,
            cwd=request.cwd,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=log_file,
            stderr=subprocess.STDOUT,
            start_new_session=False,
            **platform.hidden_subprocess_kwargs(),
        )
    except Exception:
        log_file.close()
        raise
    log_file.close()
    assign_parent_death(process)
    if runtime_dir is not None:
        process._aha_opencode_runtime_dir = runtime_dir  # type: ignore[attr-defined]
    if auth_file is not None:
        process._aha_opencode_auth_file = auth_file  # type: ignore[attr-defined]
    try:
        deadline = time.monotonic() + OPENCODE_START_TIMEOUT_SECONDS
        last_error = ""
        while time.monotonic() < deadline:
            if process.poll() is not None:
                raise RuntimeError(f"OpenCode server exited with code {process.returncode}")
            try:
                health = _json_request(
                    base_url,
                    "/global/health",
                    username=username,
                    password=password,
                    timeout=1,
                )
                if isinstance(health, dict) and health.get("healthy"):
                    if isinstance(provider, dict):
                        credential = str(provider.get("credential") or "").strip()
                        if is_opencode_zen_url(provider.get("base_url")) and credential:
                            _json_request(
                                base_url,
                                "/auth/opencode",
                                username=username,
                                password=password,
                                method="PUT",
                                body={"type": "api", "key": credential},
                                timeout=10,
                            )
                    return process, base_url, username, password, health
            except (HTTPError, URLError, OSError, ValueError) as exc:
                last_error = str(exc)
            time.sleep(0.1)
        raise RuntimeError(f"timed out waiting for OpenCode server: {last_error}")
    except Exception:
        _stop_server(process)
        raise


def _stop_server(process: subprocess.Popen) -> None:
    try:
        if process.poll() is None:
            try:
                process.terminate()
                process.wait(timeout=3)
            except (OSError, subprocess.TimeoutExpired):
                try:
                    process.kill()
                except OSError:
                    pass
    finally:
        auth_file = getattr(process, "_aha_opencode_auth_file", None)
        if isinstance(auth_file, Path):
            try:
                auth_file.unlink(missing_ok=True)
            except OSError:
                pass
        runtime_dir = getattr(process, "_aha_opencode_runtime_dir", None)
        if runtime_dir is not None:
            try:
                runtime_dir.cleanup()
            except Exception:  # noqa: BLE001
                pass


def _model_payload(model: str | None) -> dict | None:
    value = str(model or "").strip()
    if not value or "/" not in value:
        return None
    provider_id, model_id = value.split("/", 1)
    if not provider_id or not model_id:
        return None
    return {"providerID": provider_id, "modelID": model_id}


def _tool_command(part: dict) -> str:
    name = str(part.get("tool") or part.get("name") or "tool")
    state = part.get("state") if isinstance(part.get("state"), dict) else {}
    payload = state.get("input") if isinstance(state.get("input"), dict) else part.get("input")
    if isinstance(payload, dict) and payload:
        return f"{name} {json.dumps(payload, ensure_ascii=False, sort_keys=True)}"
    return name


def _part_session_id(part: dict) -> str:
    return str(
        part.get("sessionID")
        or part.get("sessionId")
        or part.get("session_id")
        or ""
    )


class _OpenCodeEventStream:
    def __init__(
        self,
        request: BackendTurnRequest,
        *,
        base_url: str,
        username: str,
        password: str,
        session_id: str,
    ) -> None:
        self.request = request
        self.base_url = base_url
        self.username = username
        self.password = password
        self.session_id = session_id
        self.started_tools: set[str] = set()
        self.finished_tools: set[str] = set()
        self.text_emitted = False
        self._stop = threading.Event()
        self._response = None
        self._thread = threading.Thread(
            target=self._run,
            name=f"aha-opencode-events-{session_id}",
            daemon=True,
        )

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        response = self._response
        if response is not None:
            try:
                response.close()
            except Exception:  # noqa: BLE001
                pass
        self._thread.join(timeout=2)

    def _run(self) -> None:
        request = Request(
            f"{self.base_url}/event",
            headers={"Authorization": _auth_header(self.username, self.password)},
            method="GET",
        )
        try:
            with urlopen(request, timeout=OPENCODE_TURN_TIMEOUT_SECONDS) as response:
                self._response = response
                while not self._stop.is_set():
                    raw = response.readline()
                    if not raw:
                        break
                    line = raw.decode("utf-8", errors="replace").strip()
                    if not line.startswith("data:"):
                        continue
                    try:
                        event = json.loads(line[5:].strip())
                    except json.JSONDecodeError:
                        continue
                    self._handle_event(event)
        except Exception:  # noqa: BLE001 - final REST response remains authoritative
            return
        finally:
            self._response = None

    def _handle_event(self, envelope: object) -> None:
        if not isinstance(envelope, dict):
            return
        payload = envelope.get("payload")
        event = payload if isinstance(payload, dict) else envelope
        event_type = str(event.get("type") or "")
        properties = (
            event.get("properties")
            if isinstance(event.get("properties"), dict)
            else {}
        )
        part = properties.get("part") if isinstance(properties.get("part"), dict) else {}
        info = properties.get("info") if isinstance(properties.get("info"), dict) else {}
        event_session_id = str(
            properties.get("sessionID")
            or properties.get("sessionId")
            or info.get("sessionID")
            or _part_session_id(part)
            or ""
        )
        if event_session_id and event_session_id != self.session_id:
            return
        if event_type == "message.part.updated":
            self._handle_part(part, str(properties.get("delta") or ""))
        elif event_type == "session.error":
            append_event_to_file(
                self.request.events_file,
                self.request.run_id,
                "agent_error",
                {
                    "source": self.request.source,
                    "task_id": self.request.task_id,
                    "target": self.request.target,
                    "message": str(properties.get("error") or "OpenCode session error"),
                },
            )
        elif event_type == "session.compacted":
            append_event_to_file(
                self.request.events_file,
                self.request.run_id,
                "backend_auto_context_compact",
                {
                    "source": self.request.source,
                    "task_id": self.request.task_id,
                    "target": self.request.target,
                    "backend_session_id": self.session_id,
                    "reason": "opencode_session_compacted",
                },
            )

    def _handle_part(self, part: dict, delta: str) -> None:
        part_type = str(part.get("type") or "")
        if part_type == "text" and delta:
            self.text_emitted = True
            append_event_to_file(
                self.request.events_file,
                self.request.run_id,
                "agent_message",
                {
                    "source": self.request.source,
                    "task_id": self.request.task_id,
                    "target": self.request.target,
                    "text": delta,
                    "item_type": "agent_message",
                    "partial": True,
                },
            )
            return
        if part_type != "tool":
            return
        state = part.get("state") if isinstance(part.get("state"), dict) else {}
        status = str(state.get("status") or "")
        call_id = str(part.get("callID") or part.get("id") or _tool_command(part))
        base = {
            "source": self.request.source,
            "task_id": self.request.task_id,
            "target": self.request.target,
            "tool_name": part.get("tool") or part.get("name"),
            "tool_use_id": call_id,
            "command": _tool_command(part),
        }
        if call_id not in self.started_tools:
            self.started_tools.add(call_id)
            append_event_to_file(
                self.request.events_file,
                self.request.run_id,
                "agent_command_started",
                base | {"status": "in_progress", "exit_code": None},
            )
            if self.request.event_callback is not None:
                self.request.event_callback("agent_command_started", base)
        if status not in {"completed", "error"} or call_id in self.finished_tools:
            return
        self.finished_tools.add(call_id)
        output = str(state.get("output") or state.get("error") or "")
        finished = base | {
            "status": "failed" if status == "error" else "completed",
            "exit_code": 1 if status == "error" else 0,
            "output_tail": output[-1200:],
            "output_chars": len(output),
        }
        append_event_to_file(
            self.request.events_file,
            self.request.run_id,
            "agent_command_finished",
            finished,
        )
        if self.request.event_callback is not None:
            self.request.event_callback("agent_command_finished", finished)


def _emit_response_events(
    request: BackendTurnRequest,
    response: dict,
    *,
    started_tools: set[str] | None = None,
    finished_tools: set[str] | None = None,
    text_already_emitted: bool = False,
) -> str:
    info = response.get("info") if isinstance(response.get("info"), dict) else {}
    parts = response.get("parts") if isinstance(response.get("parts"), list) else []
    texts: list[str] = []
    started_tools = started_tools if started_tools is not None else set()
    finished_tools = finished_tools if finished_tools is not None else set()
    for part in parts:
        if not isinstance(part, dict):
            continue
        part_type = str(part.get("type") or "")
        if part_type == "text" and part.get("text"):
            texts.append(str(part.get("text") or ""))
            continue
        if part_type != "tool":
            continue
        state = part.get("state") if isinstance(part.get("state"), dict) else {}
        status = str(state.get("status") or "")
        call_id = str(part.get("callID") or part.get("id") or _tool_command(part))
        base = {
            "source": request.source,
            "task_id": request.task_id,
            "target": request.target,
            "tool_name": part.get("tool") or part.get("name"),
            "tool_use_id": call_id,
            "command": _tool_command(part),
        }
        if call_id not in started_tools:
            append_event_to_file(
                request.events_file,
                request.run_id,
                "agent_command_started",
                base | {"status": "in_progress", "exit_code": None},
            )
            if request.event_callback is not None:
                request.event_callback("agent_command_started", base)
            started_tools.add(call_id)
        output = str(state.get("output") or state.get("error") or "")
        finished = base | {
            "status": "failed" if status == "error" else "completed",
            "exit_code": 1 if status == "error" else 0,
            "output_tail": output[-1200:],
            "output_chars": len(output),
        }
        if call_id not in finished_tools:
            append_event_to_file(
                request.events_file,
                request.run_id,
                "agent_command_finished",
                finished,
            )
            if request.event_callback is not None:
                request.event_callback("agent_command_finished", finished)
            finished_tools.add(call_id)
    text = "\n".join(item for item in texts if item).strip()
    if text and not text_already_emitted:
        append_event_to_file(
            request.events_file,
            request.run_id,
            "agent_message",
            {
                "source": request.source,
                "task_id": request.task_id,
                "target": request.target,
                "text": text,
                "item_type": "agent_message",
            },
        )
    usage = normalize_opencode_usage(
        info.get("tokens") if isinstance(info.get("tokens"), dict) else {}
    )
    append_event_to_file(
        request.events_file,
        request.run_id,
        "agent_usage",
        {
            "source": request.source,
            "task_id": request.task_id,
            "target": request.target,
            "backend_session_id": request.session.get("backend_session_id"),
            "usage": {
                **usage,
                **({"cost": info.get("cost")} if info.get("cost") is not None else {}),
            },
        },
    )
    return text


def run_opencode_turn(request: BackendTurnRequest) -> BackendTurnResult:
    process = None
    event_stream = None
    try:
        process, base_url, username, password, health = _start_server(request)
        session_id = str(request.session.get("backend_session_id") or "").strip()
        if not session_id:
            created = _json_request(
                base_url,
                "/session",
                username=username,
                password=password,
                method="POST",
                body={
                    "title": f"AHA {request.run_id}/{request.task_id or request.target or 'main'}",
                    "permission": _permission_rules(request.sandbox),
                },
                timeout=10,
            )
            if not isinstance(created, dict) or not created.get("id"):
                raise RuntimeError("OpenCode did not return a session id")
            session_id = str(created["id"])
            request.session["backend_session_id"] = session_id
            request.session["status"] = "active"
            request.session["updated_at"] = utc_now()
            if isinstance(request.resolved.backend_config.get("_aha_provider"), dict):
                write_opencode_session_marker(request, session_id)
            append_event_to_file(
                request.events_file,
                request.run_id,
                "agent_thread",
                {
                    "source": request.source,
                    "task_id": request.task_id,
                    "target": request.target,
                    "thread_id": session_id,
                    "backend_session_id": session_id,
                    "server_version": (
                        health.get("version") if isinstance(health, dict) else None
                    ),
                },
            )
        event_stream = _OpenCodeEventStream(
            request,
            base_url=base_url,
            username=username,
            password=password,
            session_id=session_id,
        )
        event_stream.start()
        body: dict[str, object] = {
            "agent": str(request.resolved.extras.get("agent") or "aha"),
            "parts": [{"type": "text", "text": request.prompt}],
        }
        model = _model_payload(request.resolved.command_model)
        if model:
            body["model"] = model
        if request.resolved.reasoning_effort:
            body["variant"] = request.resolved.reasoning_effort
        response = _json_request(
            base_url,
            f"/session/{session_id}/message",
            username=username,
            password=password,
            method="POST",
            body=body,
            timeout=OPENCODE_TURN_TIMEOUT_SECONDS,
        )
        if not isinstance(response, dict):
            raise RuntimeError("OpenCode returned an invalid message response")
        event_stream.stop()
        reply = _emit_response_events(
            request,
            response,
            started_tools=event_stream.started_tools,
            finished_tools=event_stream.finished_tools,
            text_already_emitted=event_stream.text_emitted,
        )
        if not reply:
            raise RuntimeError("OpenCode completed without assistant text")
        request.output_file.parent.mkdir(parents=True, exist_ok=True)
        request.output_file.write_text(reply, encoding="utf-8")
        return BackendTurnResult(exit_code=0, reply=reply, session=request.session)
    except Exception as exc:  # noqa: BLE001 - backend errors become normal turn failures
        message = f"OpenCode backend failed: {type(exc).__name__}: {exc}"
        append_event_to_file(
            request.events_file,
            request.run_id,
            "agent_error",
            {
                "source": request.source,
                "task_id": request.task_id,
                "target": request.target,
                "message": message,
            },
        )
        request.output_file.parent.mkdir(parents=True, exist_ok=True)
        request.output_file.write_text(message, encoding="utf-8")
        return BackendTurnResult(exit_code=1, reply=message, session=request.session)
    finally:
        if event_stream is not None:
            event_stream.stop()
        if process is not None:
            _stop_server(process)


__all__ = [
    "OPENCODE_REASONING_EFFORTS",
    "normalize_opencode_reasoning_effort",
    "_load_opencode_model_options",
    "detect_opencode_zen_models",
    "detect_opencode_zen_models_for_runtime",
    "is_opencode_zen_url",
    "normalize_opencode_usage",
    "opencode_binary",
    "opencode_model_options",
    "opencode_session_artifact_info",
    "opencode_session_database_path",
    "opencode_session_resume_id",
    "opencode_session_store_root",
    "resolve_opencode_turn",
    "run_opencode_turn",
    "write_opencode_session_marker",
]
