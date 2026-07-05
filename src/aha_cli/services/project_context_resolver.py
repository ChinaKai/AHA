from __future__ import annotations

import re
from pathlib import Path
from typing import Iterable

from aha_cli.store.knowledge import (
    NAVIGATION_SLUG,
    knowledge_config,
    knowledge_root,
    parse_entry,
    project_key_aliases,
)


_NAV_LINK_RE = re.compile(r"!?\[[^\]]+\]\(([^)#?]+)(?:#[^)]*)?\)")
_CODE_SPAN_RE = re.compile(r"`([^`]+)`")
_PATH_HINT_SUFFIXES = (
    ".cfg",
    ".css",
    ".h",
    ".html",
    ".ini",
    ".js",
    ".json",
    ".md",
    ".py",
    ".rst",
    ".toml",
    ".ts",
    ".tsx",
    ".txt",
    ".yaml",
    ".yml",
)
_STOP = {
    "and",
    "are",
    "for",
    "from",
    "into",
    "that",
    "the",
    "this",
    "with",
    "you",
    "任务",
    "用户",
}


def _tokenize_text(text: str) -> list[str]:
    pieces: list[str] = []
    normalized = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", str(text or ""))
    for token in re.split(r"[^A-Za-z0-9]+", normalized.lower()):
        if len(token) < 2 or token in _STOP:
            continue
        pieces.append(token)
    joined = "".join(pieces)
    if len(joined) >= 3 and joined not in pieces:
        pieces.append(joined)
    return pieces


def _terms(*texts: str) -> list[str]:
    seen: list[str] = []
    for text in texts:
        raw = str(text or "")
        for token in re.findall(r"[a-zA-Z0-9_./+-]{2,}", raw):
            for part in _tokenize_text(token):
                if part not in seen:
                    seen.append(part)
        for run in re.findall(r"[一-鿿]{2,}", raw):
            for index in range(len(run) - 1):
                bigram = run[index : index + 2]
                if bigram not in _STOP and bigram not in seen:
                    seen.append(bigram)
    return seen


def _slug_from_nav_href(href: str) -> str:
    value = str(href or "").strip().replace("\\", "/")
    if not value or value.startswith(("http://", "https://", "mailto:")):
        return ""
    value = value.split("#", 1)[0].split("?", 1)[0].strip("/")
    if value.endswith(".md"):
        value = value[:-3]
    if value.startswith("./"):
        value = value[2:]
    parts = [part for part in value.split("/") if part and part not in {".", ".."}]
    return "/".join(parts)


def _entry_file(kb_root: Path, project_key_value: str, slug: str) -> Path:
    suffix = "index" if slug == NAVIGATION_SLUG else slug
    return kb_root / "projects" / project_key_value / "navigation" / f"{suffix}.md"


def _entry_relpath(path: Path, kb_root: Path) -> str:
    try:
        return path.relative_to(kb_root).as_posix()
    except ValueError:
        return str(path)


