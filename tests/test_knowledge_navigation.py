from __future__ import annotations

import json
import re
from pathlib import Path

from aha_cli.domain.models import default_knowledge_config
from aha_cli.services.knowledge_distill import (
    distill_and_enqueue,
    normalize_sidecar_candidates,
    navigation_delta_event_payload,
)
from aha_cli.services.knowledge_navigation import (
    bootstrap_project_navigation,
    build_navigation_candidate,
    build_navigation_candidates,
    build_navigation_bootstrap_prompt,
    generate_navigation_candidate,
    scan_workspace,
    validate_navigation_candidates,
)
from aha_cli.services.knowledge_retrieval import retrieve_for_task
from aha_cli.store.knowledge import (
    NAVIGATION_SLUG,
    approve_candidate,
    init_knowledge_base,
    list_entries,
    list_pending,
    read_entry,
    write_entry,
)


def _cfg(gate: str = "manual") -> dict:
    kb = default_knowledge_config()
    kb["enabled"] = True
    kb["curation"]["gate"] = gate
    return {"knowledge": kb}


def _make_project(tmp_path: Path) -> Path:
    ws = tmp_path / "demo"
    pkg = ws / "src" / "demo_pkg"
    (pkg / "core").mkdir(parents=True)
    (pkg / "web").mkdir(parents=True)
    (pkg / "core" / "__init__.py").write_text('"""Core engine for demo.\n\nmore."""\n', encoding="utf-8")
    (pkg / "web" / "__init__.py").write_text("# no docstring\n", encoding="utf-8")
    (ws / "src" / "demo_pkg.egg-info").mkdir(parents=True)
    (ws / "README.md").write_text(
        "# Demo\n\n[中文](README.md) | [EN](README.en.md)\n\nDemo does a useful thing for tests.\n",
        encoding="utf-8",
    )
    (ws / "pyproject.toml").write_text(
        "[project.scripts]\ndemo = \"demo_pkg.cli:main\"\n", encoding="utf-8"
    )
    return ws


def _navigation_agent_reply(project_key: str) -> str:
    return json.dumps([
        {
            "kind": "navigation",
            "scope": "project",
            "project_key": project_key,
            "slug": "index",
            "title": "demo 导航入口",
            "body": (
                "## 项目介绍\nDemo is a test project for agents.\n\n"
                "## 如何编译 / 使用\n- `python -m pytest`\n\n"
                "## 注意事项\n- Keep nav concise.\n\n"
                "## 编码规范\n- Follow existing style.\n\n"
                "## 项目结构 / 核心 Nav\n### 模块索引\n- [core](modules/core.md)\n- [web](modules/web.md)\n"
            ),
            "tags": ["navigation", "index"],
            "related_files": ["README.md"],
            "confidence": 0.8,
        },
        {
            "kind": "navigation",
            "scope": "project",
            "project_key": project_key,
            "slug": "modules/core",
            "title": "core",
            "body": "## 模块职责\nCore engine for demo.\n",
            "tags": ["navigation", "module"],
            "related_files": ["src/demo_pkg/core"],
            "confidence": 0.7,
        },
        {
            "kind": "navigation",
            "scope": "project",
            "project_key": project_key,
            "slug": "modules/web",
            "title": "web",
            "body": "## 模块职责\nWeb layer for demo.\n",
            "tags": ["navigation", "module"],
            "related_files": ["src/demo_pkg/web"],
            "confidence": 0.7,
        },
    ])


# --------------------------------------------------------------------------- #
def test_scan_extracts_modules_overview_and_entry_points(tmp_path: Path):
    ws = _make_project(tmp_path)
    scan = scan_workspace(ws)

    # Overview skips the language-switcher link bar and uses real prose.
    assert scan["overview"] == "Demo does a useful thing for tests."
    names = {m["name"]: m for m in scan["modules"]}
    assert "core" in names and "web" in names
    # egg-info / dist metadata is not a project module.
    assert not any(n.endswith(".egg-info") for n in names)
    # Module docstring becomes the role; missing docstring falls back to a stub.
    assert names["core"]["role"] == "Core engine for demo."
    assert "待补充" in names["web"]["role"]
    assert names["core"]["files"] == "src/demo_pkg/core"
    assert any("demo_pkg.cli:main" in ep for ep in scan["entry_points"])


