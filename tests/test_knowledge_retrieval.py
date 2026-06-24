from __future__ import annotations

import contextlib
import io
from pathlib import Path

from aha_cli.cli import main
from aha_cli.domain.models import default_knowledge_config
from aha_cli.services.knowledge_retrieval import (
    format_injection,
    knowledge_context_for_task,
    retrieve_for_task,
    _terms,
)
from aha_cli.services.orchestrator import dispatch_task_to_main, task_assignment_prompt
from aha_cli.store.config import load_config
from aha_cli.store.io import write_json
from aha_cli.store.knowledge import init_knowledge_base, project_key as derive_project_key, project_key_aliases, write_entry
from aha_cli.store.paths import config_path, inbox_path
from aha_cli.store.runs import require_plan


def _cfg(enabled: bool = True) -> dict:
    kb = default_knowledge_config()
    kb["enabled"] = enabled
    return {"knowledge": kb}


def _make_git_workspace(path: Path, remote: str) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    git_dir = path / ".git"
    git_dir.mkdir()
    (git_dir / "config").write_text(f'[remote "origin"]\n\turl = {remote}\n', encoding="utf-8")
    return path


def test_terms_drop_stopwords_and_short():
    terms = _terms("Fix the flaky CI build", "with retries")
    assert "fix" in terms and "flaky" in terms and "build" in terms
    assert "the" not in terms and "ci" not in terms  # too short / stopword


def test_cjk_terms_and_retrieval(tmp_path: Path):
    terms = _terms("串口桥接超时怎么处理")
    assert "串口" in terms and "桥接" in terms
    root = tmp_path / ".aha"
    cfg = _cfg()
    init_knowledge_base(root, cfg)
    write_entry(root, config=cfg, scope="general", kind="wiki",
                title="串口桥接说明", body="超时就调整波特率并重试", meta={})
    hits = retrieve_for_task(root, cfg, project_key="git-none", terms=terms, max_entries=5)
    assert any("串口桥接" in h["meta"]["title"] for h in hits)


def test_retrieve_ranks_by_overlap_then_recency_fallback(tmp_path: Path):
    root = tmp_path / ".aha"
    cfg = _cfg()
    init_knowledge_base(root, cfg)
    write_entry(root, config=cfg, scope="project", kind="solutions", project_key_value="git-abc",
                title="Fix flaky build", body="rerun clean cache", meta={"tags": ["build"]})
    write_entry(root, config=cfg, scope="project", kind="solutions", project_key_value="git-abc",
                title="Serial timeout", body="adjust baud", meta={"tags": ["serial"]})

    hits = retrieve_for_task(root, cfg, project_key="git-abc", terms=["flaky", "build"], max_entries=5)
    assert hits[0]["meta"]["title"] == "Fix flaky build"

    # No term match -> fallback to project entries (non-empty).
    fallback = retrieve_for_task(root, cfg, project_key="git-abc", terms=["nomatch"], max_entries=5)
    assert len(fallback) == 2

    # Wrong project -> nothing.
    assert retrieve_for_task(root, cfg, project_key="git-OTHER", terms=["flaky"], max_entries=5) == []


def test_retrieve_skips_deprecated_entries(tmp_path: Path):
    root = tmp_path / ".aha"
    cfg = _cfg()
    init_knowledge_base(root, cfg)
    write_entry(root, config=cfg, scope="project", kind="solutions", project_key_value="git-abc",
                title="Old build fix", body="deprecated clean cache", meta={"status": "deprecated"})
    write_entry(root, config=cfg, scope="project", kind="solutions", project_key_value="git-abc",
                title="Current build fix", body="use the supported build command", meta={})

    hits = retrieve_for_task(root, cfg, project_key="git-abc", terms=["build", "fix"], max_entries=5)
    assert [hit["meta"]["title"] for hit in hits] == ["Current build fix"]


