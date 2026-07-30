from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re

from aha_cli.store.config import load_config
from aha_cli.store.event_views import event_task_id, searchable_conversation_messages
from aha_cli.store.knowledge import iter_all_entries
from aha_cli.store.knowledge_capture import list_notes
from aha_cli.store.runs import list_run_summaries, require_plan
from aha_cli.store.task_memos import read_task_memos

GLOBAL_SEARCH_TYPES = {"all", "task", "memo", "kb"}


def _text(value: object) -> str:
    return str(value or "").strip()


@dataclass(frozen=True)
class _SearchAtom:
    source: str
    pattern: re.Pattern[str]

    def search(self, value: str) -> re.Match[str] | None:
        return self.pattern.search(value)


@dataclass(frozen=True)
class _SearchExpression:
    source: str
    clauses: tuple[tuple[_SearchAtom, ...], ...]

    @property
    def is_single_atom(self) -> bool:
        return len(self.clauses) == 1 and len(self.clauses[0]) == 1

    def search(self, value: str) -> tuple[re.Match[str], ...] | None:
        for clause in self.clauses:
            matches: list[re.Match[str]] = []
            for atom in clause:
                match = atom.search(value)
                if match is None:
                    break
                matches.append(match)
            else:
                return tuple(matches)
        return None

    def matches(self, value: str) -> bool:
        return self.search(value) is not None

    def fullmatches(self, value: str) -> bool:
        if not self.is_single_atom:
            return False
        return self.clauses[0][0].pattern.fullmatch(value) is not None


def _regex_token_end(query: str, start: int) -> int | None:
    escaped = False
    for index in range(start + 1, len(query)):
        character = query[index]
        if escaped:
            escaped = False
            continue
        if character == "\\":
            escaped = True
            continue
        if character != "/":
            continue
        end = index + 1
        while end < len(query) and query[end] in "ims":
            end += 1
        if end == len(query) or query[end].isspace() or query[end] in "&|":
            return end
    return None


def _query_tokens(query: str) -> list[tuple[str, str]]:
    tokens: list[tuple[str, str]] = []
    index = 0
    while index < len(query):
        if query[index].isspace():
            index += 1
            continue
        if query[index] in "&|":
            operator = query[index]
            tokens.append(("operator", operator))
            index += 2 if index + 1 < len(query) and query[index + 1] == operator else 1
            continue
        if query[index] == "/":
            regex_end = _regex_token_end(query, index)
            if regex_end is not None:
                tokens.append(("atom", query[index:regex_end]))
                index = regex_end
                continue
        end = index
        while end < len(query):
            if query[end].isspace() or query[end] in "&|":
                break
            end += 1
        tokens.append(("atom", query[index:end]))
        index = end
    return tokens


def _compile_atom(source: str) -> _SearchAtom:
    if source.startswith("/") and len(source) > 1:
        flags_start = len(source)
        while flags_start > 0 and source[flags_start - 1] in "ims":
            flags_start -= 1
        delimiter = flags_start - 1
        if delimiter > 0 and source[delimiter] == "/":
            flag_text = source[flags_start:]
            flags = 0
            if "i" in flag_text:
                flags |= re.IGNORECASE
            if "m" in flag_text:
                flags |= re.MULTILINE
            if "s" in flag_text:
                flags |= re.DOTALL
            try:
                pattern = re.compile(source[1:delimiter], flags)
            except re.error as exc:
                raise ValueError(f"invalid regular expression: {exc}") from exc
            if pattern.search("") is not None:
                raise ValueError("regular expression must not match empty text")
            return _SearchAtom(source=source, pattern=pattern)

    if "*" in source:
        wildcard_source = source.strip("*")
        pattern_text = "[^\\n]*?".join(re.escape(part) for part in wildcard_source.split("*"))
        if not pattern_text:
            pattern_text = ".+?"
    else:
        pattern_text = re.escape(source)
    return _SearchAtom(
        source=source,
        pattern=re.compile(pattern_text, re.IGNORECASE | re.DOTALL),
    )


def _compile_search_expression(query: str) -> _SearchExpression:
    clauses: list[list[_SearchAtom]] = [[]]
    expects_atom = True
    for token_type, token in _query_tokens(query):
        if token_type == "atom":
            clauses[-1].append(_compile_atom(token))
            expects_atom = False
            continue
        if expects_atom:
            raise ValueError(f"search operator {token} requires a term on both sides")
        if token == "|":
            clauses.append([])
        expects_atom = True
    if expects_atom:
        raise ValueError("search expression cannot end with an operator")
    return _SearchExpression(
        source=query,
        clauses=tuple(tuple(clause) for clause in clauses),
    )


