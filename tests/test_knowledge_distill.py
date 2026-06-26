from __future__ import annotations

import contextlib
import io
import json
from pathlib import Path

from aha_cli.cli import main
from aha_cli.domain.models import default_knowledge_config
from aha_cli.services.knowledge_distill import (
    build_distill_context,
    distill_and_enqueue,
    distill_after_kb_command,
    distill_after_nav_command,
    heuristic_solution_candidate,
    normalize_sidecar_candidates,
)
from aha_cli.services.chat import write_memo_report_result
from aha_cli.store.config import load_config
from aha_cli.store.filesystem import event_path, iter_jsonl_from
from aha_cli.store.io import write_json
from aha_cli.store.knowledge import (
    approve_candidate,
    init_knowledge_base,
    knowledge_root,
    list_entries,
    list_pending,
    project_key,
    write_entry,
)
from aha_cli.store.paths import config_path
from aha_cli.store.runs import require_plan
from aha_cli.store.task_memos import create_task_memo
from aha_cli.store.knowledge_sidecar import split_knowledge_sidecar


def _cfg(gate: str = "manual", enabled: bool = True) -> dict:
    kb = default_knowledge_config()
    kb["enabled"] = enabled
    kb["curation"]["gate"] = gate
    return {"knowledge": kb}


def _context() -> dict:
    return build_distill_context(
        final_body="## 可复用经验\nWhen packaging zipapps, include imported submodules.",
        final_context={
            "summary": "Fix zipapp ModuleNotFound by bundling submodule",
            "changed_files": ["scripts/build_onebin.py"],
            "verification": ["python3 -m aha_cli --help"],
            "risks": ["only covers py3.10+"],
        },
        task_title="Package onebin",
        project_key_value="git-abc123",
        source={"run_id": "r1", "task_id": "task-001", "round_id": "1"},
    )


# --------------------------------------------------------------------------- #
def test_heuristic_candidate_shape():
    cands = heuristic_solution_candidate(_context())
    assert len(cands) == 1
    c = cands[0]
    assert c["kind"] == "solutions"
    assert c["scope"] == "project"
    assert c["project_key"] == "git-abc123"
    assert c["title"].startswith("Fix zipapp")
    assert "## 验证方式" in c["body"]
    assert "scripts/build_onebin.py" in c["body"]
    assert c["meta"]["distilled_by"] == "heuristic"
    assert c["meta"]["source_tasks"] == ["r1/task-001/1"]


def test_heuristic_uses_final_body_when_summary_missing():
    ctx = build_distill_context(
        final_body="## 可复用经验\nThe root cause was a stale lock file; deleting .aha/lock fixed startup.",
        final_context={"changed_files": ["src/x.py"]},  # no summary
        task_title="Startup hang",
        project_key_value="git-abc123",
        source={"run_id": "r1", "task_id": "t1", "round_id": "1"},
    )
    c = heuristic_solution_candidate(ctx)[0]
    # The final body is surfaced as the effective solution, not a placeholder.
    assert "stale lock file" in c["body"]
    assert "(见任务 final)" not in c["body"]


def test_heuristic_keeps_final_excerpt_section_when_summary_present():
    ctx = build_distill_context(
        final_body="## 可复用经验\nDetailed narrative about the fix and the debugging journey.",
        final_context={"summary": "Reusable startup lock cleanup playbook"},
        task_title="Startup hang",
        project_key_value="git-abc123",
        source={"run_id": "r1", "task_id": "t1", "round_id": "1"},
    )
    c = heuristic_solution_candidate(ctx)[0]
    assert "Reusable startup lock cleanup playbook" in c["body"]
    assert "## 来源摘录" in c["body"]
    assert "debugging journey" in c["body"]


def test_heuristic_keeps_useful_sections_without_truncating():
    long_line = "keep-this-detail " * 80
    ctx = build_distill_context(
        final_body=f"# 任务 Final\n\n## 任务轮次\nnoise\n\n## 稳定结果\n{long_line}\n\n## 可复用经验\nUse the exact command.",
        final_context={},
        task_title="Long report",
        project_key_value="repo-git-abc123",
        source={"run_id": "r1", "task_id": "t1", "round_id": "1"},
    )
    body = heuristic_solution_candidate(ctx)[0]["body"]
    assert "## 稳定结果" in body
    assert "## 可复用经验" in body
    assert "Use the exact command." in body
    assert "keep-this-detail" in body
    assert "…" not in body
    assert "noise" not in body


