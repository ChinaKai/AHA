from __future__ import annotations

import base64
import datetime as dt
import json
from pathlib import Path
import secrets
import threading
import time
from urllib.parse import quote, urljoin
import urllib.request

from aha_cli.domain.models import utc_now
from aha_cli.store.paths import aha_home_path

DEFAULT_BASE_URL = "https://ilinkai.weixin.qq.com"
CHANNEL_VERSION = "2.1.7"
ILINK_APP_ID = "bot"
ILINK_BOT_TYPE = "3"
LOGIN_TTL_SECONDS = 8 * 60
API_TIMEOUT_SECONDS = 15
QR_POLL_TIMEOUT_SECONDS = 6
CONTEXT_TOKEN_STALE_SECONDS = 30 * 60

MESSAGE_TYPE_BOT = 2
MESSAGE_STATE_FINISH = 2
MESSAGE_ITEM_TEXT = 1
RECENT_RECEIVED_MESSAGE_LIMIT = 3
RECENT_RECEIVED_TEXT_LIMIT = 240

_updates_lock = threading.Lock()


class WeixinError(RuntimeError):
    pass


def weixin_dir(root: Path) -> Path:
    return aha_home_path(root) / "weixin"


def pairing_path(root: Path) -> Path:
    return weixin_dir(root) / "pairing.json"


def account_path(root: Path) -> Path:
    return weixin_dir(root) / "account.json"


def updates_path(root: Path) -> Path:
    return weixin_dir(root) / "updates.json"


def contexts_path(root: Path) -> Path:
    return weixin_dir(root) / "contexts.json"


def context_state_path(root: Path) -> Path:
    return weixin_dir(root) / "context_state.json"


def _read_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}


def _write_secret_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    try:
        path.chmod(0o600)
    except OSError:
        pass


def _build_client_version(version: str) -> int:
    parts = [(int(part) if part.isdigit() else 0) for part in version.split(".")]
    major, minor, patch = (parts + [0, 0, 0])[:3]
    return ((major & 0xFF) << 16) | ((minor & 0xFF) << 8) | (patch & 0xFF)


def _random_wechat_uin() -> str:
    return base64.b64encode(str(secrets.randbits(32)).encode("utf-8")).decode("ascii")


def _common_headers() -> dict[str, str]:
    return {
        "iLink-App-Id": ILINK_APP_ID,
        "iLink-App-ClientVersion": str(_build_client_version(CHANNEL_VERSION)),
    }


def _api_url(base_url: str, endpoint: str) -> str:
    return urljoin(base_url.rstrip("/") + "/", endpoint)


