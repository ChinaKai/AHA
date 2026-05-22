from __future__ import annotations

import json
from pathlib import Path

from aha_cli.domain.models import utc_now
from aha_cli.services.prompt_templates import render_prompt_template
from aha_cli.store.filesystem import run_dir

AHA_BACKEND_PROMPT_MARKER = render_prompt_template("backend_prompt_prefix.md").strip()


def session_jsonl_text(value) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "\n".join(part for item in value if (part := session_jsonl_text(item)))
    if isinstance(value, dict):
        return "\n".join(part for item in value.values() if (part := session_jsonl_text(item)))
    return ""


def classify_aha_session_prompt(text: str) -> str:
    if AHA_BACKEND_PROMPT_MARKER not in text or "User message from" not in text:
        return ""
    if "Current delta status:" in text:
        return "sticky_delta"
    if "Current status:" in text:
        return "full"
    return "unknown"


def bump_count(counts: dict, key: str, amount: int = 1) -> None:
    counts[key] = int(counts.get(key) or 0) + amount


def message_content_text(message: dict) -> str:
    if not isinstance(message, dict):
        return ""
    return session_jsonl_text(message.get("content"))


def message_has_content_type(message: dict, content_type: str) -> bool:
    content = message.get("content") if isinstance(message, dict) else None
    return isinstance(content, list) and any(isinstance(item, dict) and item.get("type") == content_type for item in content)


def analyze_backend_session_jsonl(path: Path, backend: str = "") -> dict:
    type_counts: dict[str, int] = {}
    response_item_counts: dict[str, int] = {}
    response_item_chars: dict[str, int] = {}
    aha_prompt_counts: dict[str, int] = {}
    aha_prompt_chars: dict[str, int] = {}
    mirror_prompt_counts: dict[str, int] = {}
    mirror_prompt_chars: dict[str, int] = {}
    latest_aha_prompts: list[dict] = []
    line_count = 0
    parse_errors = 0
    total_payload_text_chars = 0

    with path.open("r", encoding="utf-8", errors="replace") as handle:
        for line_count, line in enumerate(handle, 1):
            if not line.strip():
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                parse_errors += 1
                continue
            record_type = str(record.get("type") or "unknown")
            bump_count(type_counts, record_type)
            payload = record.get("payload") if isinstance(record.get("payload"), dict) else {}
            if backend == "claude":
                text = session_jsonl_text(record.get("message") or record.get("content") or record.get("lastPrompt") or record.get("attachment") or "")
            else:
                text = session_jsonl_text(payload)
            text_chars = len(text)
            total_payload_text_chars += text_chars

            if record_type == "response_item":
                payload_type = str(payload.get("type") or "-")
                role = str(payload.get("role") or "-")
                key = f"{payload_type}:{role}"
                bump_count(response_item_counts, key)
                bump_count(response_item_chars, key, text_chars)
                if payload_type == "message" and role == "user":
                    prompt_mode = classify_aha_session_prompt(text)
                    if prompt_mode:
                        bump_count(aha_prompt_counts, prompt_mode)
                        bump_count(aha_prompt_chars, prompt_mode, text_chars)
                        latest_aha_prompts.append({"line": line_count, "mode": prompt_mode, "chars": text_chars})
                        latest_aha_prompts = latest_aha_prompts[-6:]
            elif record_type == "event_msg":
                prompt_mode = classify_aha_session_prompt(text)
                if prompt_mode:
                    bump_count(mirror_prompt_counts, prompt_mode)
                    bump_count(mirror_prompt_chars, prompt_mode, text_chars)
            elif backend == "claude" and record_type == "queue-operation":
                content = str(record.get("content") or "")
                prompt_mode = classify_aha_session_prompt(content)
                if prompt_mode:
                    bump_count(mirror_prompt_counts, prompt_mode)
                    bump_count(mirror_prompt_chars, prompt_mode, len(content))
            elif backend == "claude" and record_type in {"user", "assistant"}:
                message = record.get("message") if isinstance(record.get("message"), dict) else {}
                content_text = message_content_text(message)
                content_chars = len(content_text)
                role = str(message.get("role") or record_type)
                key = "tool_result:user" if record_type == "user" and message_has_content_type(message, "tool_result") else f"message:{role}"
                bump_count(response_item_counts, key)
                bump_count(response_item_chars, key, content_chars)
                if record_type == "user":
                    prompt_mode = classify_aha_session_prompt(content_text)
                    if prompt_mode:
                        bump_count(aha_prompt_counts, prompt_mode)
                        bump_count(aha_prompt_chars, prompt_mode, content_chars)
                        latest_aha_prompts.append({"line": line_count, "mode": prompt_mode, "chars": content_chars})
                        latest_aha_prompts = latest_aha_prompts[-6:]

    tool_output_chars = sum(
        int(response_item_chars.get(key) or 0)
        for key in ("function_call_output:-", "custom_tool_call_output:-", "tool_result:-", "tool_result:user")
    )
    return {
        "backend": backend,
        "line_count": line_count,
        "parse_errors": parse_errors,
        "type_counts": type_counts,
        "response_item_counts": response_item_counts,
        "response_item_chars": response_item_chars,
        "aha_prompt_counts": aha_prompt_counts,
        "aha_prompt_chars": aha_prompt_chars,
        "aha_prompt_total_count": sum(int(value or 0) for value in aha_prompt_counts.values()),
        "aha_prompt_total_chars": sum(int(value or 0) for value in aha_prompt_chars.values()),
        "event_msg_prompt_mirror_counts": mirror_prompt_counts,
        "event_msg_prompt_mirror_chars": mirror_prompt_chars,
        "event_msg_prompt_mirror_total_count": sum(int(value or 0) for value in mirror_prompt_counts.values()),
        "event_msg_prompt_mirror_total_chars": sum(int(value or 0) for value in mirror_prompt_chars.values()),
        "tool_output_chars": tool_output_chars,
        "assistant_message_chars": int(response_item_chars.get("message:assistant") or 0),
        "total_payload_text_chars": total_payload_text_chars,
        "latest_prompt_mode": latest_aha_prompts[-1]["mode"] if latest_aha_prompts else "",
        "latest_aha_prompts": latest_aha_prompts,
    }