def test_heuristic_does_not_embed_prior_entries_in_body():
    # Prior knowledge must NOT be spliced into the candidate body: it bloats the
    # entry and (when a task is re-finalized) creates self-referential entries.
    ctx = build_distill_context(
        final_body="## 可复用经验\nNew result says the old cache workaround is obsolete.",
        final_context={"changed_files": ["src/cache.py"]},
        task_title="Cache behavior changed",
        project_key_value="repo-git-abc123",
        source={"run_id": "r1", "task_id": "t1", "round_id": "1"},
        prior_entries=[
            {
                "meta": {"id": "kb_old", "title": "Old cache workaround", "project_key": "repo-git-abc123"},
                "body": "Previously clear the cache on every startup.",
            }
        ],
    )
    c = heuristic_solution_candidate(ctx)[0]
    assert "## 既有知识复核" not in c["body"]
    assert "Old cache workaround" not in c["body"]


def test_heuristic_skips_plain_bug_fix_even_with_files_and_verification():
    ctx = build_distill_context(
        final_body="Fixed the task list active filter fallback.",
        final_context={
            "summary": "Fix task list active filter fallback",
            "changed_files": ["src/aha_cli/web/static/task_list.js"],
            "verification": ["python3 -m pytest tests/test_frontend_static.py"],
        },
        task_title="Fix task list bug",
        project_key_value="repo-git-abc123",
        source={"run_id": "r1", "task_id": "t1", "round_id": "1"},
    )
    assert heuristic_solution_candidate(ctx) == []


def test_heuristic_skips_task_with_no_reusable_signal():
    # Pure Q&A / no-op task: no summary, no changed files, no verification, and a
    # report that is only task narrative -> nothing reusable, produce nothing.
    ctx = build_distill_context(
        final_body=(
            "# 任务 Final：科普一下云端工程师\n\n"
            "## 任务轮次\n纯问答，main 直接作答。\n\n"
            "## 变更文件与决策\n无文件变更（纯问答，未触碰仓库）。\n\n"
            "## 验证\n无需代码验证。"
        ),
        final_context={},
        task_title="科普一下云端工程师",
        project_key_value="repo-git-abc123",
        source={"run_id": "r1", "task_id": "t1", "round_id": "1"},
    )
    assert heuristic_solution_candidate(ctx) == []


def test_split_knowledge_sidecar_strips_visible_report():
    visible, candidates, error = split_knowledge_sidecar(
        '## Final\nDone.\n<aha_knowledge_candidates>[{"title":"Reuse","body":"Do x"}]</aha_knowledge_candidates>'
    )
    assert error is None
    assert visible == "## Final\nDone."
    assert candidates == [{"title": "Reuse", "body": "Do x"}]


def test_sidecar_solution_body_uses_solution_template_when_generated():
    cand = normalize_sidecar_candidates(
        _context(),
        [{
            "kind": "solutions",
            "title": "Fix startup",
            "problem": "Service fails after stale lock.",
            "solution": "Delete the stale lock and restart.",
            "related_files": ["src/app.py"],
            "verification": ["service starts"],
            "invalid_when": "Locking is redesigned.",
        }],
    )[0]
    assert cand["kind"] == "solutions"
    assert "## 适用场景" in cand["body"]
    assert "## 推荐做法" in cand["body"]
    assert "## 验证方式" in cand["body"]
    assert "Delete the stale lock" in cand["body"]


def test_sidecar_wiki_body_uses_wiki_template_when_generated():
    cand = normalize_sidecar_candidates(
        _context(),
        [{
            "kind": "wiki",
            "title": "AHA source root rule",
            "conclusion": "Upgrade actions must run from the AHA source root.",
            "rules": "Do not use the active workspace cwd as the source root.",
            "related_files": ["src/aha_cli/web/system_routes.py"],
            "update_when": "Installer layout changes.",
        }],
    )[0]
    assert cand["kind"] == "wiki"
    assert cand["meta"]["type"] == "wiki"
    assert "## 结论" in cand["body"]
    assert "## 规则 / 约定" in cand["body"]
    assert "AHA source root" in cand["body"]


def test_distill_initializes_skeleton_without_prior_init(tmp_path: Path):
    # No init_knowledge_base / `aha kb init` beforehand: distill must build the
    # skeleton (incl. the .gitignore that excludes .pending/) before writing.
    root = tmp_path / ".aha"
    cfg = _cfg("manual")
    res = distill_and_enqueue(root, cfg, _context())
    assert res["candidates"] == 1

    kb_root = knowledge_root(root, cfg)
    assert (kb_root / "aha-knowledge.json").is_file()
    assert (kb_root / "README.md").is_file()
    gitignore = kb_root / ".gitignore"
    assert gitignore.is_file()
    assert ".pending/" in gitignore.read_text()
    assert len(list_pending(root, cfg)) == 1