def _request_json(url: str, *, method: str = "GET", token: str | None = None, body: dict | None = None, timeout: int = API_TIMEOUT_SECONDS) -> dict:
    raw_body = b""
    headers = _common_headers()
    if body is not None:
        raw_payload = json.dumps({**body, "base_info": {"channel_version": CHANNEL_VERSION}}, ensure_ascii=False)
        raw_body = raw_payload.encode("utf-8")
        headers.update(
            {
                "Content-Type": "application/json",
                "AuthorizationType": "ilink_bot_token",
                "Content-Length": str(len(raw_body)),
                "X-WECHAT-UIN": _random_wechat_uin(),
            }
        )
        if token:
            headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(url, data=raw_body if body is not None else None, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            text = response.read().decode("utf-8")
    except Exception as exc:  # pragma: no cover - exact urllib exceptions vary by platform.
        raise WeixinError(f"微信接口请求失败: {exc}") from exc
    try:
        return json.loads(text or "{}")
    except json.JSONDecodeError as exc:
        raise WeixinError(f"微信接口返回非 JSON: {text[:200]}") from exc


def _api_get_json(base_url: str, endpoint: str, *, timeout: int = API_TIMEOUT_SECONDS) -> dict:
    return _request_json(_api_url(base_url, endpoint), timeout=timeout)


def _api_post_json(base_url: str, endpoint: str, token: str, body: dict, *, timeout: int = API_TIMEOUT_SECONDS) -> dict:
    return _request_json(_api_url(base_url, endpoint), method="POST", token=token, body=body, timeout=timeout)


def _raise_for_api_error(resp: dict, label: str) -> None:
    ret = resp.get("ret")
    errcode = resp.get("errcode")
    if ret not in (None, 0) or errcode not in (None, 0):
        errmsg = str(resp.get("errmsg") or resp.get("message") or "").strip()
        raise WeixinError(f"{label}失败: ret={ret} errcode={errcode} {errmsg}".rstrip())


QR_L_PARAMS = {
    1: ([19], 7),
    2: ([34], 10),
    3: ([55], 15),
    4: ([80], 20),
    5: ([108], 26),
    6: ([68, 68], 18),
    7: ([78, 78], 20),
    8: ([97, 97], 24),
    9: ([116, 116], 30),
    10: ([68, 68, 69, 69], 18),
}

QR_ALIGNMENT_POSITIONS = {
    1: [],
    2: [6, 18],
    3: [6, 22],
    4: [6, 26],
    5: [6, 30],
    6: [6, 34],
    7: [6, 22, 38],
    8: [6, 24, 42],
    9: [6, 26, 46],
    10: [6, 28, 50],
}


def _qr_version_for_payload(payload: bytes) -> int:
    for version, (blocks, _ecc_len) in QR_L_PARAMS.items():
        count_bits = 8 if version <= 9 else 16
        if 4 + count_bits + len(payload) * 8 <= sum(blocks) * 8:
            return version
    raise WeixinError("微信登录二维码内容过长，当前内置二维码编码器无法承载")


def _append_bits(bits: list[int], value: int, length: int) -> None:
    for i in reversed(range(length)):
        bits.append((value >> i) & 1)


def _encode_qr_data(payload: str, version: int) -> list[int]:
    data = payload.encode("utf-8")
    blocks, _ecc_len = QR_L_PARAMS[version]
    data_codewords = sum(blocks)
    bits: list[int] = []
    _append_bits(bits, 0b0100, 4)
    _append_bits(bits, len(data), 8 if version <= 9 else 16)
    for byte in data:
        _append_bits(bits, byte, 8)
    _append_bits(bits, 0, min(4, data_codewords * 8 - len(bits)))
    while len(bits) % 8:
        bits.append(0)
    codewords = [
        int("".join(str(bit) for bit in bits[index:index + 8]), 2)
        for index in range(0, len(bits), 8)
    ]
    pad = 0xEC
    while len(codewords) < data_codewords:
        codewords.append(pad)
        pad = 0x11 if pad == 0xEC else 0xEC
    return codewords


def _gf_mul(x: int, y: int) -> int:
    result = 0
    while y:
        if y & 1:
            result ^= x
        x <<= 1
        if x & 0x100:
            x ^= 0x11D
        y >>= 1
    return result


def _gf_pow(x: int, power: int) -> int:
    result = 1
    for _ in range(power):
        result = _gf_mul(result, x)
    return result


def _rs_generator(degree: int) -> list[int]:
    coefficients = [1]
    for i in range(degree):
        next_coefficients = [0] * (len(coefficients) + 1)
        factor = _gf_pow(2, i)
        for index, coefficient in enumerate(coefficients):
            next_coefficients[index] ^= coefficient
            next_coefficients[index + 1] ^= _gf_mul(coefficient, factor)
        coefficients = next_coefficients
    return coefficients[1:]


def _rs_remainder(data: list[int], degree: int) -> list[int]:
    generator = _rs_generator(degree)
    result = [0] * degree
    for byte in data:
        factor = byte ^ result.pop(0)
        result.append(0)
        for index, coefficient in enumerate(generator):
            result[index] ^= _gf_mul(coefficient, factor)
    return result


def _interleave_codewords(data: list[int], version: int) -> list[int]:
    block_lengths, ecc_len = QR_L_PARAMS[version]
    blocks: list[tuple[list[int], list[int]]] = []
    offset = 0
    for length in block_lengths:
        block_data = data[offset:offset + length]
        offset += length
        blocks.append((block_data, _rs_remainder(block_data, ecc_len)))
    result: list[int] = []
    max_data = max(len(block[0]) for block in blocks)
    for index in range(max_data):
        for block_data, _ecc in blocks:
            if index < len(block_data):
                result.append(block_data[index])
    for index in range(ecc_len):
        for _block_data, ecc in blocks:
            result.append(ecc[index])
    return result


def _bch_remainder(value: int, polynomial: int) -> int:
    shift = polynomial.bit_length() - 1
    value <<= shift
    while value.bit_length() >= polynomial.bit_length():
        value ^= polynomial << (value.bit_length() - polynomial.bit_length())
    return value


def _qr_svg(payload: str) -> str:
    version = _qr_version_for_payload(payload.encode("utf-8"))
    size = version * 4 + 17
    modules = [[False] * size for _ in range(size)]
    reserved = [[False] * size for _ in range(size)]

    def set_module(row: int, col: int, value: bool, reserve: bool = True) -> None:
        if 0 <= row < size and 0 <= col < size:
            modules[row][col] = value
            if reserve:
                reserved[row][col] = True

    def draw_finder(center_row: int, center_col: int) -> None:
        for row in range(center_row - 4, center_row + 5):
            for col in range(center_col - 4, center_col + 5):
                dist = max(abs(row - center_row), abs(col - center_col))
                set_module(row, col, dist in {0, 1, 3})

    def draw_alignment(center_row: int, center_col: int) -> None:
        for row in range(center_row - 2, center_row + 3):
            for col in range(center_col - 2, center_col + 3):
                dist = max(abs(row - center_row), abs(col - center_col))
                set_module(row, col, dist in {0, 2})

    draw_finder(3, 3)
    draw_finder(3, size - 4)
    draw_finder(size - 4, 3)
    for i in range(8, size - 8):
        value = i % 2 == 0
        set_module(6, i, value)
        set_module(i, 6, value)
    for row in QR_ALIGNMENT_POSITIONS[version]:
        for col in QR_ALIGNMENT_POSITIONS[version]:
            if reserved[row][col]:
                continue
            draw_alignment(row, col)
    set_module(4 * version + 9, 8, True)
    for i in range(6):
        set_module(i, 8, False)
        set_module(8, i, False)
    set_module(7, 8, False)
    set_module(8, 7, False)
    set_module(8, 8, False)
    for i in range(8):
        set_module(8, size - 1 - i, False)
    for i in range(8, 15):
        set_module(size - 15 + i, 8, False)
    if version >= 7:
        for i in range(18):
            row = i // 3
            col = size - 11 + i % 3
            set_module(row, col, False)
            set_module(col, row, False)

    data = _interleave_codewords(_encode_qr_data(payload, version), version)
    data_bits = [(byte >> i) & 1 for byte in data for i in reversed(range(8))]
    bit_index = 0
    upward = True
    col = size - 1
    while col > 0:
        if col == 6:
            col -= 1
        row_range = range(size - 1, -1, -1) if upward else range(size)
        for row in row_range:
            for current_col in (col, col - 1):
                if reserved[row][current_col]:
                    continue
                bit = data_bits[bit_index] if bit_index < len(data_bits) else 0
                bit_index += 1
                mask = (row + current_col) % 2 == 0
                modules[row][current_col] = bool(bit) ^ mask
        upward = not upward
        col -= 2

    format_data = (1 << 3) | 0
    format_bits = ((format_data << 10) | _bch_remainder(format_data, 0x537)) ^ 0x5412
    for i in range(6):
        set_module(i, 8, bool((format_bits >> i) & 1))
    set_module(7, 8, bool((format_bits >> 6) & 1))
    set_module(8, 8, bool((format_bits >> 7) & 1))
    set_module(8, 7, bool((format_bits >> 8) & 1))
    for i in range(9, 15):
        set_module(8, 14 - i, bool((format_bits >> i) & 1))
    for i in range(8):
        set_module(8, size - 1 - i, bool((format_bits >> i) & 1))
    for i in range(8, 15):
        set_module(size - 15 + i, 8, bool((format_bits >> i) & 1))
    set_module(4 * version + 9, 8, True)

    if version >= 7:
        version_bits = (version << 12) | _bch_remainder(version, 0x1F25)
        for i in range(18):
            bit = bool((version_bits >> i) & 1)
            row = i // 3
            col = size - 11 + i % 3
            set_module(row, col, bit)
            set_module(col, row, bit)

    rects = []
    border = 4
    for row, values in enumerate(modules):
        start = None
        for col, value in enumerate([*values, False]):
            if value and start is None:
                start = col
            elif not value and start is not None:
                rects.append(f'<rect x="{start + border}" y="{row + border}" width="{col - start}" height="1"/>')
                start = None
    svg_size = size + border * 2
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {svg_size} {svg_size}" '
        f'shape-rendering="crispEdges"><rect width="100%" height="100%" fill="#fff"/>'
        f'<g fill="#000">{"".join(rects)}</g></svg>'
    )


