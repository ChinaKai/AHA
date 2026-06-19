from __future__ import annotations

import json

SIDECAR_OPEN = "<aha_knowledge_candidates>"
SIDECAR_CLOSE = "</aha_knowledge_candidates>"


def split_knowledge_sidecar(text: str) -> tuple[str, list[dict] | None, str | None]:
    """Return visible text, parsed sidecar candidates, and an optional error.

    ``None`` candidates means no usable sidecar was present, so callers may use
    their fallback distiller. An empty list means an explicit sidecar said there
    are no reusable candidates.
    """
    raw_text = text or ""
    start = raw_text.find(SIDECAR_OPEN)
    if start < 0:
        return raw_text, None, None
    body_start = start + len(SIDECAR_OPEN)
    end = raw_text.find(SIDECAR_CLOSE, body_start)
    if end < 0:
        visible = (raw_text[:start]).rstrip()
        return visible, None, "knowledge sidecar is missing closing tag"

    visible = (raw_text[:start] + raw_text[end + len(SIDECAR_CLOSE):]).strip()
    payload = raw_text[body_start:end].strip()
    try:
        parsed = json.loads(payload or "[]")
    except json.JSONDecodeError as exc:
        return visible, None, f"knowledge sidecar JSON is invalid: {exc.msg}"

    candidates = parsed.get("candidates") if isinstance(parsed, dict) else parsed
    if not isinstance(candidates, list):
        return visible, None, "knowledge sidecar must be a JSON list or {candidates: [...]}"
    usable = [item for item in candidates if isinstance(item, dict)]
    return visible, usable, None