def test_manual_gate_enqueues_without_touching_tracked_tree(tmp_path: Path):
    root = tmp_path / ".aha"
    cfg = _cfg("manual")
    init_knowledge_base(root, cfg)
    res = distill_and_enqueue(root, cfg, _context())
    assert res["gate"] == "manual"
    assert res["candidates"] == 1

    pending = list_pending(root, cfg)
    assert len(pending) == 1
    # Nothing written into the tracked solutions tree yet.
    assert list_entries(root, config=cfg, scope="project", kind="solutions",
                        project_key_value="git-abc123") == []


def test_auto_gate_writes_entry_directly(tmp_path: Path):
    root = tmp_path / ".aha"
    cfg = _cfg("auto")
    init_knowledge_base(root, cfg)
    res = distill_and_enqueue(root, cfg, _context())
    assert res["gate"] == "auto"
    assert len(res["written"]) == 1
    entries = list_entries(root, config=cfg, scope="project", kind="solutions",
                           project_key_value="git-abc123")
    assert len(entries) == 1
    # git disabled -> commit step is skipped, not failed.
    assert res["git"].get("skipped")


def test_gate_off_and_disabled_skip(tmp_path: Path):
    root = tmp_path / ".aha"
    assert distill_and_enqueue(root, _cfg("off"), _context())["candidates"] == 0
    assert "skipped" in distill_and_enqueue(root, _cfg(enabled=False), _context())


def test_approve_promotes_pending_to_entry(tmp_path: Path):
    root = tmp_path / ".aha"
    cfg = _cfg("manual")
    init_knowledge_base(root, cfg)
    distill_and_enqueue(root, cfg, _context())
    pending = list_pending(root, cfg)
    assert len(pending) == 1
    cid = pending[0]["id"]

    entry_path = approve_candidate(root, cfg, cid)
    assert entry_path.exists()
    assert list_pending(root, cfg) == []
    entries = list_entries(root, config=cfg, scope="project", kind="solutions",
                           project_key_value="git-abc123")
    assert len(entries) == 1
    assert entries[0]["meta"]["outcome"] == "success"


def test_pluggable_distiller_overrides_heuristic(tmp_path: Path):
    root = tmp_path / ".aha"
    cfg = _cfg("manual")
    init_knowledge_base(root, cfg)

    def fake(ctx: dict) -> list[dict]:
        return [{"kind": "solutions", "scope": "general", "title": "Custom", "body": "x", "meta": {}}]

    res = distill_and_enqueue(root, cfg, _context(), distiller=fake)
    assert res["candidates"] == 1
    assert list_pending(root, cfg)[0]["title"] == "Custom"


# --------------------------------------------------------------------------- #
# Integration: finalize only feeds back navigation.
# --------------------------------------------------------------------------- #
def test_finalize_does_not_distill_non_navigation_without_sidecar(tmp_path: Path):
    home = tmp_path / ".aha"
    rc = main(["--home", str(home), "init"])
    assert rc == 0
    out = io.StringIO()
    import contextlib

    with contextlib.redirect_stdout(out):
        rc = main(["--home", str(home), "plan", "Build onebin", "--agents", "1"])
    assert rc == 0
    run_id = out.getvalue().splitlines()[0].split(": ", 1)[1].strip()

    # Enable knowledge distillation in this home's config.
    cfg = load_config(home)
    cfg["knowledge"]["enabled"] = True
    write_json(config_path(home), cfg)

    from aha_cli.store.finals import write_task_result

    write_task_result(
        home,
        run_id,
        "task-001",
        "## 可复用经验\nBundle submodule into the zipapp when imports are loaded dynamically.",
        policy="finalize",
        final_context={
            "summary": "Bundle submodule into the zipapp",
            "changed_files": ["scripts/build_onebin.py"],
            "verification": ["aha --help"],
            "risks": ["py3.10+ only"],
        },
    )

    pending = list_pending(home, load_config(home))
    assert pending == []