def test_build_navigation_candidate_shape(tmp_path: Path):
    ws = _make_project(tmp_path)
    cand = build_navigation_candidate(ws, "demo-key")
    assert cand["kind"] == "navigation"
    assert cand["slug"] == NAVIGATION_SLUG
    assert cand["meta"]["type"] == "navigation"
    assert cand["meta"]["update_mode"] == "bootstrap"
    assert cand["meta"]["navigation_role"] == "index"
    assert cand["title"].endswith("导航入口")
    for header in ("## 项目介绍", "## 如何编译 / 使用", "## 注意事项", "## 编码规范", "## 项目结构 / 核心 Nav", "### 模块索引", "### 入口"):
        assert header in cand["body"]
    # Index-only candidates are safe to enqueue by themselves: they do not link
    # docs that this batch is not also producing.
    assert "[core](modules/core.md)" not in cand["body"]
    assert "core — Core engine for demo." in cand["body"]
    assert "不要把整个 navigation 全量读入" in cand["body"]

    cands = build_navigation_candidates(ws, "demo-key")
    assert [c["slug"] for c in cands] == ["index", "modules/core", "modules/web"]
    assert "[core](modules/core.md)" in cands[0]["body"]
    assert "## 关键源文件" in cands[1]["body"]
    assert cands[1]["meta"]["update_mode"] == "bootstrap"
    assert cands[1]["meta"]["navigation_role"] == "module"


def test_navigation_bootstrap_prompt_requires_project_readme_and_map(tmp_path: Path):
    ws = _make_project(tmp_path)
    prompt = build_navigation_bootstrap_prompt(
        scan_workspace(ws),
        workspace_path=str(ws),
        project_key_value="demo-key",
    )

    assert "Project navigation is a first-read router" in prompt
    assert "Inspect the workspace in read-only mode" in prompt
    assert "`## 项目介绍`" in prompt
    assert "`## 项目结构 / 核心 Nav`" in prompt
    assert "COMPRESSED WORKSPACE SCAN JSON" not in prompt


def _linked_navigation_slugs(body: str) -> set[str]:
    return {match.group(1)[:-3] for match in re.finditer(r"\[[^\]]+\]\(([^)]+\.md)\)", body)}


def test_bootstrap_project_navigation_has_no_dead_links(tmp_path: Path):
    home = tmp_path / ".aha"
    ws = _make_project(tmp_path)
    cfg = _cfg(gate="manual")
    init_knowledge_base(home, cfg)

    result = bootstrap_project_navigation(
        home, cfg, workspace_path=str(ws), project_key_value="demo-key",
        backend="codex", agent=lambda _context: _navigation_agent_reply("demo-key"),
    )

    assert result["ok"] is True
    assert result["validation"]["ok"] is True
    assert result["event"]["type"] == "knowledge_navigation_bootstrap"
    pending = list_pending(home, cfg)
    by_slug = {item["slug"]: item for item in pending}
    assert set(by_slug) == {"index", "modules/core", "modules/web"}
    assert _linked_navigation_slugs(by_slug["index"]["body"]) == {"modules/core", "modules/web"}


def test_bootstrap_project_navigation_accepts_fenced_json_agent_reply(tmp_path: Path):
    home = tmp_path / ".aha"
    ws = _make_project(tmp_path)
    cfg = _cfg(gate="manual")
    init_knowledge_base(home, cfg)

    reply = "```json\n" + _navigation_agent_reply("demo-key") + "\n```"
    result = bootstrap_project_navigation(
        home, cfg, workspace_path=str(ws), project_key_value="demo-key",
        backend="codex", agent=lambda _context: reply,
    )

    assert result["ok"] is True
    assert result["validation"]["ok"] is True
    assert {item["slug"] for item in list_pending(home, cfg)} == {"index", "modules/core", "modules/web"}