def _first_match(expression: _SearchExpression, value: str) -> re.Match[str] | None:
    matches = expression.search(value)
    if not matches:
        return None
    return min(matches, key=lambda match: (match.start(), -len(match.group(0))))


def _matched_text(match: re.Match[str], limit: int = 200) -> str:
    return match.group(0)[:limit]


def _search_score(
    expression: _SearchExpression,
    *,
    title: str,
    item_id: str,
    description: str,
    run_goal: str,
) -> int:
    fields = {
        "title": title,
        "id": item_id,
        "description": description,
        "run_goal": run_goal,
    }
    haystack = " ".join(fields.values())
    if not expression.matches(haystack):
        return 0
    score = 1
    if expression.fullmatches(fields["title"]):
        score += 120
    elif expression.matches(fields["title"]):
        score += 80 if expression.is_single_atom else 55
    if expression.fullmatches(fields["id"]):
        score += 100
    elif expression.matches(fields["id"]):
        score += 45
    if expression.matches(fields["description"]):
        score += 25
    if expression.matches(fields["run_goal"]):
        score += 10
    return score


def _match_details(
    expression: _SearchExpression,
    *,
    title: str,
    item_id: str,
    description: str,
    context: str,
) -> tuple[str, str]:
    for field, value in (
        ("title", title),
        ("description", description),
        ("id", item_id),
        ("context", context),
    ):
        match = _first_match(expression, value)
        if match is not None:
            return field, _matched_text(match)
    return "", ""


