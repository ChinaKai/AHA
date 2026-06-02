from __future__ import annotations

import hmac
from http.cookies import SimpleCookie
import ipaddress
import json
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from aha_cli.web.http_utils import http_response, json_response

AUTH_COOKIE_NAME = "aha_web_token"
AUTH_QUERY_KEYS = ("token", "aha_token")


def normalize_auth_token(value: object) -> str:
    token = str(value or "").strip()
    if any(char in token for char in "\r\n\0"):
        raise ValueError("auth token must not contain control characters")
    return token


def read_auth_token_file(path: Path) -> str:
    token = normalize_auth_token(path.expanduser().read_text(encoding="utf-8"))
    if not token:
        raise ValueError(f"auth token file is empty: {path}")
    return token


def resolve_auth_token(token: object = None, token_file: object = None) -> str:
    explicit_token = normalize_auth_token(token)
    token_file_text = str(token_file or "").strip()
    if explicit_token and token_file_text:
        raise ValueError("--auth-token and --auth-token-file are mutually exclusive")
    if explicit_token:
        return explicit_token
    if token_file_text:
        return read_auth_token_file(Path(token_file_text))
    return ""


def bind_host_exposes_network(host: str) -> bool:
    value = str(host or "").strip().strip("[]").lower()
    if value in {"", "0.0.0.0", "::"}:
        return True
    if value == "localhost":
        return False
    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        return True
    return not address.is_loopback


def _token_from_query(target: str) -> str:
    query = parse_qs(urlparse(target).query, keep_blank_values=True)
    for key in AUTH_QUERY_KEYS:
        values = query.get(key) or []
        token = normalize_auth_token(values[0] if values else "")
        if token:
            return token
    return ""


def _token_from_cookie(headers: dict[str, str]) -> str:
    raw_cookie = headers.get("cookie", "")
    if not raw_cookie:
        return ""
    cookie = SimpleCookie()
    try:
        cookie.load(raw_cookie)
    except Exception:  # noqa: BLE001
        return ""
    morsel = cookie.get(AUTH_COOKIE_NAME)
    return normalize_auth_token(morsel.value if morsel else "")


def _token_from_headers(headers: dict[str, str]) -> str:
    explicit = normalize_auth_token(headers.get("x-aha-token", ""))
    if explicit:
        return explicit
    authorization = str(headers.get("authorization") or "").strip()
    if authorization.lower().startswith("bearer "):
        return normalize_auth_token(authorization[7:])
    return normalize_auth_token(authorization)


def request_auth_token(target: str, headers: dict[str, str]) -> tuple[str, bool]:
    query_token = _token_from_query(target)
    if query_token:
        return query_token, True
    header_token = _token_from_headers(headers)
    if header_token:
        return header_token, False
    return _token_from_cookie(headers), False


def is_authorized_request(required_token: str, target: str, headers: dict[str, str]) -> tuple[bool, bool]:
    if not required_token:
        return True, False
    provided, came_from_query = request_auth_token(target, headers)
    return hmac.compare_digest(provided, required_token), came_from_query


def optional_authorized_request(required_token: str, target: str, headers: dict[str, str]) -> tuple[bool, bool]:
    if not required_token:
        return True, False
    provided, came_from_query = request_auth_token(target, headers)
    if not provided:
        return True, False
    return hmac.compare_digest(provided, required_token), came_from_query


def auth_cookie_header(token: str) -> dict[str, str]:
    cookie = SimpleCookie()
    cookie[AUTH_COOKIE_NAME] = token
    cookie[AUTH_COOKIE_NAME]["path"] = "/"
    cookie[AUTH_COOKIE_NAME]["httponly"] = True
    cookie[AUTH_COOKIE_NAME]["samesite"] = "Strict"
    return {"Set-Cookie": cookie.output(header="").strip()}


def clear_auth_cookie_header() -> dict[str, str]:
    cookie = SimpleCookie()
    cookie[AUTH_COOKIE_NAME] = ""
    cookie[AUTH_COOKIE_NAME]["path"] = "/"
    cookie[AUTH_COOKIE_NAME]["httponly"] = True
    cookie[AUTH_COOKIE_NAME]["samesite"] = "Strict"
    cookie[AUTH_COOKIE_NAME]["max-age"] = 0
    cookie[AUTH_COOKIE_NAME]["expires"] = "Thu, 01 Jan 1970 00:00:00 GMT"
    return {"Set-Cookie": cookie.output(header="").strip()}


def unauthorized_response(method: str) -> bytes:
    headers = {"WWW-Authenticate": 'Bearer realm="AHA Web UI"'}
    body = b"" if method == "HEAD" else b"authentication required\n"
    return http_response("401 Unauthorized", body, "text/plain; charset=utf-8", headers=headers)


def login_response(required_token: str, method: str, body: bytes) -> bytes:
    if method == "HEAD":
        return http_response("405 Method Not Allowed", b"", headers={"Allow": "POST"})
    if method != "POST":
        return json_response({"error": "method not allowed"}, "405 Method Not Allowed", headers={"Allow": "POST"})
    if not required_token:
        return json_response({"ok": True, "auth_required": False})
    try:
        payload = json.loads(body.decode("utf-8") if body else "{}")
    except json.JSONDecodeError:
        return json_response({"error": "invalid json"}, "400 Bad Request")
    provided = normalize_auth_token(payload.get("token") if isinstance(payload, dict) else "")
    if not hmac.compare_digest(provided, required_token):
        return json_response(
            {"error": "invalid token"},
            "401 Unauthorized",
            headers={"WWW-Authenticate": 'Bearer realm="AHA Web UI"'},
        )
    return json_response({"ok": True, "auth_required": True}, headers=auth_cookie_header(required_token))


def logout_response(method: str) -> bytes:
    if method == "HEAD":
        return http_response("405 Method Not Allowed", b"", headers={"Allow": "POST"})
    if method != "POST":
        return json_response({"error": "method not allowed"}, "405 Method Not Allowed", headers={"Allow": "POST"})
    return json_response({"ok": True}, headers=clear_auth_cookie_header())


def append_response_headers(response: bytes, headers: dict[str, str]) -> bytes:
    if not headers:
        return response
    marker = b"\r\n\r\n"
    if marker not in response:
        return response
    head, body = response.split(marker, 1)
    extra = b"".join(
        f"\r\n{key}: {value}".encode("utf-8")
        for key, value in headers.items()
    )
    return head + extra + marker + body


__all__ = [
    "AUTH_COOKIE_NAME",
    "AUTH_QUERY_KEYS",
    "append_response_headers",
    "auth_cookie_header",
    "bind_host_exposes_network",
    "clear_auth_cookie_header",
    "is_authorized_request",
    "login_response",
    "logout_response",
    "optional_authorized_request",
    "resolve_auth_token",
    "unauthorized_response",
]