def test_bootstrap_project_navigation_records_agent_reply_excerpt_on_parse_failure(tmp_path: Path):
    home = tmp_path / ".aha"
    ws = _make_project(tmp_path)
    cfg = _cfg(gate="manual")
    init_knowledge_base(home, cfg)

    result = bootstrap_project_navigation(
        home, cfg, workspace_path=str(ws), project_key_value="demo-key",
        backend="codex", agent=lambda _context: "I created a navigation draft, but not JSON.",
    )

    assert result["ok"] is False
    assert result["agent"]["status"] == "invalid"
    assert result["agent"]["reply_excerpt"] == "I created a navigation draft, but not JSON."


def test_generate_enqueues_and_approve_lands_at_navigation_folder(tmp_path: Path):
    home = tmp_path / ".aha"
    ws = _make_project(tmp_path)
    cfg = _cfg(gate="manual")
    init_knowledge_base(home, cfg)

    result = generate_navigation_candidate(
        home, cfg, workspace_path=str(ws), project_key_value="demo-key",
        backend="codex", agent=lambda _context: _navigation_agent_reply("demo-key"),
    )
    assert result["gate"] == "manual"
    assert result["validation"]["ok"] is True
    pending = list_pending(home, cfg)
    assert len(pending) == 3
    assert {p["kind"] for p in pending} == {"navigation"}
    assert {p["slug"] for p in pending} == {"index", "modules/core", "modules/web"}

    for candidate in pending:
        approve_candidate(home, cfg, candidate["id"])
    entries = list_entries(home, config=cfg, scope="project", kind="navigation", project_key_value="demo-key")
    assert len(entries) == 3
    slugs = {entry["meta"]["slug"] for entry in entries}
    paths = {entry["path"] for entry in entries}
    assert slugs == {"index", "modules/core", "modules/web"}
    assert any(path.endswith("navigation/index.md") for path in paths)
    assert any(path.endswith("navigation/modules/core.md") for path in paths)


def test_bootstrap_skips_existing_navigation_index_without_overwrite(tmp_path: Path):
    home = tmp_path / ".aha"
    ws = _make_project(tmp_path)
    cfg = _cfg(gate="manual")
    init_knowledge_base(home, cfg)
    seed_path = write_entry(
        home, config=cfg, scope="project", kind="navigation", project_key_value="demo-key",
        title="现有导航", body="## 模块索引\n- existing\n", slug=NAVIGATION_SLUG,
        meta={"type": "navigation"},
    )

    result = bootstrap_project_navigation(
        home, cfg, workspace_path=str(ws), project_key_value="demo-key"
    )

    assert result["ok"] is True
    assert result["skipped"] == "navigation exists"
    assert list_pending(home, cfg) == []
    assert read_entry(seed_path)["body"] == "## 模块索引\n- existing"


def test_bootstrap_project_nav_disabled_still_allows_explicit_generation(tmp_path: Path):
    home = tmp_path / ".aha"
    ws = _make_project(tmp_path)
    cfg = _cfg(gate="manual")
    cfg["knowledge"]["project_nav"]["enabled"] = False
    init_knowledge_base(home, cfg)

    result = bootstrap_project_navigation(
        home, cfg, workspace_path=str(ws), project_key_value="demo-key",
        backend="codex", agent=lambda _context: _navigation_agent_reply("demo-key"),
    )

    assert result["ok"] is True
    assert result["validation"]["ok"] is True
    assert {item["slug"] for item in list_pending(home, cfg)} == {"index", "modules/core", "modules/web"}