def test_retrieve_respects_project_nav_enabled(tmp_path: Path):
    root = tmp_path / ".aha"
    cfg = _cfg()
    init_knowledge_base(root, cfg)
    write_entry(root, config=cfg, scope="project", kind="navigation", project_key_value="git-abc",
                title="项目导航", body="## 模块索引\n- [web](modules/web.md)", meta={}, slug="index")
    write_entry(root, config=cfg, scope="project", kind="solutions", project_key_value="git-abc",
                title="Web fix", body="HTTP route fix", meta={})

    hits = retrieve_for_task(root, cfg, project_key="git-abc", terms=["web"], max_entries=5)
    assert any(hit["meta"]["type"] == "navigation" for hit in hits)

    cfg["knowledge"]["project_nav"]["enabled"] = False
    hits = retrieve_for_task(root, cfg, project_key="git-abc", terms=["web"], max_entries=5)
    assert all(hit["meta"]["type"] != "navigation" for hit in hits)
    assert [hit["meta"]["title"] for hit in hits] == ["Web fix"]


def test_format_injection_bounds_chars(tmp_path: Path):
    entries = [
        {"meta": {"title": f"T{i}", "type": "solution"}, "body": "x" * 500}
        for i in range(10)
    ]
    out = format_injection(entries, max_chars=600)
    assert "项目已知经验" in out
    assert len(out) <= 600  # hard budget


def test_format_injection_defaults_to_reference_mode(tmp_path: Path):
    kb_root = tmp_path / "knowledge"
    entries = [
        {
            "meta": {
                "title": "项目导航",
                "type": "navigation",
                "slug": "index",
                "tags": ["navigation"],
                "updated_at": "2026-06-01T00:00:00+00:00",
            },
            "body": "NAV_BODY_SHOULD_NOT_APPEAR modules and flows",
            "path": str(kb_root / "projects" / "git-abc" / "navigation" / "index.md"),
        },
        {
            "meta": {"title": "Avoid stale lock", "type": "solution", "slug": "avoid-stale-lock", "tags": ["startup"]},
            "body": "short summary start. FULL_BODY_SHOULD_NOT_APPEAR " * 20,
            "path": str(kb_root / "projects" / "git-abc" / "solutions" / "avoid-stale-lock.md"),
        },
    ]

    out = format_injection(entries, kb_root=kb_root, project_key="git-abc", summary_chars=24)

    assert "KB root:" in out
    assert "Project key: git-abc" in out
    assert "Project nav path: projects/git-abc/navigation/index.md" in out
    assert "path: projects/git-abc/solutions/avoid-stale-lock.md" in out
    assert "tags: startup" in out
    assert "summary: short summary start." in out
    assert "[navigation]" not in out
    assert "slug: index" not in out
    assert "tags: navigation" not in out
    assert "2026-06-01" not in out
    assert "NAV_BODY_SHOULD_NOT_APPEAR" not in out
    assert "FULL_BODY_SHOULD_NOT_APPEAR" not in out


def test_reference_mode_only_injects_navigation_index_path_for_large_nav(tmp_path: Path):
    kb_root = tmp_path / "knowledge"
    entries = [
        {
            "meta": {"title": "项目导航", "type": "navigation", "slug": "index", "tags": ["navigation"]},
            "body": "## 模块索引\n- [store](modules/store.md)",
            "path": str(kb_root / "projects" / "git-abc" / "navigation" / "index.md"),
        }
    ]
    entries.extend(
        {
            "meta": {"title": f"module {idx}", "type": "navigation", "slug": f"modules/module-{idx}", "tags": ["module"]},
            "body": f"NAV_DETAIL_BODY_{idx} " * 80,
            "path": str(kb_root / "projects" / "git-abc" / "navigation" / "modules" / f"module-{idx}.md"),
        }
        for idx in range(20)
    )
    entries.append(
        {
            "meta": {"title": "Useful fix", "type": "solution", "slug": "useful-fix", "tags": ["build"]},
            "body": "check build cache before retrying",
            "path": str(kb_root / "projects" / "git-abc" / "solutions" / "useful-fix.md"),
        }
    )

    out = format_injection(entries, kb_root=kb_root, project_key="git-abc", max_chars=1200)

    assert "Project nav path: projects/git-abc/navigation/index.md" in out
    assert "Project nav rule:" in out
    assert "Useful fix" in out
    assert "path: projects/git-abc/solutions/useful-fix.md" in out
    assert "module 0" not in out
    assert "modules/module-0.md" not in out
    assert "NAV_DETAIL_BODY_0" not in out
    assert "## 模块索引" not in out
    assert len(out) < 1200