def _public_account(account: dict) -> dict:
    return {
        "account_id": account.get("account_id") or account.get("accountId") or "",
        "user_id": account.get("user_id") or account.get("userId") or "",
        "base_url": account.get("base_url") or account.get("baseUrl") or DEFAULT_BASE_URL,
        "saved_at": account.get("saved_at") or account.get("savedAt") or "",
    }


def load_account(root: Path) -> dict:
    return _read_json(account_path(root))


def save_account(root: Path, account: dict) -> None:
    normalized = {
        "account_id": account.get("account_id") or account.get("accountId"),
        "token": account.get("token"),
        "base_url": account.get("base_url") or account.get("baseUrl") or DEFAULT_BASE_URL,
        "user_id": account.get("user_id") or account.get("userId"),
        "saved_at": utc_now(),
    }
    if not normalized["account_id"] or not normalized["token"]:
        raise WeixinError("微信配对成功但缺少账号或 token")
    _write_secret_json(account_path(root), normalized)


def notify_channel_start(root: Path) -> dict:
    account = load_account(root)
    token = str(account.get("token") or "")
    if not token:
        raise WeixinError("微信尚未配对，不能通知通道启动")
    resp = _api_post_json(
        str(account.get("base_url") or DEFAULT_BASE_URL),
        "ilink/bot/msg/notifystart",
        token,
        {},
    )
    _raise_for_api_error(resp, "微信通道启动通知")
    return {"ok": True}