def test_navigation_is_pinned_to_top_of_retrieval(tmp_path: Path):
    home = tmp_path / ".aha"
    cfg = _cfg()
    init_knowledge_base(home, cfg)
    # A solution that matches the query terms, and a map that does NOT.
    write_entry(
        home, config=cfg, scope="project", kind="solutions", project_key_value="k",
        title="zipapp packaging fix", body="bundle the submodule zipapp",
        meta={"type": "solution"},
    )
    write_entry(
        home, config=cfg, scope="project", kind="navigation", project_key_value="k",
        title="项目导航", body="项目定位与模块索引", slug=NAVIGATION_SLUG,
        meta={"type": "navigation"},
    )
    out = retrieve_for_task(home, cfg, project_key="k", terms=["zipapp", "packaging"], max_entries=5)
    assert out[0]["meta"]["type"] == "navigation"  # map pinned first despite zero overlap


def test_navigation_sidecar_writeback_updates_existing_map(tmp_path: Path):
    home = tmp_path / ".aha"
    cfg = _cfg(gate="manual")
    init_knowledge_base(home, cfg)
    # Seed the project navigation index (as if a prior `kb map build` was approved).
    seed_path = write_entry(
        home, config=cfg, scope="project", kind="navigation", project_key_value="k",
        title="demo 项目导航", body="## 模块索引\n- **old**", slug=NAVIGATION_SLUG,
        meta={"type": "navigation"},
    )
    seed_id = read_entry(seed_path)["meta"]["id"]

    # A finalize emits a navigation sidecar that refreshes the map.
    raw = [{
        "kind": "navigation",
        "title": "demo 项目导航",
        "overview": "updated overview",
        "modules": [{"name": "new", "role": "fresh role", "files": ["src/new"]}],
    }]
    cands = normalize_sidecar_candidates({"project_key": "k"}, raw)
    res = distill_and_enqueue(home, cfg, {"project_key": "k"}, candidates=cands)
    assert res["gate"] == "manual"

    pending = list_pending(home, cfg)
    assert len(pending) == 1
    assert pending[0]["kind"] == "navigation"
    assert pending[0]["meta"]["update_mode"] == "incremental"
    assert pending[0]["meta"]["navigation_role"] == "index"
    # The queue recognizes this as an update of the existing map, not a new entry.
    assert pending[0].get("action") == "update"
    assert pending[0].get("updates_entry_id") == seed_id

    approve_candidate(home, cfg, pending[0]["id"])
    entries = list_entries(home, config=cfg, scope="project", kind="navigation", project_key_value="k")
    assert len(entries) == 1  # still exactly one map — updated, not duplicated
    updated = entries[0]
    assert updated["meta"]["id"] == seed_id  # stable id preserved across write-back
    assert updated["meta"]["slug"] == NAVIGATION_SLUG
    assert "## 项目介绍" in updated["body"]
    assert "## 项目结构 / 核心 Nav" in updated["body"]
    assert "new — fresh role" in updated["body"] and "updated overview" in updated["body"]
    assert "[new](modules/new.md)" not in updated["body"]
    assert "**old**" in updated["body"]


def test_sidecar_navigation_kind_is_normalized(tmp_path: Path):
    raw = [{
        "kind": "navigation",
        "title": "项目导航",
        "overview": "what it is",
        "architecture": "layered",
        "modules": [{"name": "store", "role": "filesystem store", "files": ["src/store"]}],
        "entry_points": ["aha = cli:main"],
    }]
    out = normalize_sidecar_candidates({"project_key": "k"}, raw)
    assert len(out) == 1
    assert out[0]["kind"] == "navigation"
    assert out[0]["slug"] == NAVIGATION_SLUG
    assert out[0]["meta"]["type"] == "navigation"
    assert "store — filesystem store" in out[0]["body"]
    assert "[store](modules/store.md)" not in out[0]["body"]
    assert "## 项目介绍" in out[0]["body"]
    assert "## 项目结构 / 核心 Nav" in out[0]["body"]
    assert "### 模块索引" in out[0]["body"]