def test_format_injection_excerpts_mode_preserves_legacy_body_snippets():
    entries = [
        {"meta": {"title": "项目导航", "type": "navigation", "slug": "index"}, "body": "## 模块索引\n- [store](modules/store.md)"},
        {"meta": {"title": "Fix", "type": "solution"}, "body": "do x"},
    ]

    out = format_injection(entries, mode="excerpts")

    assert "## 模块索引" in out
    assert "do x" in out
    assert 'kind:"navigation"' in out


def test_injection_adds_navigation_contract_only_when_navigation_present():
    # Plain knowledge: no navigation directive (existing injection behavior unchanged).
    plain = format_injection([{"meta": {"title": "Fix", "type": "solution"}, "body": "do x"}])
    assert "项目已知经验" in plain
    assert "navigation" not in plain
    assert "项目导航" not in plain

    # With a navigation entry present, default references only name the entry point.
    withmap = format_injection([
        {"meta": {"title": "项目导航", "type": "navigation", "slug": "index"}, "body": "## 模块索引\n- [store](modules/store.md)"},
        {"meta": {"title": "Fix", "type": "solution"}, "body": "do x"},
    ])
    assert "Project nav rule:" in withmap
    assert "navigation/index" in withmap
    assert "默认不展开 navigation detail" in withmap
    assert 'kind:"navigation"' not in withmap
    assert "## 模块索引" not in withmap  # default reference mode keeps nav body out of the prompt


def test_retrieve_keeps_navigation_details_on_demand(tmp_path: Path):
    root = tmp_path / ".aha"
    cfg = _cfg()
    init_knowledge_base(root, cfg)
    write_entry(
        root, config=cfg, scope="project", kind="navigation", project_key_value="git-abc",
        title="项目导航", body="## 模块索引\n- [store](modules/store.md)\n- [web](modules/web.md)",
        slug="index", meta={"type": "navigation"},
    )
    write_entry(
        root, config=cfg, scope="project", kind="navigation", project_key_value="git-abc",
        title="store 模块", body="filesystem persistence and JSONL state",
        slug="modules/store", meta={"type": "navigation"},
    )
    write_entry(
        root, config=cfg, scope="project", kind="solutions", project_key_value="git-abc",
        title="Recent unrelated fix", body="ordinary project fallback",
        meta={"type": "solution"},
    )

    fallback = retrieve_for_task(root, cfg, project_key="git-abc", terms=["nomatch"], max_entries=5)
    assert [entry["meta"]["slug"] for entry in fallback] == ["index", "recent-unrelated-fix"]

    matched = retrieve_for_task(root, cfg, project_key="git-abc", terms=["filesystem", "jsonl"], max_entries=5)
    assert [entry["meta"]["slug"] for entry in matched][:2] == ["index", "modules/store"]


def test_retrieve_can_skip_navigation_details_for_reference_prompt(tmp_path: Path):
    root = tmp_path / ".aha"
    cfg = _cfg()
    init_knowledge_base(root, cfg)
    write_entry(
        root, config=cfg, scope="project", kind="navigation", project_key_value="git-abc",
        title="项目导航", body="## 模块索引\n- [store](modules/store.md)",
        slug="index", meta={"type": "navigation"},
    )
    for idx in range(8):
        write_entry(
            root, config=cfg, scope="project", kind="navigation", project_key_value="git-abc",
            title=f"store detail {idx}", body="filesystem jsonl routing",
            slug=f"modules/store-{idx}", meta={"type": "navigation"},
        )
    write_entry(
        root, config=cfg, scope="project", kind="solutions", project_key_value="git-abc",
        title="Store fix", body="filesystem jsonl repair", meta={"type": "solution"},
    )

    hits = retrieve_for_task(
        root,
        cfg,
        project_key="git-abc",
        terms=["filesystem", "jsonl"],
        max_entries=3,
        include_navigation_details=False,
    )

    assert [entry["meta"]["slug"] for entry in hits] == ["index", "store-fix"]


