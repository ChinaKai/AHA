from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import threading
from typing import Any


def _json_dumps(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _jsonable(value: object) -> object:
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json", exclude_none=True)
    return value


def _event_type(value: object) -> str:
    event_type = getattr(value, "type", "")
    if hasattr(event_type, "value"):
        return str(event_type.value)
    return str(event_type or "message")


def _chat_compatible_tools(tools: object) -> list[dict]:
    if not isinstance(tools, list):
        return []
    compatible: list[dict] = []
    for tool in tools:
        if not isinstance(tool, dict):
            continue
        if tool.get("type") == "function" and str(tool.get("name") or "").strip():
            compatible.append(dict(tool))
    return compatible


def _tool_choice_for_chat(tool_choice: object, tools: list[dict]) -> object:
    if not tools:
        return "auto"
    if not isinstance(tool_choice, dict):
        return tool_choice
    names = {str(tool.get("name") or "").strip() for tool in tools}
    requested = str(tool_choice.get("name") or "").strip()
    function = tool_choice.get("function")
    if not requested and isinstance(function, dict):
        requested = str(function.get("name") or "").strip()
    return tool_choice if requested in names else "auto"


CODEX_REASONING_LEVELS = [
    {"effort": "low", "description": "Fast responses with lighter reasoning"},
    {"effort": "medium", "description": "Balances speed and reasoning depth for everyday tasks"},
    {"effort": "high", "description": "Greater reasoning depth for complex problems"},
    {"effort": "xhigh", "description": "Extra high reasoning depth for complex problems"},
]


def _responses_kwargs(body: dict, bridge_config: dict, api_key: str) -> dict:
    allowed = (
        "input",
        "instructions",
        "include",
        "max_output_tokens",
        "metadata",
        "parallel_tool_calls",
        "previous_response_id",
        "reasoning",
        "store",
        "temperature",
        "text",
        "tool_choice",
        "top_p",
        "truncation",
        "user",
    )
    kwargs = {key: body.get(key) for key in allowed if key in body}
    tools = _chat_compatible_tools(body.get("tools"))
    if tools:
        kwargs["tools"] = tools
    kwargs["tool_choice"] = _tool_choice_for_chat(body.get("tool_choice", kwargs.get("tool_choice")), tools)
    upstream_model = str(bridge_config.get("upstream_model") or "").strip()
    if upstream_model and not upstream_model.startswith("openai/"):
        upstream_model = f"openai/{upstream_model}"
    kwargs.update(
        {
            "model": upstream_model or "openai/kimi-for-coding",
            "api_key": api_key,
            "api_base": str(bridge_config.get("upstream_base_url") or "").strip(),
            "use_chat_completions_api": True,
            "extra_headers": {"User-Agent": "claude-code/1.0", "X-Client-Name": "claude-code"},
            "timeout": float(bridge_config.get("timeout") or 120),
        }
    )
    return kwargs


class _LitellmBridgeHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    bridge_config: dict = {}
    api_key: str = ""

    def log_message(self, _format: str, *_args: object) -> None:
        return

    def _send_body(self, status: int, body: str | bytes, content_type: str = "application/json") -> None:
        data = body if isinstance(body, bytes) else body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _send_json(self, status: int, body: object) -> None:
        self._send_body(status, _json_dumps(body), "application/json")

    def do_GET(self) -> None:
        if self.path.split("?", 1)[0] != "/v1/models":
            self._send_json(404, {"error": "not found"})
            return
        client_model = str(self.bridge_config.get("client_model") or self.bridge_config.get("upstream_model") or "kimi-for-coding")
        context_window = int(self.bridge_config.get("context_window") or 262144)
        metadata = {
            "slug": client_model,
            "id": client_model,
            "name": client_model,
            "display_name": str(self.bridge_config.get("display_name") or client_model),
            "model": client_model,
            "description": "Kimi Code through AHA's local Responses bridge.",
            "default_reasoning_level": "medium",
            "context_window": context_window,
            "max_context_window": context_window,
            "max_output_tokens": int(self.bridge_config.get("max_output_tokens") or 32768),
            "supported_reasoning_levels": CODEX_REASONING_LEVELS,
            "shell_type": "shell_command",
            "visibility": "list",
            "supported_in_api": True,
            "priority": 1,
            "additional_speed_tiers": [],
            "service_tiers": [],
            "availability_nux": None,
            "upgrade": None,
            "base_instructions": "",
            "model_messages": {},
            "supports_reasoning_summaries": False,
            "default_reasoning_summary": "none",
            "support_verbosity": False,
            "default_verbosity": "low",
            "apply_patch_tool_type": "freeform",
            "web_search_tool_type": "text_and_image",
            "truncation_policy": {"mode": "tokens", "limit": 10000},
            "supports_parallel_tool_calls": True,
            "supports_image_detail_original": False,
            "comp_hash": "aha-kimi",
            "effective_context_window_percent": 95,
            "experimental_supported_tools": [],
            "input_modalities": ["text"],
            "supports_search_tool": False,
            "use_responses_lite": False,
        }
        self._send_json(200, {"object": "list", "models": [metadata], "data": [{"id": client_model, "object": "model"}]})

    def do_POST(self) -> None:
        if self.path.split("?", 1)[0] != "/v1/responses":
            self._send_json(404, {"error": "not found"})
            return
        length = int(self.headers.get("content-length") or 0)
        try:
            body = json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError as exc:
            self._send_json(400, {"error": f"invalid JSON: {exc}"})
            return
        if not isinstance(body, dict):
            self._send_json(400, {"error": "request body must be an object"})
            return
        if not self.api_key:
            self._send_json(401, {"error": "missing upstream API key"})
            return
        stream = bool(body.get("stream", True))
        if stream:
            self._send_stream(body)
        else:
            self._send_non_stream(body)

    def _send_stream(self, body: dict) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.end_headers()
        try:
            import litellm

            for event in litellm.responses(stream=True, **_responses_kwargs(body, self.bridge_config, self.api_key)):
                event_type = _event_type(event)
                data = _json_dumps(_jsonable(event))
                self.wfile.write(f"event: {event_type}\ndata: {data}\n\n".encode("utf-8"))
                self.wfile.flush()
        except Exception as exc:  # pragma: no cover - exercised by integration failures.
            error = {"type": "error", "message": str(exc)}
            self.wfile.write(f"event: error\ndata: {_json_dumps(error)}\n\n".encode("utf-8"))
            self.wfile.flush()

    def _send_non_stream(self, body: dict) -> None:
        try:
            import litellm

            response = litellm.responses(stream=False, **_responses_kwargs(body, self.bridge_config, self.api_key))
        except Exception as exc:  # pragma: no cover - exercised by integration failures.
            self._send_json(500, {"error": str(exc)})
            return
        self._send_json(200, _jsonable(response))


class LitellmResponsesBridge:
    def __init__(self, *, bridge_config: dict, api_key: str):
        self.bridge_config = dict(bridge_config)
        self.api_key = str(api_key or "")
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None
        self.base_url = ""

    def __enter__(self) -> "LitellmResponsesBridge":
        config = self.bridge_config
        api_key = self.api_key

        Handler = type(
            "LitellmBridgeHandler",
            (_LitellmBridgeHandler,),
            {"bridge_config": config, "api_key": api_key},
        )

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        host, port = server.server_address[:2]
        self.base_url = f"http://{host}:{port}/v1"
        self._server = server
        self._thread = threading.Thread(target=server.serve_forever, name="aha-codex-litellm-bridge", daemon=True)
        self._thread.start()
        return self

    def __exit__(self, _exc_type: object, _exc: object, _tb: object) -> bool:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
        if self._thread is not None:
            self._thread.join(timeout=2)
        return False


def start_litellm_responses_bridge(*, bridge_config: dict, api_key: str) -> LitellmResponsesBridge:
    return LitellmResponsesBridge(bridge_config=bridge_config, api_key=api_key)


__all__ = ["LitellmResponsesBridge", "start_litellm_responses_bridge"]