def _read_nav_doc(path: Path, *, slug: str, kb_root: Path) -> dict | None:
    try:
        meta, body = parse_entry(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return {
        "slug": slug,
        "path": _entry_relpath(path, kb_root),
        "title": str(meta.get("title") or slug),
        "summary": str(meta.get("summary") or meta.get("description") or ""),
        "tags": [str(tag) for tag in (meta.get("tags") or []) if str(tag).strip()],
        "related_files": [str(item) for item in (meta.get("related_files") or []) if str(item).strip()],
        "body": body,
    }


def _navigation_docs_for_project(kb_root: Path, project_key_value: str) -> list[dict]:
    index_path = _entry_file(kb_root, project_key_value, NAVIGATION_SLUG)
    index_doc = _read_nav_doc(index_path, slug=NAVIGATION_SLUG, kb_root=kb_root)
    if index_doc is None:
        return []
    docs = [index_doc]
    seen = {NAVIGATION_SLUG}
    for href in _NAV_LINK_RE.findall(index_doc.get("body") or ""):
        slug = _slug_from_nav_href(href)
        if not slug or slug in seen:
            continue
        doc = _read_nav_doc(_entry_file(kb_root, project_key_value, slug), slug=slug, kb_root=kb_root)
        if doc is None:
            continue
        docs.append(doc)
        seen.add(slug)
    return docs


def _navigation_docs(root: Path, config: dict | None, workspace: Path, project_key_value: str | None) -> list[dict]:
    cfg = knowledge_config(config)
    project_nav = cfg.get("project_nav") if isinstance(cfg.get("project_nav"), dict) else {}
    if not project_nav.get("enabled", True):
        return []
    kb_root = knowledge_root(root, config)
    keys = [project_key_value] if project_key_value else project_key_aliases(workspace)
    seen_paths: set[str] = set()
    docs: list[dict] = []
    for key in [str(item) for item in keys if str(item or "").strip()]:
        for doc in _navigation_docs_for_project(kb_root, key):
            path = str(doc.get("path") or "")
            if path in seen_paths:
                continue
            docs.append(doc)
            seen_paths.add(path)
    return docs


def _doc_score(doc: dict, query_terms: list[str]) -> int:
    title = str(doc.get("title") or "").lower()
    slug = str(doc.get("slug") or "").lower()
    summary = str(doc.get("summary") or "").lower()
    body = str(doc.get("body") or "").lower()
    related = " ".join(str(item) for item in (doc.get("related_files") or [])).lower()
    tags = " ".join(str(item) for item in (doc.get("tags") or [])).lower()
    score = 0
    matched = 0
    for term in query_terms:
        term = term.lower()
        term_score = 0
        if term in title:
            term_score += 8
        if term in slug:
            term_score += 6
        if term in tags:
            term_score += 5
        if term in related:
            term_score += 5
        if term in summary:
            term_score += 4
        if term in body:
            term_score += 2
        if term_score:
            matched += 1
            score += term_score
    if matched >= 2:
        score += matched * 3
    return score


def _ordered_unique(values: Iterable[str], *, limit: int) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for value in values:
        item = str(value or "").strip()
        if not item or item in seen:
            continue
        seen.add(item)
        out.append(item)
        if len(out) >= limit:
            break
    return out


def _path_terms(path: str) -> list[str]:
    tokens = _terms(path)
    parts = [part for part in re.split(r"[/._+-]+", str(path or "")) if len(part) >= 2]
    return _ordered_unique([*tokens, *[part.lower() for part in parts]], limit=16)


def _code_terms_from_doc(doc: dict) -> list[str]:
    values: list[str] = []
    values.extend(str(item) for item in (doc.get("related_files") or []))
    for span in _CODE_SPAN_RE.findall(str(doc.get("body") or "")):
        span = span.strip()
        if not span:
            continue
        if any(marker in span for marker in ("/", ".", "_", "-")) or re.match(r"[A-Z0-9_]{4,}$", span):
            values.append(span)
    terms: list[str] = []
    for value in values:
        terms.extend(_path_terms(value))
        if "/" in value or "." in value:
            terms.append(value)
    terms.extend(_terms(doc.get("title", ""), doc.get("summary", ""), doc.get("slug", "")))
    return _ordered_unique(terms, limit=32)


def _looks_like_path_hint(value: str) -> bool:
    normalized = str(value or "").replace("\\", "/").strip("/")
    if "/" in normalized:
        return True
    return normalized.lower().endswith(_PATH_HINT_SUFFIXES)


def _path_hints_from_doc(doc: dict, workspace: Path | None = None) -> tuple[list[str], list[str]]:
    values: list[str] = []
    values.extend(str(item) for item in (doc.get("related_files") or []))
    for span in _CODE_SPAN_RE.findall(str(doc.get("body") or "")):
        span = span.strip()
        if "/" in span or "." in span:
            values.append(span)
    hints: list[str] = []
    stale: list[str] = []
    workspace_path = Path(workspace).expanduser() if workspace is not None else None
    for value in values:
        normalized = value.strip().strip(".,;:")
        if not normalized or normalized.startswith(("http://", "https://")):
            continue
        if not _looks_like_path_hint(normalized):
            continue
        target = normalized.replace("\\", "/").strip("/")
        if workspace_path is not None and not (workspace_path / target).exists():
            stale.append(target)
            continue
        hints.append(target)
        if "/" in normalized:
            parent = target.rsplit("/", 1)[0]
            if parent:
                hints.append(parent)
    return _ordered_unique(hints, limit=24), _ordered_unique(stale, limit=24)


def resolve_project_context_query(
    root: Path,
    workspace: Path,
    query: str,
    *,
    config: dict | None = None,
    project_key_value: str | None = None,
    max_nav_routes: int = 3,
    max_terms: int = 48,
    min_nav_score: int = 8,
) -> dict:
    """Expand a natural-language map query through project navigation docs.

    The resolver is intentionally deterministic: navigation docs route the
    natural-language request to module/path/code terms, and the project map
    remains responsible for final source/config/build ranking.
    """
    query_text = str(query or "").strip()
    query_terms = _terms(query_text)
    if not query_text or not query_terms:
        return {
            "original_query": query_text,
            "query": query_text,
            "terms": query_terms,
            "used_navigation": False,
            "nav_routes": [],
            "expanded_terms": [],
            "path_hints": [],
            "stale_path_hints": [],
        }
    docs = _navigation_docs(root, config, Path(workspace).expanduser(), project_key_value)
    scored = [(_doc_score(doc, query_terms), doc) for doc in docs]
    scored = [
        (score, doc)
        for score, doc in scored
        if score >= min_nav_score and doc.get("slug") != NAVIGATION_SLUG
    ]
    scored.sort(key=lambda item: (-item[0], str(item[1].get("slug") or "")))
    selected = scored[: max(0, max_nav_routes)]
    if not selected:
        return {
            "original_query": query_text,
            "query": query_text,
            "terms": query_terms,
            "used_navigation": False,
            "nav_routes": [],
            "expanded_terms": [],
            "path_hints": [],
            "stale_path_hints": [],
        }
    hint_pairs = [_path_hints_from_doc(doc, workspace) for _score, doc in selected]
    path_hints = _ordered_unique([hint for hints, _stale in hint_pairs for hint in hints], limit=24)
    stale_path_hints = _ordered_unique([hint for _hints, stale in hint_pairs for hint in stale], limit=24)
    expanded_terms = _ordered_unique(
        [
            *query_terms,
            *[
                term
                for _score, doc in selected
                for term in _code_terms_from_doc(doc)
            ],
        ],
        limit=max_terms,
    )
    expanded_query = " ".join(expanded_terms)
    return {
        "original_query": query_text,
        "query": expanded_query or query_text,
        "terms": expanded_terms,
        "used_navigation": True,
        "expanded_terms": [term for term in expanded_terms if term not in query_terms],
        "path_hints": path_hints,
        "stale_path_hints": stale_path_hints,
        "nav_routes": [
            {
                "slug": str(doc.get("slug") or ""),
                "title": str(doc.get("title") or ""),
                "path": str(doc.get("path") or ""),
                "score": score,
            }
            for score, doc in selected
        ],
    }


__all__ = ["resolve_project_context_query"]
