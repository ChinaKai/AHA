from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from aha_cli.domain.models import utc_now
from aha_cli.store.events import append_event as default_append_event
from aha_cli.store.io import append_jsonl, read_json, write_json
from aha_cli.store.paths import run_dir
from aha_cli.store.rounds import (
    ensure_task_round_record,
    list_task_lifecycle_rounds,
    list_task_rounds,
    round_sequence_from_id,
    task_rounds_path,
)
from aha_cli.store.runs import locked_plan, require_plan, save_plan


def run_relative_path(root: Path, run_id: str, path: Path) -> str:
    try:
        return str(path.relative_to(run_dir(root, run_id)))
    except ValueError:
        return str(path)


def resolve_run_path(root: Path, run_id: str, value: object) -> Path:
    path = Path(str(value or ""))
    return path if path.is_absolute() else run_dir(root, run_id) / path


def string_list(value) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value.strip()] if value.strip() else []
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    return [str(value).strip()] if str(value).strip() else []


def render_task_rounds_markdown(task: dict, rounds: list[dict]) -> str:
    title = str(task.get("title") or task.get("id") or "Task")
    lines = ["# Final", "", f"Task: {title}", "", "## 任务轮次"]
    if not rounds:
        lines.append("")
        lines.append("_暂无任务轮次记录。_")
        return "\n".join(lines).rstrip() + "\n"
    for index, item in enumerate(rounds, start=1):
        heading = str(item.get("summary") or "").strip() or "(no summary)"
        prefix = str(item.get("round_id") or f"round-{item.get('sequence') or '?'}")
        trigger = str(item.get("trigger") or "manual")
        lines.append("")
        lines.append(f"{index}. **{heading}**")
        lines.append(f"   - 轮次：`{prefix}`")
        lines.append(f"   - 触发：`{trigger}`")
        changed_files = string_list(item.get("changed_files"))
        verification = string_list(item.get("verification"))
        risks = string_list(item.get("risks"))
        agents = string_list(item.get("agents"))
        if changed_files:
            lines.append(f"   - 文件：{', '.join(changed_files)}")
        if verification:
            lines.append(f"   - 验证：{'; '.join(verification)}")
        if risks:
            lines.append(f"   - 风险：{'; '.join(risks)}")
        if agents:
            lines.append(f"   - Agent：{', '.join(agents)}")
    return "\n".join(lines).rstrip() + "\n"


def _collect_unique_strings(entries: list[dict], key: str) -> list[str]:
    values: list[str] = []
    seen: set[str] = set()
    for entry in entries:
        for value in string_list(entry.get(key)):
            if value not in seen:
                values.append(value)
                seen.add(value)
    return values


def read_round_final(root: Path, run_id: str, round_record: dict) -> tuple[str, Path | None]:
    final_path = round_record.get("final_path")
    if not final_path:
        return "", None
    path = resolve_run_path(root, run_id, final_path)
    if not path.exists():
        return "", path
    return path.read_text(encoding="utf-8"), path


def latest_final_artifact(root: Path, run_id: str, lifecycle_rounds: list[dict]) -> tuple[dict | None, str, dict]:
    finalized_rounds = [item for item in lifecycle_rounds if item.get("status") == "finalized" and item.get("final_path")]
    if not finalized_rounds:
        return None, "", {}
    latest = finalized_rounds[-1]
    final_text, final_file = read_round_final(root, run_id, latest)
    final_meta: dict = {}
    meta_path = latest.get("final_meta_path")
    if meta_path:
        final_meta_file = resolve_run_path(root, run_id, meta_path)
        if final_meta_file.exists():
            final_meta = read_json(final_meta_file)
    elif final_file is not None:
        final_meta_file = final_file.with_suffix(".meta.json")
        if final_meta_file.exists():
            final_meta = read_json(final_meta_file)
    return latest, final_text, final_meta


def _task_output_has_overview(root: Path, run_id: str, task: dict) -> bool:
    output_file = run_dir(root, run_id) / task["output_file"]
    output_meta_file = output_file.with_suffix(".meta.json")
    if not output_meta_file.exists():
        return False
    try:
        return read_json(output_meta_file).get("format") == "task_overview"
    except (OSError, ValueError):
        return False


def _should_render_task_overview(
    root: Path,
    run_id: str,
    task: dict,
    lifecycle_rounds: list[dict],
    journal_entries: list[dict],
) -> bool:
    return (
        _task_output_has_overview(root, run_id, task)
        or len(lifecycle_rounds) > 1
        or bool(journal_entries)
        or any(item.get("reopened_from_round_id") for item in lifecycle_rounds)
    )


def _overview_inline_text(value: object) -> str:
    text = " ".join(str(value or "").split())
    for prefix in ("###### ", "##### ", "#### ", "### ", "## ", "# "):
        text = text.replace(prefix, "")
    return text


