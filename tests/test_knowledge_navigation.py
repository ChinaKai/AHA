from __future__ import annotations

from pathlib import Path

from aha_cli.domain.models import default_knowledge_config
from aha_cli.services.knowledge_distill import (
    distill_and_enqueue,
    normalize_sidecar_candidates,
)
from aha_cli.services.knowledge_navigation import (
    build_navigation_candidate,
    build_navigation_candidates,
    generate_navigation_candidate,
    scan_workspace,
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
    for header in ("## 项目定位", "## 架构概览", "## 模块索引", "## 入口"):
        assert header in cand["body"]
    assert "[core](modules/core.md)" in cand["body"]
    assert "不要把整个 navigation 全量读入" in cand["body"]

    cands = build_navigation_candidates(ws, "demo-key")
    assert [c["slug"] for c in cands] == ["index", "modules/core", "modules/web"]
    assert "## 关键源文件" in cands[1]["body"]
    assert cands[1]["meta"]["update_mode"] == "bootstrap"
    assert cands[1]["meta"]["navigation_role"] == "module"


def test_generate_enqueues_and_approve_lands_at_navigation_folder(tmp_path: Path):
    home = tmp_path / ".aha"
    ws = _make_project(tmp_path)
    cfg = _cfg(gate="manual")
    init_knowledge_base(home, cfg)

    result = generate_navigation_candidate(
        home, cfg, workspace_path=str(ws), project_key_value="demo-key"
    )
    assert result["gate"] == "manual"
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
    assert "[new](modules/new.md)" in updated["body"] and "updated overview" in updated["body"]
    assert "**old**" not in updated["body"]


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
    assert "[store](modules/store.md)" in out[0]["body"]
    assert "## 模块索引" in out[0]["body"]


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

    pending = list_pending(home, cfg)
    by_slug = {item["slug"]: item for item in pending}
    assert set(by_slug) == {"modules/web/routes", "modules/web", "index"}
    assert "modules/web/routes.md" in by_slug["modules/web"]["body"]
    assert "modules/web.md" in by_slug["index"]["body"]
    assert "modules/web/routes.md" not in by_slug["index"]["body"]


def test_missing_index_uses_workspace_bootstrap_when_available(tmp_path: Path):
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
    assert result["candidates"] == 3

    index = next(item for item in list_pending(home, cfg) if item["slug"] == "index")
    assert index["meta"]["update_mode"] == "bootstrap"
    assert "Demo does a useful thing for tests." in index["body"]
    assert "[core](modules/core.md)" in index["body"]
    assert "[web](modules/web.md)" in index["body"]
    assert "modules/web/routes.md" not in index["body"]


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