def notify_channel_stop(root: Path) -> dict:
    account = load_account(root)
    token = str(account.get("token") or "")
    if not token:
        raise WeixinError("微信尚未配对，不能通知通道停止")
    resp = _api_post_json(
        str(account.get("base_url") or DEFAULT_BASE_URL),
        "ilink/bot/msg/notifystop",
        token,
        {},
    )
    _raise_for_api_error(resp, "微信通道停止通知")
    return {"ok": True}


def reset_pairing(root: Path, run_id: str) -> dict:
    for path in (pairing_path(root), account_path(root), updates_path(root), contexts_path(root), context_state_path(root)):
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        except OSError as exc:
            raise WeixinError(f"微信配对重置失败: {exc}") from exc
    return status_snapshot(root, run_id, poll=False)


def load_context_token(root: Path, user_id: str) -> str:
    contexts = _read_json(contexts_path(root))
    token = contexts.get(user_id)
    return token if isinstance(token, str) else ""


def _parse_utc_timestamp(value: object) -> dt.datetime | None:
    if not value:
        return None
    try:
        parsed = dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return parsed.astimezone(dt.timezone.utc)


def _file_updated_at(path: Path) -> str:
    try:
        return dt.datetime.fromtimestamp(path.stat().st_mtime, dt.timezone.utc).isoformat()
    except OSError:
        return ""


def _context_updated_at(root: Path, user_id: str) -> str:
    state = _read_json(context_state_path(root))
    entry = state.get(user_id) if isinstance(state.get(user_id), dict) else {}
    updated_at = str(entry.get("updated_at") or "")
    if updated_at:
        return updated_at
    return _file_updated_at(contexts_path(root)) if load_context_token(root, user_id) else ""