def test_finalize_plain_bug_fix_does_not_create_heuristic_candidate(tmp_path: Path):
    home = tmp_path / ".aha"
    assert main(["--home", str(home), "init"]) == 0
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        assert main(["--home", str(home), "plan", "Bug fix", "--agents", "1"]) == 0
    run_id = out.getvalue().splitlines()[0].split(": ", 1)[1].strip()
    cfg = load_config(home)
    cfg["knowledge"]["enabled"] = True
    write_json(config_path(home), cfg)

    from aha_cli.store.finals import write_task_result

    write_task_result(
        home,
        run_id,
        "task-001",
        "Fixed the active filter fallback.",
        policy="finalize",
        final_context={
            "summary": "Fix active filter fallback",
            "changed_files": ["src/aha_cli/web/static/task_list.js"],
            "verification": ["frontend static tests"],
        },
    )

    assert list_pending(home, load_config(home)) == []


def test_memo_report_triggers_distill_when_enabled(tmp_path: Path):
    home = tmp_path / ".aha"
    rc = main(["--home", str(home), "init"])
    assert rc == 0
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        rc = main(["--home", str(home), "plan", "Memo workflow", "--agents", "1"])
    assert rc == 0
    run_id = out.getvalue().splitlines()[0].split(": ", 1)[1].strip()
    cfg = load_config(home)
    cfg["knowledge"]["enabled"] = True
    write_json(config_path(home), cfg)
    memo = create_task_memo(home, run_id, {"title": "Memo closeout", "created_task_id": "task-001"})

    updated = write_memo_report_result(
        home,
        run_id,
        {"memo_report_context": {"memo_id": memo["id"], "task_id": "task-001"}},
        "## 完成报告\n\n## 可复用经验\nMemo report captured a reusable solution.",
        0,
    )

    assert updated and updated["report_status"] == "ready"
    pending = list_pending(home, load_config(home))
    assert len(pending) == 1
    assert pending[0]["source"]["source_type"] == "memo_report"
    assert pending[0]["source"]["memo_id"] == memo["id"]


def test_final_non_navigation_sidecar_is_stripped_without_enqueue(tmp_path: Path):
    home = tmp_path / ".aha"
    main(["--home", str(home), "init"])
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        main(["--home", str(home), "plan", "Sidecar flow", "--agents", "1"])
    run_id = out.getvalue().splitlines()[0].split(": ", 1)[1].strip()
    cfg = load_config(home)
    cfg["knowledge"]["enabled"] = True
    write_json(config_path(home), cfg)

    from aha_cli.store.finals import write_task_result

    write_task_result(
        home,
        run_id,
        "task-001",
        '## Final\nVisible only.\n<aha_knowledge_candidates>[{"title":"Use sidecar","body":"## When\\nUse this next time.","tags":["kb"]}]</aha_knowledge_candidates>',
        policy="finalize",
    )

    plan = require_plan(home, run_id)
    output = home / "runs" / run_id / plan["tasks"][0]["output_file"]
    assert "<aha_knowledge_candidates>" not in output.read_text(encoding="utf-8")
    pending = list_pending(home, load_config(home))
    assert pending == []


def test_kb_command_sidecar_is_enqueued(tmp_path: Path):
    home = tmp_path / ".aha"
    main(["--home", str(home), "init"])
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        main(["--home", str(home), "plan", "KB command flow", "--agents", "1"])
    run_id = out.getvalue().splitlines()[0].split(": ", 1)[1].strip()
    cfg = load_config(home)
    cfg["knowledge"]["enabled"] = True
    write_json(config_path(home), cfg)
    plan = require_plan(home, run_id)
    task = plan["tasks"][0]

    result = distill_after_kb_command(
        home,
        run_id,
        "task-001",
        "已整理。",
        task_title=task["title"],
        workspace_path=task.get("workspace_path"),
        goal=plan.get("goal"),
        sidecar_candidates=[
            {
                "kind": "solutions",
                "scope": "project",
                "title": "蓝牙配网流程整理",
                "body": "## 适用场景\n整理蓝牙配网流程时只保留用户确认过的步骤。",
                "tags": ["bluetooth"],
            }
        ],
    )

    pending = list_pending(home, load_config(home))
    assert result["candidates"] == 1
    assert len(pending) == 1
    assert pending[0]["title"] == "蓝牙配网流程整理"
    assert pending[0]["source"]["source_type"] == "kb_command"
    assert pending[0]["kind"] == "solutions"


