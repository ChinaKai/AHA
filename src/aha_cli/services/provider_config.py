from __future__ import annotations

import hashlib
import re
from typing import Iterable

PROVIDER_AUTH_STYLES = {"auto", "bearer", "x-api-key", "none"}
PROVIDER_WIRE_APIS = {"responses", "chat_completions", "anthropic_messages"}
PROVIDER_BACKENDS = {"codex", "claude"}
_CREDENTIAL_KEYS = {
    "credential",
    "api_key",
    "auth_token",
    "OPENAI_API_KEY",
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
    "OPENAI_ACCESS_TOKEN",
}


def _text(value: object) -> str:
    return str(value or "").strip()


def _provider_id(name: str, base_url: str) -> str:
    digest = hashlib.sha256(f"{name}\0{base_url}".encode("utf-8")).hexdigest()[:12]
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:36] or "provider"
    return f"{slug}-{digest}"


def normalize_providers(value: object, existing: object = None) -> list[dict[str, object]]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise ValueError("providers must be a list")
    existing_by_id = {
        _text(item.get("id")): item
        for item in (existing if isinstance(existing, list) else [])
        if isinstance(item, dict) and _text(item.get("id"))
    }
    providers: list[dict[str, object]] = []
    seen: set[str] = set()
    for item in value:
        if not isinstance(item, dict):
            raise ValueError("providers entries must be objects")
        name = _text(item.get("name"))
        base_url = _text(item.get("base_url")).rstrip("/")
        if not name or not base_url:
            raise ValueError("provider name and base_url are required")
        provider_id = _text(item.get("id")) or _provider_id(name, base_url)
        if provider_id in seen:
            raise ValueError(f"duplicate provider id: {provider_id}")
        seen.add(provider_id)
        auth_style = _text(item.get("auth_style")) or "auto"
        if auth_style not in PROVIDER_AUTH_STYLES:
            raise ValueError(f"unknown provider auth_style: {auth_style}")
        credential = _text(item.get("credential") or item.get("api_key") or item.get("auth_token"))
        if not credential:
            previous = existing_by_id.get(provider_id, {})
            credential = _text(previous.get("credential"))
        anthropic_base_url = _text(item.get("anthropic_base_url")).rstrip("/")
        provider: dict[str, object] = {
            "id": provider_id,
            "name": name,
            "base_url": base_url,
            "auth_style": auth_style,
        }
        if anthropic_base_url:
            provider["anthropic_base_url"] = anthropic_base_url
        if credential:
            provider["credential"] = credential
        providers.append(provider)
    return providers


def normalize_configured_models(value: object, provider_ids: Iterable[str]) -> list[dict[str, object]]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise ValueError("configured_models must be a list")
    valid_provider_ids = set(provider_ids)
    models: list[dict[str, object]] = []
    seen: set[tuple[str, str, str, str]] = set()
    for item in value:
        if not isinstance(item, dict):
            raise ValueError("configured_models entries must be objects")
        provider_id = _text(item.get("provider_id"))
        model_id = _text(item.get("model_id") or item.get("model"))
        backend = _text(item.get("backend")).lower()
        wire_api = _text(item.get("wire_api")).lower()
        if wire_api == "chat":
            wire_api = "chat_completions"
        if not provider_id or not model_id or not backend or not wire_api:
            raise ValueError("configured model provider_id, model_id, backend, and wire_api are required")
        if provider_id not in valid_provider_ids:
            raise ValueError(f"unknown configured model provider: {provider_id}")
        if backend not in PROVIDER_BACKENDS:
            raise ValueError(f"unknown configured model backend: {backend}")
        if wire_api not in PROVIDER_WIRE_APIS:
            raise ValueError(f"unknown configured model wire_api: {wire_api}")
        key = (provider_id, model_id, backend, wire_api)
        if key in seen:
            continue
        seen.add(key)
        model: dict[str, object] = {
            "provider_id": provider_id,
            "model_id": model_id,
            "backend": backend,
            "wire_api": wire_api,
        }
        for field in ("name", "context_window", "fable_model", "opus_model", "sonnet_model", "haiku_model"):
            if item.get(field) not in (None, ""):
                model[field] = item[field]
        models.append(model)
    return models


def provider_by_id(config: dict, provider_id: str) -> dict:
    for provider in config.get("providers", []):
        if isinstance(provider, dict) and _text(provider.get("id")) == provider_id:
            return provider
    return {}