def test_format_injection_hard_budget_clips_first_long_entry():
    # A single very long first entry must NOT blow the budget.
    entries = [{"meta": {"title": "Big", "type": "solution"}, "body": "y" * 5000}]
    for budget in (120, 200, 400):
        out = format_injection(entries, max_chars=budget)
        assert len(out) <= budget, (budget, len(out))
        assert "项目已知经验" in out


def test_context_disabled_or_no_workspace_is_empty(tmp_path: Path):
    root = tmp_path / ".aha"
    write_json(config_path(root), _cfg(enabled=False))
    init_knowledge_base(root, _cfg())
    assert knowledge_context_for_task(root, "r", {"workspace_path": "/x", "title": "t"}) == ""
    # enabled but no workspace
    write_json(config_path(root), _cfg(enabled=True))
    assert knowledge_context_for_task(root, "r", {"title": "t"}) == ""


def test_context_includes_matching_entry(tmp_path: Path):
    root = tmp_path / ".aha"
    cfg = _cfg(enabled=True)
    write_json(config_path(root), cfg)
    init_knowledge_base(root, cfg)
    workspace = tmp_path / "proj"
    workspace.mkdir()
    # _plan_goal will fail (no plan) -> goal None; match that here.
    key = derive_project_key(workspace, goal=None)
    write_entry(root, config=cfg, scope="project", kind="solutions", project_key_value=key,
                title="Avoid stale lock", body="delete .aha/lock on startup", meta={"tags": ["startup"]})

    ctx = knowledge_context_for_task(root, "norun", {
        "workspace_path": str(workspace), "title": "startup hang", "description": "lock issue",
    })
    assert "项目已知经验" in ctx
    assert "Avoid stale lock" in ctx
    assert "path: projects/" in ctx


def test_context_excerpts_mode_compatibility(tmp_path: Path):
    root = tmp_path / ".aha"
    cfg = _cfg(enabled=True)
    cfg["knowledge"]["retrieval"]["inject_mode"] = "excerpts"
    write_json(config_path(root), cfg)
    init_knowledge_base(root, cfg)
    workspace = tmp_path / "proj"
    workspace.mkdir()
    key = derive_project_key(workspace, goal=None)
    write_entry(root, config=cfg, scope="project", kind="solutions", project_key_value=key,
                title="Avoid stale lock", body="delete .aha/lock on startup", meta={"tags": ["startup"]})

    ctx = knowledge_context_for_task(root, "norun", {
        "workspace_path": str(workspace), "title": "startup hang", "description": "lock issue",
    })

    assert "delete .aha/lock on startup" in ctx
    assert "path: projects/" not in ctx


def test_context_reference_mode_omits_navigation_details_and_keeps_solution_refs(tmp_path: Path):
    root = tmp_path / ".aha"
    cfg = _cfg(enabled=True)
    cfg["knowledge"]["retrieval"]["max_entries"] = 3
    write_json(config_path(root), cfg)
    init_knowledge_base(root, cfg)
    workspace = tmp_path / "proj"
    workspace.mkdir()
    key = derive_project_key(workspace, goal=None)
    write_entry(
        root, config=cfg, scope="project", kind="navigation", project_key_value=key,
        title="项目导航", body="## 模块索引\n- [store](modules/store.md)",
        slug="index", meta={"type": "navigation"},
    )
    for idx in range(6):
        write_entry(
            root, config=cfg, scope="project", kind="navigation", project_key_value=key,
            title=f"store module {idx}", body="startup lock filesystem details",
            slug=f"modules/store-{idx}", meta={"type": "navigation"},
        )
    write_entry(root, config=cfg, scope="project", kind="solutions", project_key_value=key,
                title="Avoid stale lock", body="delete .aha/lock on startup", meta={"tags": ["startup"]})

    ctx = knowledge_context_for_task(root, "norun", {
        "workspace_path": str(workspace), "title": "startup hang", "description": "lock filesystem",
    })

    assert "Project nav path: projects/" in ctx
    assert "navigation/index.md" in ctx
    assert "Avoid stale lock" in ctx
    assert "store module 0" not in ctx
    assert "modules/store-0.md" not in ctx
    assert "startup lock filesystem details" not in ctx