def send_context_snapshot(root: Path, user_id: str | None = None) -> dict:
    account = load_account(root)
    target = str(user_id or account.get("user_id") or "")
    if not target:
        return {
            "state": "not_paired",
            "has_context_token": False,
            "fresh": False,
            "updated_at": "",
            "age_seconds": None,
            "stale_after_seconds": CONTEXT_TOKEN_STALE_SECONDS,
            "requires_user_message": True,
        }
    token = load_context_token(root, target)
    if not token:
        return {
            "state": "missing",
            "has_context_token": False,
            "fresh": False,
            "updated_at": "",
            "age_seconds": None,
            "stale_after_seconds": CONTEXT_TOKEN_STALE_SECONDS,
            "requires_user_message": True,
        }
    updated_at = _context_updated_at(root, target)
    updated = _parse_utc_timestamp(updated_at)
    now = _parse_utc_timestamp(utc_now()) or dt.datetime.now(dt.timezone.utc)
    age_seconds = int(max(0, (now - updated).total_seconds())) if updated else None
    if age_seconds is None:
        state = "unknown"
        fresh = False
    else:
        fresh = age_seconds <= CONTEXT_TOKEN_STALE_SECONDS
        state = "fresh" if fresh else "stale"
    return {
        "state": state,
        "has_context_token": True,
        "fresh": fresh,
        "updated_at": updated_at,
        "age_seconds": age_seconds,
        "stale_after_seconds": CONTEXT_TOKEN_STALE_SECONDS,
        "requires_user_message": state in {"missing", "stale"},
    }


def _message_text(msg: dict) -> str:
    texts: list[str] = []
    item_list = msg.get("item_list") if isinstance(msg.get("item_list"), list) else []
    for item in item_list:
        if not isinstance(item, dict):
            continue
        text_item = item.get("text_item") if isinstance(item.get("text_item"), dict) else {}
        text = str(text_item.get("text") or item.get("text") or "").strip()
        if text:
            texts.append(text)
    fallback = str(msg.get("text") or msg.get("content") or "").strip()
    text = "\n".join(texts) or fallback
    return _truncate_text(text, RECENT_RECEIVED_TEXT_LIMIT)


def _truncate_text(text: str, limit: int) -> str:
    stripped = str(text or "").strip()
    return stripped if len(stripped) <= limit else stripped[: limit - 1].rstrip() + "…"


def _public_received_message(msg: dict, received_at: str) -> dict:
    raw_timestamp = str(msg.get("create_time") or msg.get("createTime") or msg.get("timestamp") or msg.get("time") or "")
    return {
        "from_user_id": str(msg.get("from_user_id") or msg.get("fromUserId") or ""),
        "message_id": str(msg.get("msg_id") or msg.get("message_id") or msg.get("client_id") or ""),
        "raw_timestamp": raw_timestamp,
        "received_at": received_at,
        "text": _message_text(msg),
    }


def _received_message_key(message: dict) -> str:
    message_id = str(message.get("message_id") or "").strip()
    if message_id:
        return f"id:{message_id}"
    return "body:{from_user_id}:{raw_timestamp}:{text}".format(
        from_user_id=message.get("from_user_id") or "",
        raw_timestamp=message.get("raw_timestamp") or "",
        text=message.get("text") or "",
    )


def _merge_recent_received_messages(existing: object, incoming: list[dict]) -> list[dict]:
    existing_messages = [item for item in existing if isinstance(item, dict)] if isinstance(existing, list) else []
    combined = [*existing_messages, *incoming]
    deduped_reversed: list[dict] = []
    seen: set[str] = set()
    for message in reversed(combined):
        key = _received_message_key(message)
        if key in seen:
            continue
        seen.add(key)
        deduped_reversed.append(message)
    return list(reversed(deduped_reversed))[-RECENT_RECEIVED_MESSAGE_LIMIT:]


def recent_received_messages(root: Path) -> list[dict]:
    sync = _read_json(updates_path(root))
    recent = sync.get("recent_messages") if isinstance(sync.get("recent_messages"), list) else []
    return list(reversed(recent[-RECENT_RECEIVED_MESSAGE_LIMIT:]))