def test_readonly_diagnostic_navigation_sidecar_keeps_troubleshooting_path():
    raw = [{
        "kind": "navigation",
        "title": "AHA 微信通知模块导航",
        "slug": "modules/weixin-notifications",
        "responsibility": "负责微信配对状态、通知开关和主动推送前的会话上下文检查。",
        "related_files": [
            "src/aha_cli/services/weixin.py",
            "src/aha_cli/services/weixin_notifications.py",
            "src/aha_cli/web/system_routes.py",
        ],
        "entry_points": ["/api/weixin", "/api/weixin/notifications"],
        "diagnostic_paths": [
            "已配对但收不到消息时先看 notification_status.ready 和 send_context.state。",
        ],
        "navigation_reason": "read-only diagnostic discovered the reusable Weixin notification path",
    }]
    out = normalize_sidecar_candidates({"project_key": "k"}, raw)

    assert len(out) == 1
    cand = out[0]
    assert cand["kind"] == "navigation"
    assert cand["slug"] == "modules/weixin-notifications"
    assert "## 常用排查路径" in cand["body"]
    assert "notification_status.ready" in cand["body"]
    assert cand["meta"]["navigation_reason"].startswith("read-only diagnostic")
    assert cand["meta"]["diagnostic_paths"]


def test_project_nav_disabled_filters_navigation_candidates(tmp_path: Path):
    home = tmp_path / ".aha"
    cfg = _cfg(gate="manual")
    cfg["knowledge"]["project_nav"]["enabled"] = False
    init_knowledge_base(home, cfg)
    cands = normalize_sidecar_candidates(
        {"project_key": "k"},
        [{"kind": "navigation", "title": "Web 模块导航", "slug": "modules/web", "responsibility": "HTTP API"}],
    )

    result = distill_and_enqueue(home, cfg, {"project_key": "k"}, candidates=cands)

    assert result["candidates"] == 0
    assert result["navigation"]["skipped"]["reason"] == "project navigation disabled"
    assert navigation_delta_event_payload(result, source_type="task_final")["navigation"]["skipped"]["candidates"] == 1
    assert list_pending(home, cfg) == []


def test_sidecar_project_wiki_is_navigation_module_doc():
    raw = [{
        "kind": "wiki",
        "scope": "project",
        "title": "store 模块约束",
        "body": "项目内稳定事实",
        "related_files": ["src/store.py"],
    }]
    out = normalize_sidecar_candidates({"project_key": "k"}, raw)
    assert out[0]["kind"] == "navigation"
    assert out[0]["scope"] == "project"
    assert out[0]["slug"] == "modules/store"
    assert out[0]["meta"]["update_mode"] == "incremental"
    assert out[0]["meta"]["navigation_role"] == "module"


def test_nested_navigation_candidate_backfills_parent_chain(tmp_path: Path):
    home = tmp_path / ".aha"
    cfg = _cfg(gate="manual")
    init_knowledge_base(home, cfg)
    write_entry(
        home, config=cfg, scope="project", kind="navigation", project_key_value="k",
        title="项目导航", body="## 模块索引\n", slug="index",
        meta={"type": "navigation"},
    )
    raw = [{
        "kind": "navigation",
        "scope": "project",
        "title": "web routes 子文档",
        "slug": "modules/web/routes",
        "body": "routes detail",
    }]
    cands = normalize_sidecar_candidates({"project_key": "k"}, raw)
    result = distill_and_enqueue(home, cfg, {"project_key": "k"}, candidates=cands)
    assert result["candidates"] == 3
    assert result["validation"]["ok"] is True

    pending = list_pending(home, cfg)
    by_slug = {item["slug"]: item for item in pending}
    assert set(by_slug) == {"modules/web/routes", "modules/web", "index"}
    assert "modules/web/routes.md" in by_slug["modules/web"]["body"]
    assert "modules/web.md" in by_slug["index"]["body"]
    assert "modules/web/routes.md" not in by_slug["index"]["body"]