def backend_session_jsonl_info(session: dict) -> dict:
    session_id = str(session.get("backend_session_id") or "").strip()
    backend = str(session.get("backend") or "").strip()
    history = session.get("history_backend_sessions") if isinstance(session.get("history_backend_sessions"), list) else []
    compact_summary = session.get("compact_summary") if isinstance(session.get("compact_summary"), dict) else None
    if not session_id:
        return {"id": "", "backend": backend, "path": "", "size_bytes": None, "exists": False, "analysis": {}, "history": history, "compact_summary": compact_summary}

    candidates: list[Path] = []
    home = Path.home()
    if backend == "claude":
        candidates.extend((home / ".claude" / "projects").glob(f"*/*{session_id}.jsonl"))
    else:
        candidates.extend((home / ".codex" / "sessions").glob(f"**/*{session_id}.jsonl"))

    path = candidates[0] if candidates else None
    if not path or not path.exists():
        return {"id": session_id, "backend": backend, "path": "", "size_bytes": None, "exists": False, "analysis": {}, "history": history, "compact_summary": compact_summary}
    try:
        stat = path.stat()
    except OSError:
        return {"id": session_id, "backend": backend, "path": str(path), "size_bytes": None, "exists": False, "analysis": {}, "history": history, "compact_summary": compact_summary}
    try:
        analysis = analyze_backend_session_jsonl(path, backend)
    except OSError as exc:
        analysis = {"error": str(exc)}
    return {
        "id": session_id,
        "backend": backend,
        "path": str(path),
        "size_bytes": stat.st_size,
        "modified_at": stat.st_mtime,
        "exists": True,
        "analysis": analysis,
        "history": history,
        "compact_summary": compact_summary,
    }


def realtime_debug_log(source: str, **fields: object) -> None:
    root = fields.pop("_root", None)
    run_id = str(fields.get("run_id") or "")
    payload = {"ts": utc_now(), "source": source, **fields}
    line = "[aha realtime] " + json.dumps(payload, ensure_ascii=False, default=str)
    print(line, flush=True)
    if isinstance(root, Path) and run_id:
        try:
            log_dir = run_dir(root, run_id) / "logs"
            log_dir.mkdir(parents=True, exist_ok=True)
            with (log_dir / "realtime-debug.log").open("a", encoding="utf-8") as handle:
                handle.write(line + "\n")
        except OSError:
            pass
