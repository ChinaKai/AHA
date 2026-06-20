from __future__ import annotations

from pathlib import Path

from aha_cli.domain.models import default_knowledge_config
from aha_cli.services.knowledge_distill import (
    distill_and_enqueue,
    normalize_sidecar_candidates,
)
from aha_cli.services.knowledge_navigation import (
    build_navigation_candidate,
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
    assert cand["title"].endswith("项目地图")
    for header in ("## 项目定位", "## 架构概览", "## 模块索引", "## 入口"):
        assert header in cand["body"]
    assert "**core**" in cand["body"]


def test_generate_enqueues_and_approve_lands_at_map_slug(tmp_path: Path):
    home = tmp_path / ".aha"
    ws = _make_project(tmp_path)
    cfg = _cfg(gate="manual")
    init_knowledge_base(home, cfg)

    result = generate_navigation_candidate(
        home, cfg, workspace_path=str(ws), project_key_value="demo-key"
    )
    assert result["gate"] == "manual"
    pending = list_pending(home, cfg)
    assert len(pending) == 1
    assert pending[0]["kind"] == "navigation"

    approve_candidate(home, cfg, pending[0]["id"])
    entries = list_entries(home, config=cfg, scope="project", kind="navigation", project_key_value="demo-key")
    assert len(entries) == 1
    assert entries[0]["meta"]["slug"] == NAVIGATION_SLUG
    assert entries[0]["meta"]["type"] == "navigation"
    assert entries[0]["path"].endswith(f"navigation/{NAVIGATION_SLUG}.md")


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
        title="项目地图", body="项目定位与模块索引", slug=NAVIGATION_SLUG,
        meta={"type": "navigation"},
    )
    out = retrieve_for_task(home, cfg, project_key="k", terms=["zipapp", "packaging"], max_entries=5)
    assert out[0]["meta"]["type"] == "navigation"  # map pinned first despite zero overlap


def test_navigation_sidecar_writeback_updates_existing_map(tmp_path: Path):
    home = tmp_path / ".aha"
    cfg = _cfg(gate="manual")
    init_knowledge_base(home, cfg)
    # Seed the project map (as if a prior `kb map build` was approved).
    seed_path = write_entry(
        home, config=cfg, scope="project", kind="navigation", project_key_value="k",
        title="demo 项目地图", body="## 模块索引\n- **old**", slug=NAVIGATION_SLUG,
        meta={"type": "navigation"},
    )
    seed_id = read_entry(seed_path)["meta"]["id"]

    # A finalize emits a navigation sidecar that refreshes the map.
    raw = [{
        "kind": "navigation",
        "title": "demo 项目地图",
        "overview": "updated overview",
        "modules": [{"name": "new", "role": "fresh role", "files": ["src/new"]}],
    }]
    cands = normalize_sidecar_candidates({"project_key": "k"}, raw)
    res = distill_and_enqueue(home, cfg, {"project_key": "k"}, candidates=cands)
    assert res["gate"] == "manual"

    pending = list_pending(home, cfg)
    assert len(pending) == 1
    assert pending[0]["kind"] == "navigation"
    # The queue recognizes this as an update of the existing map, not a new entry.
    assert pending[0].get("action") == "update"
    assert pending[0].get("updates_entry_id") == seed_id

    approve_candidate(home, cfg, pending[0]["id"])
    entries = list_entries(home, config=cfg, scope="project", kind="navigation", project_key_value="k")
    assert len(entries) == 1  # still exactly one map — updated, not duplicated
    updated = entries[0]
    assert updated["meta"]["id"] == seed_id  # stable id preserved across write-back
    assert updated["meta"]["slug"] == NAVIGATION_SLUG
    assert "**new**" in updated["body"] and "updated overview" in updated["body"]
    assert "**old**" not in updated["body"]


def test_sidecar_navigation_kind_is_normalized(tmp_path: Path):
    raw = [{
        "kind": "navigation",
        "title": "项目地图",
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
    assert "**store**" in out[0]["body"]
    assert "## 模块索引" in out[0]["body"]