def _compact_summary(value: object, limit: int = 180) -> str:
    text = _overview_inline_text(value)
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 3)].rstrip() + "..."


def _entries_by_round(entries: list[dict]) -> dict[str, list[dict]]:
    grouped: dict[str, list[dict]] = {}
    for entry in entries:
        round_id = str(entry.get("round_id") or f"round-{entry.get('round_sequence') or '?'}")
        grouped.setdefault(round_id, []).append(entry)
    return grouped


def _round_overview_sentence(round_record: dict, entries: list[dict]) -> str:
    status = str(round_record.get("status") or "unknown")
    if not entries:
        return f"状态 `{status}`。"
    first = _compact_summary(entries[0].get("summary"), 110)
    latest = _compact_summary(entries[-1].get("summary"), 110)
    if len(entries) == 1 or first == latest:
        return first or f"状态 `{status}`。"
    return f"共 {len(entries)} 条进展；起点：{first}；最新：{latest}"


def _append_limited_section(lines: list[str], title: str, items: list[str], empty: str, limit: int = 6) -> None:
    lines.extend(["", f"## {title}"])
    if not items:
        lines.append(f"- {empty}")
        return
    for item in items[:limit]:
        lines.append(f"- {_overview_inline_text(item)}")
    remaining = len(items) - limit
    if remaining > 0:
        lines.append(f"- 另有 {remaining} 项，详见任务 journal。")


def render_task_overview_markdown(
    root: Path,
    run_id: str,
    task: dict,
    lifecycle_rounds: list[dict],
    journal_entries: list[dict],
) -> str:
    title = str(task.get("title") or task.get("id") or "Task")
    task_id = str(task.get("id") or "")
    latest_final, _latest_final_text, _latest_meta = latest_final_artifact(root, run_id, lifecycle_rounds)
    verification = _collect_unique_strings(journal_entries, "verification")
    risks = _collect_unique_strings(journal_entries, "risks")
    grouped_entries = _entries_by_round(journal_entries)

    lines = [
        "# Task Overview",
        "",
        f"Task: {title}",
        f"Task ID: `{task_id}`",
        f"Status: `{task.get('status') or 'unknown'}`",
        f"Current round: `{task.get('current_round_id') or '-'}`",
    ]
    if task.get("last_final_round_id"):
        lines.append(f"Last final round: `{task.get('last_final_round_id')}`")
    if task.get("last_final_at"):
        lines.append(f"Last final at: `{task.get('last_final_at')}`")
    if task.get("started_at"):
        lines.append(f"Started at: `{task.get('started_at')}`")
    if task.get("finished_at"):
        lines.append(f"Finished at: `{task.get('finished_at')}`")

    lines.extend(["", "## 任务轮次"])
    if lifecycle_rounds:
        for index, item in enumerate(lifecycle_rounds, start=1):
            round_id = str(item.get("round_id") or f"round-{item.get('sequence') or '?'}")
            summary = _round_overview_sentence(item, grouped_entries.get(round_id, []))
            lines.extend(["", f"{index}. `{round_id}` {summary}"])
            lines.append(f"   - 状态：`{item.get('status') or 'unknown'}`")
            if item.get("started_at"):
                lines.append(f"   - 开始：`{item.get('started_at')}`")
            if item.get("finalized_at"):
                lines.append(f"   - Final：`{item.get('finalized_at')}`")
            if item.get("reopened_from_round_id"):
                lines.append(f"   - Reopened from：`{item.get('reopened_from_round_id')}`")
    elif journal_entries:
        for index, entry in enumerate(journal_entries, start=1):
            round_id = str(entry.get("round_id") or f"round-{entry.get('round_sequence') or '?'}")
            summary = _compact_summary(entry.get("summary")) or "(no summary)"
            lines.extend(["", f"{index}. `{round_id}` {summary}"])
    else:
        lines.extend(["", "_暂无任务轮次记录。_"])

    lines.extend(["", "## 结果"])
    lines.append(f"- 当前状态：`{task.get('status') or 'unknown'}`")
    lines.append(f"- 当前轮次：`{task.get('current_round_id') or '-'}`")
    if journal_entries:
        lines.append(f"- Journal 记录：{len(journal_entries)} 条。")
    if lifecycle_rounds:
        finalized_count = sum(1 for item in lifecycle_rounds if item.get("status") == "finalized")
        lines.append(f"- Lifecycle round：{len(lifecycle_rounds)} 轮，其中 {finalized_count} 轮已有 Final 快照。")
    if latest_final:
        lines.append(f"- 最新 raw Final：`{latest_final.get('round_id')}`。")
    elif not journal_entries:
        lines.append("- 尚无 Final。")
    if journal_entries:
        latest_summaries = [_compact_summary(item.get("summary"), 120) for item in journal_entries[-2:]]
        latest_summaries = [item for item in latest_summaries if item]
        if latest_summaries:
            lines.append("- 最新进展：" + "；".join(latest_summaries))

    _append_limited_section(lines, "验证", verification, "暂无明确验证记录。")
    _append_limited_section(lines, "剩余风险", risks, "暂无明确剩余风险。")

    lines.extend(["", "## 详细快照索引"])
    finalized = [item for item in lifecycle_rounds if item.get("status") == "finalized" and item.get("final_path")]
    if not finalized:
        lines.extend(["", "_暂无 Final 快照。_"])
    for item in finalized:
        round_id = str(item.get("round_id") or f"round-{item.get('sequence') or '?'}")
        lines.extend(["", f"### `{round_id}`"])
        _final_text, final_path = read_round_final(root, run_id, item)
        if final_path is not None:
            lines.append(f"- Raw final: `{run_relative_path(root, run_id, final_path)}`")
        if item.get("finalized_at"):
            lines.append(f"- Finalized at: `{item.get('finalized_at')}`")
        round_entries = grouped_entries.get(round_id, [])
        if round_entries:
            lines.append(f"- Journal entries: {len(round_entries)}")

    return "\n".join(lines).rstrip() + "\n"