def test_kb_command_sidecar_keeps_distinct_chinese_titles_with_same_ascii_slug(tmp_path: Path):
    home = tmp_path / ".aha"
    main(["--home", str(home), "init"])
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        main(["--home", str(home), "plan", "KB command cjk title collision", "--agents", "1"])
    run_id = out.getvalue().splitlines()[0].split(": ", 1)[1].strip()
    cfg = load_config(home)
    cfg["knowledge"]["enabled"] = True
    write_json(config_path(home), cfg)
    plan = require_plan(home, run_id)
    task = plan["tasks"][0]

    result = distill_after_kb_command(
        home,
        run_id,
        "task-001",
        "已整理两条。",
        task_title=task["title"],
        workspace_path=task.get("workspace_path"),
        goal=plan.get("goal"),
        sidecar_candidates=[
            {
                "kind": "solutions",
                "scope": "project",
                "title": "Wyze 云存上传双路视频排查要点",
                "body": "正常链路生成 V1V2A1，长事件优先检查 MC 分片长度。",
                "tags": ["wyze", "cloud-upload"],
            },
            {
                "kind": "solutions",
                "scope": "project",
                "title": "Wyze 云存业务层时间戳对齐逻辑",
                "body": "业务层维护公共时间基准，不直接透传 localsdk PTS。",
                "tags": ["wyze", "timestamp"],
            },
        ],
    )

    pending = list_pending(home, load_config(home))
    assert result["candidates"] == 2
    assert len(pending) == 2
    assert {item["title"] for item in pending} == {
        "Wyze 云存上传双路视频排查要点",
        "Wyze 云存业务层时间戳对齐逻辑",
    }
    assert len({item["id"] for item in pending}) == 2


def test_nav_command_sidecar_is_enqueued_as_navigation(tmp_path: Path):
    home = tmp_path / ".aha"
    main(["--home", str(home), "init"])
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        main(["--home", str(home), "plan", "Nav command flow", "--agents", "1"])
    run_id = out.getvalue().splitlines()[0].split(": ", 1)[1].strip()
    cfg = load_config(home)
    cfg["knowledge"]["enabled"] = True
    write_json(config_path(home), cfg)
    plan = require_plan(home, run_id)
    task = plan["tasks"][0]
    key = project_key(Path(task.get("workspace_path")), goal=plan.get("goal"))
    write_entry(
        home,
        config=load_config(home),
        scope="project",
        kind="navigation",
        project_key_value=key,
        title="项目导航",
        body="## 模块索引\n",
        slug="index",
        meta={"type": "navigation"},
    )

    result = distill_after_nav_command(
        home,
        run_id,
        "task-001",
        "已整理导航。",
        task_title=task["title"],
        workspace_path=task.get("workspace_path"),
        goal=plan.get("goal"),
        sidecar_candidates=[
            {
                "kind": "navigation",
                "scope": "project",
                "slug": "modules/knowledge",
                "title": "知识库模块导航",
                "responsibility": "负责 /aha kb 和 /aha nav 产生的知识候选入库。",
                "related_files": ["src/aha_cli/services/knowledge_distill.py"],
                "navigation_reason": "显式 nav 命令更新项目导航。",
            }
        ],
    )

    pending = list_pending(home, load_config(home))
    assert result["candidates"] == 2
    assert len(pending) == 2
    knowledge = next(item for item in pending if item["slug"] == "modules/knowledge")
    assert knowledge["title"] == "知识库模块导航"
    assert knowledge["source"]["source_type"] == "nav_command"
    assert knowledge["kind"] == "navigation"