def test_missing_index_skips_navigation_delta_even_with_workspace(tmp_path: Path):
    home = tmp_path / ".aha"
    ws = _make_project(tmp_path)
    cfg = _cfg(gate="manual")
    init_knowledge_base(home, cfg)
    raw = [{
        "kind": "navigation",
        "scope": "project",
        "title": "web routes 子文档",
        "slug": "modules/web/routes",
        "body": "routes detail",
    }]
    cands = normalize_sidecar_candidates({"project_key": "k"}, raw)
    result = distill_and_enqueue(
        home, cfg, {"project_key": "k", "workspace_path": str(ws)}, candidates=cands
    )
    assert result["candidates"] == 0
    assert result["navigation"]["skipped"]["reason"] == "project navigation index missing"
    assert list_pending(home, cfg) == []


def test_nested_navigation_backfills_only_direct_existing_parent(tmp_path: Path):
    home = tmp_path / ".aha"
    cfg = _cfg(gate="manual")
    init_knowledge_base(home, cfg)
    write_entry(
        home, config=cfg, scope="project", kind="navigation", project_key_value="k",
        title="项目导航", body="## 模块索引\n- [web](modules/web.md)\n",
        slug=NAVIGATION_SLUG, meta={"type": "navigation"},
    )
    parent_path = write_entry(
        home, config=cfg, scope="project", kind="navigation", project_key_value="k",
        title="web 模块", body="## 下级入口\n-",
        slug="modules/web", meta={"type": "navigation"},
    )
    parent_id = read_entry(parent_path)["meta"]["id"]
    raw = [{
        "kind": "navigation",
        "scope": "project",
        "title": "web static 子文档",
        "slug": "modules/web/static",
        "body": "static detail",
    }]
    cands = normalize_sidecar_candidates({"project_key": "k"}, raw)
    result = distill_and_enqueue(home, cfg, {"project_key": "k"}, candidates=cands)
    assert result["candidates"] == 2

    pending = list_pending(home, cfg)
    by_slug = {item["slug"]: item for item in pending}
    assert set(by_slug) == {"modules/web/static", "modules/web"}
    assert by_slug["modules/web"]["action"] == "update"
    assert by_slug["modules/web"]["updates_entry_id"] == parent_id
    assert "modules/web/static.md" in by_slug["modules/web"]["body"]


def test_navigation_validation_rejects_broken_link(tmp_path: Path):
    home = tmp_path / ".aha"
    cfg = _cfg(gate="manual")
    init_knowledge_base(home, cfg)
    candidates = [{
        "kind": "navigation",
        "scope": "project",
        "project_key": "k",
        "slug": NAVIGATION_SLUG,
        "title": "bad index",
        "body": "## 模块索引\n- [missing](modules/missing.md)\n",
    }]

    validation = validate_navigation_candidates(home, cfg, candidates)

    assert validation["ok"] is False
    assert validation["errors"][0]["code"] == "broken_link"
    assert validation["errors"][0]["target_slug"] == "modules/missing"


def test_navigation_validation_rejects_invalid_slug(tmp_path: Path):
    home = tmp_path / ".aha"
    cfg = _cfg(gate="manual")
    init_knowledge_base(home, cfg)
    candidates = [{
        "kind": "navigation",
        "scope": "project",
        "project_key": "k",
        "slug": "modules/Web Routes",
        "title": "bad slug",
        "body": "detail",
    }]

    validation = validate_navigation_candidates(home, cfg, candidates)

    assert validation["ok"] is False
    assert validation["errors"][0]["code"] == "invalid_slug"
    assert validation["errors"][0]["normalized_slug"] == "modules/web-routes"
