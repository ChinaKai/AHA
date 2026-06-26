from __future__ import annotations

import asyncio
from email.parser import BytesParser
from email.policy import default as email_policy
from email.utils import formatdate
import gzip
import hashlib
from importlib import resources
import json
from pathlib import Path

STATIC_PACKAGE = "aha_cli.web"
GZIP_MIN_BYTES = 1024
STATIC_REVALIDATE_CACHE_CONTROL = "public, max-age=0, must-revalidate"
STATIC_IMMUTABLE_CACHE_CONTROL = "public, max-age=31536000, immutable"
_STATIC_BODY_CACHE: dict[str, tuple[tuple[int | None, int | None], bytes, str, str]] = {}


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


def json_response(
    data: dict,
    status: str = "200 OK",
    request_headers: dict[str, str] | None = None,
    headers: dict[str, str] | None = None,
) -> bytes:
    return http_response(
        status,
        json.dumps(data, ensure_ascii=False).encode("utf-8"),
        "application/json; charset=utf-8",
        headers=headers,
        request_headers=request_headers,
    )


def _header_value(headers: dict[str, str] | None, name: str) -> str:
    target = name.lower()
    for key, value in (headers or {}).items():
        if str(key).lower() == target:
            return str(value)
    return ""


def _static_resource_cache_key(resource: object) -> tuple[int | None, int | None]:
    try:
        stat = resource.stat()  # type: ignore[attr-defined]
    except (AttributeError, FileNotFoundError, OSError):
        return (None, None)
    mtime_ns = getattr(stat, "st_mtime_ns", None)
    if mtime_ns is None:
        mtime_ns = int(float(getattr(stat, "st_mtime", 0.0)) * 1_000_000_000)
    return (int(mtime_ns), int(getattr(stat, "st_size", 0)))


def _static_body(name: str, resource: object) -> tuple[bytes, str, str]:
    cache_key = _static_resource_cache_key(resource)
    cached = _STATIC_BODY_CACHE.get(name)
    if cached and cached[0] == cache_key:
        return cached[1], cached[2], cached[3]
    body = resource.read_bytes()  # type: ignore[attr-defined]
    digest = hashlib.sha256(body).hexdigest()[:16]
    etag = f'"aha-{len(body):x}-{digest}"'
    last_modified = ""
    if cache_key[0] is not None:
        last_modified = formatdate(cache_key[0] / 1_000_000_000, usegmt=True)
    _STATIC_BODY_CACHE[name] = (cache_key, body, etag, last_modified)
    return body, etag, last_modified


def _etag_matches(request_headers: dict[str, str] | None, etag: str) -> bool:
    raw = _header_value(request_headers, "if-none-match")
    if not raw:
        return False
    values = {item.strip() for item in raw.split(",") if item.strip()}
    return "*" in values or etag in values or f"W/{etag}" in values


def _static_cache_control(name: str, versioned: bool = False) -> str:
    if Path(name).suffix == ".html":
        return "no-store"
    return STATIC_IMMUTABLE_CACHE_CONTROL if versioned else STATIC_REVALIDATE_CACHE_CONTROL


def static_response(
    name: str,
    method: str,
    request_headers: dict[str, str] | None = None,
    *,
    versioned: bool = False,
) -> bytes:
    try:
        resource = resources.files(STATIC_PACKAGE).joinpath("static", name)
        if not resource.is_file():
            return http_response("404 Not Found", b"not found\n")
        body, etag, last_modified = _static_body(name, resource)
    except (FileNotFoundError, ModuleNotFoundError, OSError):
        return http_response("404 Not Found", b"not found\n")
    suffix = Path(name).suffix
    content_type = {
        ".html": "text/html; charset=utf-8",
        ".css": "text/css; charset=utf-8",
        ".js": "application/javascript; charset=utf-8",
        ".svg": "image/svg+xml",
    }.get(suffix, "application/octet-stream")
    headers = {"ETag": etag}
    if last_modified:
        headers["Last-Modified"] = last_modified
    cache_control = _static_cache_control(name, versioned=versioned)
    if _etag_matches(request_headers, etag):
        return http_response(
            "304 Not Modified",
            b"",
            content_type,
            headers=headers,
            cache_control=cache_control,
        )
    return http_response(
        "200 OK",
        b"" if method == "HEAD" else body,
        content_type,
        headers=headers,
        request_headers=request_headers,
        cache_control=cache_control,
    )


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
            files[str(name)] = {"filename": filename, "body": payload, "content_type": part.get_content_type()}
    return fields, files