def _matched_excerpt(value: str, expression: _SearchExpression, limit: int = 500) -> str:
    text = _text(value)
    if len(text) <= limit:
        return text
    match = _first_match(expression, text)
    index = match.start() if match is not None else 0
    start = max(0, index - limit // 3)
    end = min(len(text), start + limit)
    start = max(0, end - limit)
    return f"{'…' if start else ''}{text[start:end]}{'…' if end < len(text) else ''}"


def _task_result(run: dict, task: dict, expression: _SearchExpression) -> dict | None:
    title = _text(task.get("title"))
    item_id = _text(task.get("id"))
    description = _text(task.get("description"))
    run_goal = _text(run.get("goal"))
    score = _search_score(
        expression,
        title=title,
        item_id=item_id,
        description=description,
        run_goal=run_goal,
    )
    if not score:
        return None
    match_field, match_query = _match_details(
        expression,
        title=title,
        item_id=item_id,
        description=description,
        context=run_goal,
    )
    return {
        "type": "task",
        "run_id": _text(run.get("id")),
        "run_goal": run_goal,
        "run_lifecycle_status": _text(run.get("lifecycle_status")),
        "id": item_id,
        "title": title or item_id,
        "description": description,
        "status": _text(task.get("status")),
        "hidden": bool(task.get("hidden")),
        "created_at": _text(task.get("created_at")),
        "updated_at": _text(task.get("updated_at") or task.get("completed_at") or task.get("created_at")),
        "match_field": match_field,
        "match_query": match_query,
        "_score": score,
    }


def _memo_result(run: dict, memo: dict, expression: _SearchExpression) -> dict | None:
    title = _text(memo.get("title"))
    item_id = _text(memo.get("id"))
    description = _text(memo.get("description"))
    run_goal = _text(run.get("goal"))
    score = _search_score(
        expression,
        title=title,
        item_id=item_id,
        description=description,
        run_goal=run_goal,
    )
    if not score:
        return None
    match_field, match_query = _match_details(
        expression,
        title=title,
        item_id=item_id,
        description=description,
        context=run_goal,
    )
    return {
        "type": "memo",
        "run_id": _text(run.get("id")),
        "run_goal": run_goal,
        "run_lifecycle_status": _text(run.get("lifecycle_status")),
        "id": item_id,
        "title": title or item_id,
        "description": _matched_excerpt(description, expression),
        "status": _text(memo.get("status")),
        "scheduled_date": _text(memo.get("scheduled_date")),
        "created_task_id": _text(memo.get("created_task_id")),
        "created_at": _text(memo.get("created_at")),
        "updated_at": _text(memo.get("updated_at") or memo.get("created_at")),
        "match_field": match_field,
        "match_query": match_query,
        "_score": score,
    }


def _chat_result(run: dict, task: dict, event: dict, expression: _SearchExpression) -> dict | None:
    data = event.get("data") if isinstance(event.get("data"), dict) else {}
    message = _text(data.get("message"))
    match = _first_match(expression, message)
    if not message or match is None:
        return None
    score = 30
    if expression.fullmatches(message):
        score += 100
    elif expression.is_single_atom:
        score += 55
    return {
        "type": "task",
        "result_kind": "chat",
        "run_id": _text(run.get("id")),
        "run_goal": _text(run.get("goal")),
        "run_lifecycle_status": _text(run.get("lifecycle_status")),
        "id": _text(task.get("id")),
        "title": _text(task.get("title")) or _text(task.get("id")),
        "description": _matched_excerpt(message, expression),
        "status": _text(task.get("status")),
        "hidden": bool(task.get("hidden")),
        "message_cursor": str(event.get("_cursor")) if event.get("_cursor") is not None else "",
        "message_sender": _text(data.get("display_sender") or data.get("sender") or data.get("from_agent")),
        "message_target": _text(data.get("display_target") or data.get("target") or data.get("to_agent")),
        "created_at": _text(event.get("ts")),
        "updated_at": _text(event.get("ts")),
        "match_field": "description",
        "match_query": _matched_text(match),
        "_score": score,
    }


def _kb_entry_result(entry: dict, expression: _SearchExpression) -> dict | None:
    meta = entry.get("meta") if isinstance(entry.get("meta"), dict) else {}
    item_id = _text(meta.get("id") or meta.get("slug"))
    title = _text(meta.get("title") or meta.get("slug") or item_id)
    body = _text(entry.get("body"))
    context = " ".join(
        [
            _text(meta.get("slug")),
            _text(meta.get("project_key")),
            _text(meta.get("scope")),
            _text(meta.get("type")),
            " ".join(_text(tag) for tag in (meta.get("tags") or [])),
        ]
    )
    score = _search_score(
        expression,
        title=title,
        item_id=item_id,
        description=body,
        run_goal=context,
    )
    if not score:
        return None
    match_field, match_query = _match_details(
        expression,
        title=title,
        item_id=item_id,
        description=body,
        context=context,
    )
    return {
        "type": "kb",
        "kb_kind": "entry",
        "id": item_id,
        "slug": _text(meta.get("slug")),
        "title": title or item_id,
        "description": _matched_excerpt(body, expression),
        "status": _text(meta.get("status") or "active"),
        "scope": _text(meta.get("scope")),
        "entry_type": _text(meta.get("type")),
        "project_key": _text(meta.get("project_key")),
        "created_at": _text(meta.get("created_at")),
        "updated_at": _text(meta.get("updated_at") or meta.get("created_at")),
        "match_field": match_field,
        "match_query": match_query,
        "_score": score,
    }


def _kb_note_result(note: dict, expression: _SearchExpression) -> dict | None:
    item_id = _text(note.get("id"))
    text = _text(note.get("text"))
    title = _text(note.get("title") or (text.splitlines()[0] if text else item_id))
    context = " ".join(
        [
            _text(note.get("scope_hint")),
            _text(note.get("status")),
            " ".join(_text(candidate_id) for candidate_id in (note.get("candidate_ids") or [])),
        ]
    )
    score = _search_score(
        expression,
        title=title,
        item_id=item_id,
        description=text,
        run_goal=context,
    )
    if not score:
        return None
    match_field, match_query = _match_details(
        expression,
        title=title,
        item_id=item_id,
        description=text,
        context=context,
    )
    return {
        "type": "kb",
        "kb_kind": "note",
        "id": item_id,
        "title": title or item_id,
        "description": _matched_excerpt(text, expression),
        "status": _text(note.get("status") or "raw"),
        "scope": _text(note.get("scope_hint")),
        "created_at": _text(note.get("created_at")),
        "updated_at": _text(note.get("updated_at") or note.get("created_at")),
        "match_field": match_field,
        "match_query": match_query,
        "_score": score,
    }


def global_search_snapshot(
    root: Path,
    query_text: object,
    *,
    result_type: object = "all",
    limit: int = 50,
    scope_run_id: object = "",
    scope_task_id: object = "",
    chat_only: bool = False,
) -> dict:
    query = _text(query_text)[:200]
    normalized_type = _text(result_type).lower() or "all"
    if normalized_type not in GLOBAL_SEARCH_TYPES:
        raise ValueError("type must be one of: all, task, memo, kb")
    bounded_limit = max(1, min(int(limit), 100))
    normalized_scope_run_id = _text(scope_run_id)
    normalized_scope_task_id = _text(scope_task_id)
    if chat_only and (not normalized_scope_run_id or not normalized_scope_task_id):
        raise ValueError("chat_only requires scope_run_id and scope_task_id")
    if not query:
        return {
            "ok": True,
            "query": "",
            "type": normalized_type,
            "results": [],
            "total": 0,
            "returned": 0,
            "limit": bounded_limit,
            "scope": None,
        }
    expression = _compile_search_expression(query)

    results: list[dict] = []
    task_results: dict[tuple[str, str], dict] = {}
    for run in list_run_summaries(root):
        run_id = _text(run.get("id"))
        if chat_only and run_id != normalized_scope_run_id:
            continue
        try:
            plan = require_plan(root, run_id)
            if normalized_type in {"all", "task"}:
                tasks_by_id: dict[str, dict] = {}
                for task in plan.get("tasks", []):
                    if not isinstance(task, dict) or task.get("deleted_at"):
                        continue
                    task_id = _text(task.get("id"))
                    if chat_only and task_id != normalized_scope_task_id:
                        continue
                    tasks_by_id[task_id] = task
                    if not chat_only:
                        result = _task_result(run, task, expression)
                        if result:
                            task_results[(run_id, task_id)] = result
                for event in searchable_conversation_messages(root, run_id):
                    task_id = _text(event_task_id(event))
                    task = tasks_by_id.get(task_id)
                    if not task:
                        continue
                    result = _chat_result(run, task, event, expression)
                    if not result:
                        continue
                    if chat_only:
                        results.append(result)
                        continue
                    key = (run_id, task_id)
                    summary = task_results.get(key)
                    if not summary:
                        summary = {
                            **result,
                            "result_kind": "task",
                            "message_cursor": "",
                            "message_sender": "",
                            "message_target": "",
                        }
                        task_results[key] = summary
                    summary["chat_match_count"] = int(summary.get("chat_match_count") or 0) + 1
                    if not summary.get("chat_preview"):
                        summary["chat_preview"] = result["description"]
                    summary["_score"] = max(int(summary.get("_score") or 0), int(result.get("_score") or 0))
                    if result.get("updated_at", "") > summary.get("updated_at", ""):
                        summary["updated_at"] = result["updated_at"]
            if not chat_only and normalized_type in {"all", "memo"}:
                for memo in read_task_memos(root, run_id):
                    result = _memo_result(run, memo, expression)
                    if result:
                        results.append(result)
        except (OSError, SystemExit, ValueError, KeyError):
            continue

    if not chat_only:
        results.extend(task_results.values())

    if not chat_only and normalized_type in {"all", "kb"}:
        try:
            config = load_config(root)
            seen_entries: set[str] = set()
            for entry in iter_all_entries(root, config):
                meta = entry.get("meta") if isinstance(entry.get("meta"), dict) else {}
                identity = _text(meta.get("id") or meta.get("slug") or entry.get("path"))
                if not identity or identity in seen_entries:
                    continue
                seen_entries.add(identity)
                result = _kb_entry_result(entry, expression)
                if result:
                    results.append(result)
            for note in list_notes(root, config):
                result = _kb_note_result(note, expression)
                if result:
                    results.append(result)
        except (OSError, SystemExit, ValueError, KeyError):
            pass

    results.sort(
        key=lambda item: (
            int(item.get("_score") or 0),
            _text(item.get("updated_at") or item.get("created_at")),
            _text(item.get("run_id")),
            _text(item.get("id")),
        ),
        reverse=True,
    )
    total = len(results)
    page = results[:bounded_limit]
    for item in page:
        item.pop("_score", None)
    return {
        "ok": True,
        "query": query,
        "type": normalized_type,
        "results": page,
        "total": total,
        "returned": len(page),
        "limit": bounded_limit,
        "scope": {
            "run_id": normalized_scope_run_id,
            "task_id": normalized_scope_task_id,
        } if chat_only else None,
    }


__all__ = ["GLOBAL_SEARCH_TYPES", "global_search_snapshot"]