def public_config(config: dict) -> dict:
    def is_credential_key(key: str) -> bool:
        upper = key.upper()
        return key in _CREDENTIAL_KEYS or upper.endswith(("_API_KEY", "_AUTH_TOKEN", "_ACCESS_TOKEN"))

    def redact(value: object) -> object:
        if isinstance(value, list):
            return [redact(item) for item in value]
        if not isinstance(value, dict):
            return value
        result: dict[str, object] = {}
        configured = False
        for key, item in value.items():
            if is_credential_key(key):
                configured = configured or bool(_text(item))
                continue
            result[key] = redact(item)
        if configured:
            result["credential_configured"] = True
        return result

    return redact(config)  # type: ignore[return-value]


def _binding_name(provider: dict, binding: dict) -> str:
    provider_name = _text(provider.get("name")) or _text(provider.get("id"))
    model_id = _text(binding.get("model_id"))
    backend = _text(binding.get("backend"))
    digest = hashlib.sha1(f"{provider.get('id')}\0{model_id}\0{backend}\0{binding.get('wire_api')}".encode()).hexdigest()[:8]
    base = re.sub(r"[^a-zA-Z0-9_.-]+", "-", f"{provider_name}-{model_id}").strip("-")[:48]
    return f"{base}-{digest}" or f"configured-{digest}"


def sync_legacy_backend_env(config: dict) -> dict:
    providers = {
        _text(item.get("id")): item
        for item in config.get("providers", [])
        if isinstance(item, dict) and _text(item.get("id"))
    }
    bindings = [item for item in config.get("configured_models", []) if isinstance(item, dict)]
    for backend in PROVIDER_BACKENDS:
        section = config.get(backend)
        if not isinstance(section, dict):
            continue
        existing = section.get("env") if isinstance(section.get("env"), list) else []
        generated: list[dict[str, object]] = []
        for binding in bindings:
            if _text(binding.get("backend")) != backend:
                continue
            provider = providers.get(_text(binding.get("provider_id")))
            if not provider:
                continue
            name = _binding_name(provider, binding)
            credential = _text(provider.get("credential"))
            model_id = _text(binding.get("model_id"))
            wire_api = _text(binding.get("wire_api"))
            common: dict[str, object] = {"name": name, "AHA_PROVIDER_ID": provider["id"]}
            if backend == "codex":
                common.update({
                    "OPENAI_BASE_URL": provider["base_url"],
                    "OPENAI_MODEL": model_id,
                    "CODEX_WIRE_API": "responses" if wire_api == "responses" else "chat",
                    "CODEX_ENV_KEY": "OPENAI_API_KEY",
                })
                if credential:
                    common["OPENAI_API_KEY"] = credential
            else:
                anthropic_base = _text(provider.get("anthropic_base_url")) or str(provider.get("base_url") or "")
                common.update({"ANTHROPIC_BASE_URL": anthropic_base, "ANTHROPIC_MODEL": model_id})
                if credential:
                    auth_style = _text(provider.get("auth_style"))
                    use_auth_token = auth_style == "bearer" or (
                        auth_style == "auto" and wire_api != "anthropic_messages"
                    )
                    key = "ANTHROPIC_AUTH_TOKEN" if use_auth_token else "ANTHROPIC_API_KEY"
                    common[key] = credential
                context_window = str(binding.get("context_window") or "").replace("_", "").replace(",", "").strip()
                if context_window.isdigit() and int(context_window) > 0:
                    common["CLAUDE_CODE_MAX_CONTEXT_TOKENS"] = context_window
                _ROLE_MODEL_ENV_KEYS = {
                    "fable_model": "ANTHROPIC_DEFAULT_FABLE_MODEL",
                    "opus_model": "ANTHROPIC_DEFAULT_OPUS_MODEL",
                    "sonnet_model": "ANTHROPIC_DEFAULT_SONNET_MODEL",
                    "haiku_model": "ANTHROPIC_DEFAULT_HAIKU_MODEL",
                }
                for role_key, env_key in _ROLE_MODEL_ENV_KEYS.items():
                    role_model = _text(binding.get(role_key))
                    if role_model:
                        common[env_key] = role_model
            generated.append(common)
        managed_provider_ids = set(providers)
        unmanaged = [
            item
            for item in existing
            if isinstance(item, dict) and _text(item.get("AHA_PROVIDER_ID")) not in managed_provider_ids
        ]
        section["env"] = unmanaged + generated
    return config