def fetch_updates(root: Path) -> dict:
    with _updates_lock:
        account = load_account(root)
        token = str(account.get("token") or "")
        if not token:
            raise WeixinError("微信尚未配对，不能接收入站消息")
        sync = _read_json(updates_path(root))
        try:
            resp = _api_post_json(
                str(account.get("base_url") or DEFAULT_BASE_URL),
                "ilink/bot/getupdates",
                token,
                {"get_updates_buf": sync.get("get_updates_buf") or ""},
                timeout=QR_POLL_TIMEOUT_SECONDS,
            )
        except WeixinError as exc:
            if "timed out" not in str(exc).lower():
                raise
            resp = {"msgs": [], "get_updates_buf": sync.get("get_updates_buf") or ""}
        now = utc_now()
        contexts = _read_json(contexts_path(root))
        context_state = _read_json(context_state_path(root))
        changed = False
        context_state_changed = False
        messages = resp.get("msgs") if isinstance(resp.get("msgs"), list) else []
        incoming_recent = [_public_received_message(msg, now) for msg in messages if isinstance(msg, dict)]
        recent_messages = _merge_recent_received_messages(sync.get("recent_messages"), incoming_recent)
        _write_secret_json(
            updates_path(root),
            {
                "get_updates_buf": resp.get("get_updates_buf") or sync.get("get_updates_buf") or "",
                "recent_messages": recent_messages,
                "last_poll_at": now,
                "last_success_at": now,
                "updated_at": now,
            },
        )
        for msg in messages:
            if not isinstance(msg, dict):
                continue
            user_id = str(msg.get("from_user_id") or "")
            context_token = str(msg.get("context_token") or "")
            if user_id and context_token:
                if contexts.get(user_id) != context_token:
                    contexts[user_id] = context_token
                    changed = True
                entry = context_state.get(user_id) if isinstance(context_state.get(user_id), dict) else {}
                if entry.get("updated_at") != now:
                    context_state[user_id] = {"updated_at": now}
                    context_state_changed = True
        if changed:
            _write_secret_json(contexts_path(root), contexts)
        if context_state_changed:
            _write_secret_json(context_state_path(root), context_state)
        return {
            "ok": True,
            "messages": messages,
            "message_count": len(messages),
            "recent_messages": list(reversed(recent_messages)),
            "account": _public_account(account),
        }


def start_pairing(root: Path, run_id: str) -> dict:
    qr = _api_get_json(
        DEFAULT_BASE_URL,
        f"ilink/bot/get_bot_qrcode?bot_type={quote(ILINK_BOT_TYPE)}",
    )
    qrcode_value = str(qr.get("qrcode") or "")
    qrcode_payload = str(qr.get("qrcode_img_content") or "")
    if not qrcode_value or not qrcode_payload:
        raise WeixinError("微信二维码接口返回缺少 qrcode 字段")
    now = time.time()
    pairing = {
        "run_id": run_id,
        "status": "waiting",
        "raw_status": "wait",
        "qrcode": qrcode_value,
        "qrcode_payload": qrcode_payload,
        "qrcode_svg": _qr_svg(qrcode_payload),
        "base_url": DEFAULT_BASE_URL,
        "started_at": utc_now(),
        "updated_at": utc_now(),
        "expires_epoch": now + LOGIN_TTL_SECONDS,
    }
    _write_secret_json(pairing_path(root), pairing)
    return status_snapshot(root, run_id, poll=False)


def _poll_pairing(root: Path, pairing: dict) -> dict:
    if not pairing.get("qrcode"):
        return pairing
    if float(pairing.get("expires_epoch") or 0) < time.time():
        pairing = {**pairing, "status": "expired", "raw_status": "expired", "updated_at": utc_now()}
        _write_secret_json(pairing_path(root), pairing)
        return pairing
    base_url = str(pairing.get("base_url") or DEFAULT_BASE_URL)
    try:
        status = _api_get_json(
            base_url,
            f"ilink/bot/get_qrcode_status?qrcode={quote(str(pairing.get('qrcode') or ''))}",
            timeout=QR_POLL_TIMEOUT_SECONDS,
        )
    except WeixinError as exc:
        if "timed out" not in str(exc).lower():
            raise
        next_pairing = {**pairing, "status": "waiting", "raw_status": "wait", "updated_at": utc_now()}
        _write_secret_json(pairing_path(root), next_pairing)
        return next_pairing
    raw_status = str(status.get("status") or "wait")
    next_pairing = {**pairing, "raw_status": raw_status, "updated_at": utc_now()}
    if raw_status == "scaned":
        next_pairing["status"] = "scanned"
    elif raw_status == "scaned_but_redirect":
        next_pairing["status"] = "scanned"
        if status.get("redirect_host"):
            next_pairing["base_url"] = f"https://{status['redirect_host']}"
    elif raw_status == "expired":
        next_pairing["status"] = "expired"
    elif raw_status == "confirmed":
        save_account(
            root,
            {
                "account_id": status.get("ilink_bot_id"),
                "token": status.get("bot_token"),
                "base_url": status.get("baseurl") or next_pairing.get("base_url") or DEFAULT_BASE_URL,
                "user_id": status.get("ilink_user_id"),
            },
        )
        next_pairing["status"] = "paired"
        next_pairing["account_id"] = status.get("ilink_bot_id") or ""
        next_pairing["user_id"] = status.get("ilink_user_id") or ""
    else:
        next_pairing["status"] = "waiting"
    _write_secret_json(pairing_path(root), next_pairing)
    return next_pairing


