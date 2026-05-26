from __future__ import annotations

import asyncio
from email.parser import BytesParser
from email.policy import default as email_policy
import gzip
from importlib import resources
import json
from pathlib import Path

STATIC_PACKAGE = "aha_cli.web"
GZIP_MIN_BYTES = 1024


def parse_optional_bool(value: object, field_name: str) -> bool:
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "on"}:
        return True
    if text in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"{field_name} must be a boolean")


def request_accepts_gzip(headers: dict[str, str] | None) -> bool:
    accepted = str((headers or {}).get("accept-encoding") or "").lower()
    return any(item.strip().split(";", 1)[0] == "gzip" for item in accepted.split(","))


def http_response(
    status: str,
    body: bytes,
    content_type: str = "text/plain; charset=utf-8",
    headers: dict[str, str] | None = None,
    request_headers: dict[str, str] | None = None,
    cache_control: str = "no-store",
) -> bytes:
    response_headers = dict(headers or {})
    response_body = body
    if (
        response_body
        and len(response_body) >= GZIP_MIN_BYTES
        and request_accepts_gzip(request_headers)
        and not any(str(key).lower() == "content-encoding" for key in response_headers)
    ):
        response_body = gzip.compress(response_body)
        response_headers["Content-Encoding"] = "gzip"
        response_headers["Vary"] = "Accept-Encoding"
    header_lines = [
        f"HTTP/1.1 {status}",
        f"Content-Type: {content_type}",
        f"Content-Length: {len(response_body)}",
        f"Cache-Control: {cache_control}",
    ]
    for key, value in response_headers.items():
        safe_key = str(key).replace("\r", "").replace("\n", "")
        safe_value = str(value).replace("\r", " ").replace("\n", " ")
        header_lines.append(f"{safe_key}: {safe_value}")
    header_lines.append("Connection: close")
    return ("\r\n".join(header_lines) + "\r\n\r\n").encode("ascii") + response_body


def json_response(data: dict, status: str = "200 OK", request_headers: dict[str, str] | None = None) -> bytes:
    return http_response(
        status,
        json.dumps(data, ensure_ascii=False).encode("utf-8"),
        "application/json; charset=utf-8",
        request_headers=request_headers,
    )


def static_response(name: str, method: str, request_headers: dict[str, str] | None = None) -> bytes:
    try:
        resource = resources.files(STATIC_PACKAGE).joinpath("static", name)
        if not resource.is_file():
            return http_response("404 Not Found", b"not found\n")
        body = resource.read_bytes()
    except (FileNotFoundError, ModuleNotFoundError, OSError):
        return http_response("404 Not Found", b"not found\n")
    suffix = Path(name).suffix
    content_type = {
        ".html": "text/html; charset=utf-8",
        ".css": "text/css; charset=utf-8",
        ".js": "application/javascript; charset=utf-8",
        ".svg": "image/svg+xml",
    }.get(suffix, "application/octet-stream")
    return http_response("200 OK", b"" if method == "HEAD" else body, content_type, request_headers=request_headers)


async def read_http_request(reader: asyncio.StreamReader) -> tuple[str, str, dict[str, str], bytes]:
    raw = await reader.readuntil(b"\r\n\r\n")
    header_text = raw.decode("utf-8", errors="replace")
    lines = header_text.split("\r\n")
    request = lines[0].split()
    if len(request) < 2:
        return "GET", "/", {}, b""
    method, target = request[0], request[1]
    headers: dict[str, str] = {}
    for line in lines[1:]:
        if ":" in line:
            key, value = line.split(":", 1)
            headers[key.strip().lower()] = value.strip()
    length = int(headers.get("content-length", "0") or "0")
    body = await reader.readexactly(length) if length else b""
    return method, target, headers, body


def parse_json_body(body: bytes) -> dict:
    return json.loads(body.decode("utf-8") or "{}")


def parse_query_bool(query: dict[str, list[str]], key: str, default: bool = False) -> bool:
    if key not in query:
        return default
    return parse_optional_bool(query.get(key, [""])[0], key)


def parse_multipart_form(headers: dict[str, str], body: bytes) -> tuple[dict[str, str], dict[str, dict[str, object]]]:
    content_type = headers.get("content-type", "")
    if not content_type.lower().startswith("multipart/form-data"):
        raise ValueError("content-type must be multipart/form-data")
    message = BytesParser(policy=email_policy).parsebytes(
        f"Content-Type: {content_type}\r\nMIME-Version: 1.0\r\n\r\n".encode("utf-8") + body
    )
    if not message.is_multipart():
        raise ValueError("invalid multipart form")
    fields: dict[str, str] = {}
    files: dict[str, dict[str, object]] = {}
    for part in message.iter_parts():
        name = part.get_param("name", header="content-disposition")
        if not name:
            continue
        payload = part.get_payload(decode=True) or b""
        filename = part.get_filename()
        if filename is None:
            fields[str(name)] = payload.decode(part.get_content_charset() or "utf-8", errors="replace")
        else:
            files[str(name)] = {"filename": filename, "body": payload}
    return fields, files