def render_task_overview_result(
    root: Path,
    run_id: str,
    task_id: str,
    policy: str = "journal",
    force: bool = False,
    *,
    now_func: Callable[[], str] = utc_now,
    append_event_func: Callable[[Path, str, str, dict], dict] = default_append_event,
) -> Path:
    plan = require_plan(root, run_id)
    task = next((item for item in plan["tasks"] if item["id"] == task_id), None)
    if task is None:
        raise KeyError(task_id)
    run = run_dir(root, run_id)
    lifecycle_rounds = list_task_lifecycle_rounds(root, run_id, task_id)
    journal_entries = list_task_rounds(root, run_id, task_id)
    path = run / task["output_file"]
    if not force and not _should_render_task_overview(root, run_id, task, lifecycle_rounds, journal_entries):
        return path
    content = render_task_overview_markdown(root, run_id, task, lifecycle_rounds, journal_entries)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    write_json(
        path.with_suffix(".meta.json"),
        {
            "task_id": task_id,
            "policy": policy,
            "format": "task_overview",
            "updated_at": now_func(),
            "round_count": len(lifecycle_rounds),
            "journal_count": len(journal_entries),
            "current_round_id": task.get("current_round_id"),
            "last_final_round_id": task.get("last_final_round_id"),
        },
    )
    append_event_func(
        root,
        run_id,
        "task_journal_rendered",
        {"task_id": task_id, "path": str(path), "round_count": len(journal_entries), "format": "task_overview"},
    )
    return path


def append_task_round(
    root: Path,
    run_id: str,
    task_id: str,
    entry: dict,
    *,
    now_func: Callable[[], str] = utc_now,
    append_event_func: Callable[[Path, str, str, dict], dict] = default_append_event,
    render_journal_func: Callable[[Path, str, str], Path] | None = None,
) -> dict:
    with locked_plan(root, run_id):
        plan = require_plan(root, run_id)
        task = next((item for item in plan["tasks"] if item["id"] == task_id), None)
        if task is None or task.get("deleted_at"):
            raise KeyError(task_id)
        lifecycle_round = ensure_task_round_record(root, run_id, task, now_func=now_func)
        save_plan(root, plan)
        write_json(run_dir(root, run_id) / "tasks" / task_id / "task.json", task)
    rounds = list_task_rounds(root, run_id, task_id)
    journal_sequence = len(rounds) + 1
    round_id = str(entry.get("round_id") or lifecycle_round.get("round_id") or task.get("current_round_id") or "round-001")
    round_sequence = int(round_sequence_from_id(round_id) or lifecycle_round.get("sequence") or 1)
    payload = {
        "task_id": task_id,
        "round_id": round_id,
        "round_sequence": round_sequence,
        "sequence": round_sequence,
        "journal_id": str(entry.get("journal_id") or f"journal-{journal_sequence:03d}"),
        "journal_sequence": journal_sequence,
        "at": str(entry.get("at") or now_func()),
        "trigger": str(entry.get("trigger") or "manual"),
        "summary": str(entry.get("summary") or "").strip(),
        "changed_files": string_list(entry.get("changed_files")),
        "verification": string_list(entry.get("verification")),
        "risks": string_list(entry.get("risks")),
        "agents": string_list(entry.get("agents")),
    }
    if not payload["summary"]:
        raise ValueError("Task round summary is required")
    append_jsonl(task_rounds_path(root, run_id, task_id), payload)
    if render_journal_func:
        render_journal_func(root, run_id, task_id)
    append_event_func(
        root,
        run_id,
        "task_round_recorded",
        {
            "task_id": task_id,
            "round_id": payload["round_id"],
            "journal_id": payload["journal_id"],
            "trigger": payload["trigger"],
            "chars": len(payload["summary"]),
        },
    )
    return payload