def test_final_navigation_sidecar_emits_nav_delta_event(tmp_path: Path):
    home = tmp_path / ".aha"
    main(["--home", str(home), "init"])
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        main(["--home", str(home), "plan", "Weixin readonly diagnostic", "--agents", "1"])
    run_id = out.getvalue().splitlines()[0].split(": ", 1)[1].strip()
    cfg = load_config(home)
    cfg["knowledge"]["enabled"] = True
    write_json(config_path(home), cfg)
    plan = require_plan(home, run_id)
    workspace_path = str((plan.get("tasks") or [{}])[0].get("workspace_path") or (plan.get("main_agent") or {}).get("workspace_path"))
    key = project_key(Path(workspace_path), goal=str(plan.get("goal") or ""))
    write_entry(
        home,
        config=load_config(home),
        scope="project",
        kind="navigation",
        project_key_value=key,
        title="项目导航",
        body="## 模块索引\n",
        slug="index",
        meta={"type": "navigation"},
    )

    from aha_cli.store.finals import write_task_result

    write_task_result(
        home,
        run_id,
        "task-001",
        (
            "## Final\nRead-only diagnosis.\n"
            '<aha_knowledge_candidates>[{"kind":"navigation",'
            '"title":"AHA 微信通知模块导航",'
            '"slug":"modules/weixin-notifications",'
            '"responsibility":"负责微信配对状态、通知开关和主动推送前的会话上下文检查。",'
            '"related_files":["src/aha_cli/services/weixin.py","src/aha_cli/services/weixin_notifications.py","src/aha_cli/web/system_routes.py"],'
            '"entry_points":["/api/weixin","/api/weixin/notifications"],'
            '"diagnostic_paths":["已配对但收不到消息时先看 notification_status.ready 和 send_context.state。"],'
            '"navigation_reason":"read-only diagnostic discovered the reusable Weixin notification path"}]</aha_knowledge_candidates>'
        ),
        policy="finalize",
    )

    pending = list_pending(home, load_config(home))
    by_slug = {item["slug"]: item for item in pending}
    assert set(by_slug) == {"modules/weixin-notifications", "index"}
    assert "## 常用排查路径" in by_slug["modules/weixin-notifications"]["body"]
    assert "modules/weixin-notifications.md" in by_slug["index"]["body"]

    events, _ = iter_jsonl_from(event_path(home, run_id), 0)
    distilled = next(event for event in events if event["type"] == "knowledge_task_final_distilled")
    assert distilled["data"]["result"]["navigation"]["candidates"] == 2
    delta = next(event for event in events if event["type"] == "knowledge_navigation_delta")
    assert delta["data"]["source_type"] == "task_final"
    assert "modules/weixin-notifications" in delta["data"]["navigation"]["slugs"]


def test_linked_final_and_memo_sidecars_merge_pending_candidate(tmp_path: Path):
    home = tmp_path / ".aha"
    main(["--home", str(home), "init"])
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        main(["--home", str(home), "plan", "Merge sidecars", "--agents", "1"])
    run_id = out.getvalue().splitlines()[0].split(": ", 1)[1].strip()
    cfg = load_config(home)
    cfg["knowledge"]["enabled"] = True
    write_json(config_path(home), cfg)
    memo = create_task_memo(home, run_id, {"title": "Merge memo", "created_task_id": "task-001"})

    from aha_cli.store.finals import write_task_result

    write_task_result(
        home,
        run_id,
        "task-001",
        '## Final\nDone.\n<aha_knowledge_candidates>[{"title":"Same reusable rule","body":"Final version"}]</aha_knowledge_candidates>',
        policy="finalize",
    )
    updated = write_memo_report_result(
        home,
        run_id,
        {"memo_report_context": {"memo_id": memo["id"], "task_id": "task-001"}},
        '## Report\nDone.\n<aha_knowledge_candidates>[{"title":"Same reusable rule","body":"Memo-enriched version"}]</aha_knowledge_candidates>',
        0,
    )

    assert updated and "<aha_knowledge_candidates>" not in updated["completion_report"]
    pending = list_pending(home, load_config(home))
    assert len(pending) == 1
    assert pending[0]["title"] == "Same reusable rule"
    assert pending[0]["body"] == "Memo-enriched version\n"
    assert pending[0]["source_group"].endswith("/task/task-001")
    assert len(pending[0]["sources"]) == 1
    assert pending[0]["sources"][0]["source_type"] == "memo_report"


def test_finalize_distill_is_noop_when_disabled(tmp_path: Path):
    home = tmp_path / ".aha"
    main(["--home", str(home), "init"])
    import contextlib

    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        main(["--home", str(home), "plan", "X", "--agents", "1"])
    run_id = out.getvalue().splitlines()[0].split(": ", 1)[1].strip()

    from aha_cli.store.finals import write_task_result

    # knowledge.enabled defaults to False -> finalize must not create a KB.
    write_task_result(home, run_id, "task-001", "body", policy="finalize",
                      final_context={"summary": "s"})
    assert list_pending(home, load_config(home)) == []


def test_cli_kb_pending_lists_candidates(tmp_path: Path):
    root = tmp_path / ".aha"
    cfg = _cfg("manual")
    init_knowledge_base(root, cfg)
    distill_and_enqueue(root, cfg, _context())
    # Persist config so the CLI sees the same knowledge home.
    write_json(config_path(root), cfg)

    import contextlib

    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        rc = main(["--home", str(root), "kb", "pending", "--json"])
    assert rc == 0
    listed = json.loads(out.getvalue())
    assert len(listed) == 1
    assert listed[0]["title"].startswith("Fix zipapp")