def test_context_reads_legacy_git_project_key(tmp_path: Path):
    root = tmp_path / ".aha"
    cfg = _cfg(enabled=True)
    write_json(config_path(root), cfg)
    init_knowledge_base(root, cfg)
    workspace = _make_git_workspace(tmp_path / "proj", "git@github.com:user/repo.git")
    aliases = project_key_aliases(workspace, goal=None)
    assert aliases[0].startswith("repo-git-")
    assert aliases[1].startswith("git-")
    write_entry(root, config=cfg, scope="project", kind="solutions", project_key_value=aliases[1],
                title="Legacy project key", body="old git hash key still works", meta={})

    ctx = knowledge_context_for_task(root, "norun", {
        "workspace_path": str(workspace), "title": "legacy", "description": "git hash key",
    })
    assert "Legacy project key" in ctx


def test_context_runs_auto_pull_and_tolerates_failure(tmp_path: Path, monkeypatch):
    root = tmp_path / ".aha"
    cfg = _cfg(enabled=True)
    cfg["knowledge"]["git"]["enabled"] = True
    write_json(config_path(root), cfg)
    init_knowledge_base(root, cfg)
    workspace = tmp_path / "proj"
    workspace.mkdir()
    key = derive_project_key(workspace, goal=None)
    write_entry(root, config=cfg, scope="project", kind="solutions", project_key_value=key,
                title="Avoid stale lock", body="delete .aha/lock", meta={})
    task = {"workspace_path": str(workspace), "title": "startup", "description": "lock"}

    calls = []
    monkeypatch.setattr(
        "aha_cli.services.knowledge_git.auto_pull_before_task",
        lambda r, c: calls.append(True) or {"ok": True, "pulled": False},
    )
    ctx = knowledge_context_for_task(root, "norun", task)
    assert calls, "auto_pull_before_task must be invoked before retrieval"
    assert "Avoid stale lock" in ctx

    # A pull that blows up must not break retrieval — fall back to local KB.
    def boom(r, c):
        raise RuntimeError("remote down")

    monkeypatch.setattr("aha_cli.services.knowledge_git.auto_pull_before_task", boom)
    ctx2 = knowledge_context_for_task(root, "norun", task)
    assert "Avoid stale lock" in ctx2


def test_task_assignment_prompt_embeds_context():
    prompt = task_assignment_prompt({"title": "T", "workspace_path": "/w"}, "项目已知经验 (knowledge base):\n- foo")
    assert "项目已知经验" in prompt
    # empty context still renders a valid prompt
    assert "collaboration_mode" in task_assignment_prompt({"title": "T"}, "")


def test_dispatch_injects_knowledge_into_prompt(tmp_path: Path):
    home = tmp_path / ".aha"
    assert main(["--home", str(home), "init"]) == 0
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        main(["--home", str(home), "plan", "Build it", "--agents", "1"])
    run_id = out.getvalue().splitlines()[0].split(": ", 1)[1].strip()

    cfg = load_config(home)
    cfg["knowledge"]["enabled"] = True
    write_json(config_path(home), cfg)

    task = next(t for t in require_plan(home, run_id)["tasks"] if t["id"] == "task-001")
    key = derive_project_key(Path(task["workspace_path"]), goal=require_plan(home, run_id).get("goal"))
    write_entry(home, config=load_config(home), scope="project", kind="solutions", project_key_value=key,
                title="Known pitfall", body="watch the build cache", meta={"tags": ["build"]})

    dispatch_task_to_main(home, run_id, task)
    inbox_text = inbox_path(home, run_id, "main").read_text(encoding="utf-8")
    assert "项目已知经验" in inbox_text
    assert "Known pitfall" in inbox_text