def status_snapshot(root: Path, run_id: str, *, poll: bool = True) -> dict:
    account = load_account(root)
    pairing = _read_json(pairing_path(root))
    pairing_error = ""
    if pairing.get("qrcode_payload"):
        try:
            qrcode_svg = _qr_svg(str(pairing.get("qrcode_payload") or ""))
            if pairing.get("qrcode_svg") != qrcode_svg:
                pairing = {**pairing, "qrcode_svg": qrcode_svg, "updated_at": utc_now()}
                _write_secret_json(pairing_path(root), pairing)
        except WeixinError as exc:
            pairing_error = str(exc)
    if poll and pairing.get("status") in {"waiting", "scanned"}:
        try:
            pairing = _poll_pairing(root, pairing)
        except WeixinError as exc:
            pairing_error = str(exc)
    paired = bool(account.get("token")) or pairing.get("status") == "paired"
    if pairing.get("status") == "paired":
        account = load_account(root)
    target = str(account.get("user_id") or "") if account else ""
    return {
        "ok": True,
        "run_id": run_id,
        "paired": paired,
        "account": _public_account(account) if account else None,
        "send_context": send_context_snapshot(root, target) if target else send_context_snapshot(root, ""),
        "pairing": {
            key: pairing.get(key)
            for key in (
                "status",
                "raw_status",
                "qrcode_payload",
                "qrcode_svg",
                "started_at",
                "updated_at",
                "expires_epoch",
                "account_id",
                "user_id",
            )
            if pairing.get(key) is not None
        } if pairing else None,
        "error": pairing_error,
        "received_messages": recent_received_messages(root),
    }


def send_test_notification(root: Path, run_id: str, message: str | None = None) -> dict:
    account = load_account(root)
    token = str(account.get("token") or "")
    target = str(account.get("user_id") or "")
    if not token or not target:
        raise WeixinError("微信尚未配对，不能发送测试通知")
    fetch_updates(root)
    send_context = send_context_snapshot(root, target)
    context_token = load_context_token(root, target)
    if not context_token:
        raise WeixinError("微信会话缺少 context_token，请先从微信向 AHA 发一条消息刷新会话")
    if send_context.get("state") == "stale":
        stale_minutes = int(CONTEXT_TOKEN_STALE_SECONDS / 60)
        raise WeixinError(f"微信会话已超过 {stale_minutes} 分钟未刷新，请先从微信向 AHA 发一条消息")
    text = str(message or "").strip() or f"AHA 微信通知测试\nRun: {run_id}"
    client_id = f"aha:{int(time.time())}-{secrets.token_hex(4)}"
    msg = {
        "from_user_id": "",
        "to_user_id": target,
        "client_id": client_id,
        "message_type": MESSAGE_TYPE_BOT,
        "message_state": MESSAGE_STATE_FINISH,
        "item_list": [{"type": MESSAGE_ITEM_TEXT, "text_item": {"text": text}}],
    }
    if context_token:
        msg["context_token"] = context_token
    resp = _api_post_json(
        str(account.get("base_url") or DEFAULT_BASE_URL),
        "ilink/bot/sendmessage",
        token,
        {"msg": msg},
    )
    _raise_for_api_error(resp, "微信发送")
    return {
        "ok": True,
        "sent": True,
        "message_id": client_id,
        "target": target,
        "send_context": send_context,
        "account": _public_account(account),
    }
