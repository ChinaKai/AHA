from __future__ import annotations

import asyncio
import json
from pathlib import Path

from aha_cli.domain.models import default_knowledge_config
from aha_cli.store.config import load_config
from aha_cli.store import knowledge_nav_drafts as nav_drafts
from aha_cli.store.filesystem import create_plan
from aha_cli.store.io import read_json, write_json
from aha_cli.store.knowledge import enqueue_candidate, init_knowledge_base, list_entries, list_pending, project_key, write_entry
from aha_cli.store.paths import config_path
from aha_cli.web.knowledge_routes import knowledge_route_response
from tests.helpers import fetch_ui_response, json_response_body


def _setup(tmp_path: Path) -> Path:
    home = tmp_path / ".aha"
    kb = default_knowledge_config()
    kb["enabled"] = True
    write_json(config_path(home), {"knowledge": kb})
    init_knowledge_base(home, {"knowledge": kb})
    return home


def _get(home: Path, path: str, query=None):
    return json_response_body(knowledge_route_response(home, "GET", path, query or {}, b"", {}))


def _post(home: Path, path: str, payload: dict):
    return knowledge_route_response(home, "POST", path, {}, json.dumps(payload).encode(), {})


def _patch(home: Path, path: str, payload: dict):
    return knowledge_route_response(home, "PATCH", path, {}, json.dumps(payload).encode(), {})


def _delete(home: Path, path: str, payload: dict):
    return knowledge_route_response(home, "DELETE", path, {}, json.dumps(payload).encode(), {})


def _sync_project_nav_jobs(monkeypatch) -> None:
    import aha_cli.web.knowledge_routes as kr

    def sync_dispatch(root, cfg, draft_id, **kwargs):
        return kr.run_project_nav_draft_job(root, cfg, draft_id, **kwargs)

    monkeypatch.setattr(kr, "dispatch_project_nav_job", sync_dispatch)


def test_unknown_path_returns_none(tmp_path: Path):
    assert knowledge_route_response(tmp_path / ".aha", "GET", "/api/other", {}, b"", {}) is None


def test_status_and_entries(tmp_path: Path):
    home = _setup(tmp_path)
    cfg = load_config(home)
    write_entry(home, config=cfg, scope="project", kind="solutions",
                project_key_value="git-abc", title="Fix build", body="do x", meta={"tags": ["ci"]})
    write_entry(home, config=cfg, scope="general", kind="wiki", title="Overview", body="...")

    status = _get(home, "/api/kb/status")
    assert status["total_entries"] == 2

    all_entries = _get(home, "/api/kb/entries")
    assert all_entries["count"] == 2

    filtered = _get(home, "/api/kb/entries", {"scope": ["general"]})
    assert filtered["count"] == 1 and filtered["entries"][0]["title"] == "Overview"

    by_kind = _get(home, "/api/kb/entries", {"kind": ["solutions"]})
    assert by_kind["count"] == 1 and by_kind["entries"][0]["id"].startswith("kb_")


def test_entries_kind_filter_includes_navigation(tmp_path: Path):
    home = _setup(tmp_path)
    cfg = load_config(home)
    write_entry(home, config=cfg, scope="project", kind="solutions",
                project_key_value="git-abc", title="Fix build", body="do x")
    write_entry(home, config=cfg, scope="project", kind="navigation",
                project_key_value="git-abc", title="demo 项目导航", body="## 模块索引\n- [store](modules/store.md)", slug="index")
    write_entry(home, config=cfg, scope="general", kind="wiki", title="Docker 教程", body="containers")

    # Project navigation is no longer mixed into ordinary entries.
    nav = _get(home, "/api/kb/entries", {"kind": ["navigation"]})
    assert nav["count"] == 0

    project_nav = _get(home, "/api/kb/project-nav", {"project_key": ["git-abc"]})
    assert project_nav["count"] == 1 and project_nav["entries"][0]["type"] == "navigation"

    # The general tutorial is reachable via the general scope filter.
    general = _get(home, "/api/kb/entries", {"scope": ["general"]})
    assert general["count"] == 1 and general["entries"][0]["title"] == "Docker 教程"

    # Existing kind filters are unaffected (navigation is not a solution).
    sol = _get(home, "/api/kb/entries", {"kind": ["solutions"]})
    assert sol["count"] == 1 and sol["entries"][0]["title"] == "Fix build"


def _capture_sidecar(candidates_json: str) -> str:
    return f"done\n<aha_knowledge_candidates>{candidates_json}</aha_knowledge_candidates>"


def test_capture_api_crud_and_distill(tmp_path: Path, monkeypatch):
    import aha_cli.web.knowledge_routes as kr
    from aha_cli.services.knowledge_capture_distill import run_distill_job

    home = _setup(tmp_path)
    created = json_response_body(_post(home, "/api/kb/capture", {"text": "raw retry idea", "scope_hint": "personal"}))
    nid = created["note"]["id"]
    assert created["note"]["status"] == "raw"

    assert _get(home, "/api/kb/capture")["count"] == 1
    assert _get(home, "/api/kb/capture", {"id": [nid]})["status"] == "raw"

    edited = json_response_body(_patch(home, "/api/kb/capture", {"id": nid, "title": "retry note"}))
    assert edited["note"]["title"] == "retry note"

    # Distill runs synchronously through the seam with a stub agent.
    reply = _capture_sidecar('[{"kind":"wiki","title":"退避","body":"## 结论\\n用指数退避"}]')

    def sync_dispatch(root, cfg, note_id, backend, model, proxy_enabled=None):
        run_distill_job(root, cfg, note_id, proxy_enabled=proxy_enabled, agent=lambda ctx: reply)

    monkeypatch.setattr(kr, "dispatch_distill_job", sync_dispatch)
    resp = json_response_body(_post(home, "/api/kb/capture/distill", {"id": nid}))
    assert resp["status"] == "distilling"

    note = _get(home, "/api/kb/capture", {"id": [nid]})
    assert note["status"] == "distilled" and note["candidate_ids"]
    assert _get(home, "/api/kb/pending")["count"] == 1
    log = _get(home, "/api/kb/capture/distill-log", {"id": [nid]})["log"]
    assert log["status"] == "distilled"
    assert "raw retry idea" in log["prompt"]
    assert "aha_knowledge_candidates" in log["reply"]
    assert log["candidate_ids"] == note["candidate_ids"]

    json_response_body(_delete(home, "/api/kb/capture", {"id": nid}))
    assert _get(home, "/api/kb/capture")["count"] == 0


def test_capture_search_and_relationship_refs(tmp_path: Path, monkeypatch):
    import aha_cli.web.knowledge_routes as kr
    from aha_cli.services.knowledge_capture_distill import run_distill_job

    home = _setup(tmp_path)
    created = json_response_body(_post(home, "/api/kb/capture", {
        "text": "raw retry idea",
        "title": "retry note",
        "scope_hint": "personal",
    }))
    nid = created["note"]["id"]
    reply = _capture_sidecar('[{"kind":"wiki","title":"退避策略","body":"## 结论\\n用指数退避"}]')

    def sync_dispatch(root, cfg, note_id, backend, model, proxy_enabled=None):
        run_distill_job(root, cfg, note_id, proxy_enabled=proxy_enabled, agent=lambda ctx: reply)

    monkeypatch.setattr(kr, "dispatch_distill_job", sync_dispatch)
    json_response_body(_post(home, "/api/kb/capture/distill", {"id": nid}))

    note = _get(home, "/api/kb/capture", {"id": [nid]})
    assert note["candidate_refs"][0]["title"] == "退避策略"
    assert note["entry_refs"] == []
    searched = _get(home, "/api/kb/entries", {"q": ["退避"]})
    assert searched["count"] == 0
    assert searched["capture_notes"][0]["id"] == nid

    pending = _get(home, "/api/kb/pending")["pending"][0]
    assert pending["source_note_id"] == nid
    assert pending["source_note"]["title"] == "retry note"

    approved = json_response_body(_post(home, "/api/kb/approve", {"candidate_id": pending["id"]}))
    assert approved["ok"] is True
    assert approved["source_note"]["source_note_deleted"] is True

    note_after = knowledge_route_response(home, "GET", "/api/kb/capture", {"id": [nid]}, b"", {})
    assert b"404" in note_after.split(b"\r\n", 1)[0]

    entries = _get(home, "/api/kb/entries", {"q": ["退避"]})
    assert entries["entries"][0]["source_note_id"] == nid
    assert entries["entries"][0]["source_note_exists"] is False
    assert entries["capture_notes"] == []

    json_response_body(_delete(home, "/api/kb/entry", {"id": entries["entries"][0]["id"]}))
    entries_after_delete = _get(home, "/api/kb/entries", {"q": ["退避"]})
    assert entries_after_delete["entries"] == []
    assert entries_after_delete["capture_notes"] == []


def test_capture_reject_preserves_source_note(tmp_path: Path, monkeypatch):
    import aha_cli.web.knowledge_routes as kr
    from aha_cli.services.knowledge_capture_distill import run_distill_job

    home = _setup(tmp_path)
    created = json_response_body(_post(home, "/api/kb/capture", {
        "text": "raw retry idea",
        "title": "retry note",
        "scope_hint": "personal",
    }))
    nid = created["note"]["id"]
    reply = _capture_sidecar('[{"kind":"wiki","title":"退避策略","body":"## 结论\\n用指数退避"}]')

    def sync_dispatch(root, cfg, note_id, backend, model, proxy_enabled=None):
        run_distill_job(root, cfg, note_id, proxy_enabled=proxy_enabled, agent=lambda ctx: reply)

    monkeypatch.setattr(kr, "dispatch_distill_job", sync_dispatch)
    json_response_body(_post(home, "/api/kb/capture/distill", {"id": nid}))
    pending = _get(home, "/api/kb/pending")["pending"][0]

    rejected = json_response_body(_post(home, "/api/kb/reject", {"candidate_id": pending["id"]}))
    assert rejected["ok"] is True
    assert rejected["source_note"]["source_note_kept"] is True
    assert rejected["source_note"]["status"] == "raw"

    note = _get(home, "/api/kb/capture", {"id": [nid]})
    assert note["text"] == "raw retry idea"
    assert note["status"] == "raw"
    assert note["candidate_ids"] == []
    assert note["candidate_refs"] == []
    assert note["entry_refs"] == []


def test_capture_approve_deletes_source_note_after_last_candidate(tmp_path: Path, monkeypatch):
    import aha_cli.web.knowledge_routes as kr
    from aha_cli.services.knowledge_capture_distill import run_distill_job

    home = _setup(tmp_path)
    created = json_response_body(_post(home, "/api/kb/capture", {
        "text": "raw retry and timeout ideas",
        "scope_hint": "personal",
    }))
    nid = created["note"]["id"]
    reply = _capture_sidecar(
        "["
        '{"kind":"wiki","title":"退避策略","body":"## 结论\\n用指数退避"},'
        '{"kind":"wiki","title":"超时策略","body":"## 结论\\n设置上限"}'
        "]"
    )

    def sync_dispatch(root, cfg, note_id, backend, model, proxy_enabled=None):
        run_distill_job(root, cfg, note_id, proxy_enabled=proxy_enabled, agent=lambda ctx: reply)

    monkeypatch.setattr(kr, "dispatch_distill_job", sync_dispatch)
    json_response_body(_post(home, "/api/kb/capture/distill", {"id": nid}))
    pending = _get(home, "/api/kb/pending")["pending"]
    assert len(pending) == 2

    first = json_response_body(_post(home, "/api/kb/approve", {"candidate_id": pending[0]["id"]}))
    assert first["source_note"]["source_note_deleted"] is False
    assert len(first["source_note"]["candidate_ids"]) == 1
    note = _get(home, "/api/kb/capture", {"id": [nid]})
    assert len(note["candidate_refs"]) == 1

    second_id = first["source_note"]["candidate_ids"][0]
    second = json_response_body(_post(home, "/api/kb/approve", {"candidate_id": second_id}))
    assert second["source_note"]["source_note_deleted"] is True
    missing = knowledge_route_response(home, "GET", "/api/kb/capture", {"id": [nid]}, b"", {})
    assert b"404" in missing.split(b"\r\n", 1)[0]


def test_capture_image_upload_serve_and_delete(tmp_path: Path):
    import base64

    home = _setup(tmp_path)
    created = json_response_body(_post(home, "/api/kb/capture", {"text": "with image"}))
    nid = created["note"]["id"]
    png = b"\x89PNG\r\n\x1a\n" + b"body"
    data_url = "data:image/png;base64," + base64.b64encode(png).decode()

    up = json_response_body(_post(home, "/api/kb/capture/image", {"id": nid, "filename": "a.png", "data_url": data_url}))
    assert up["image"]["mime"] == "image/png"
    name = up["image"]["name"]

    # The note now carries the image metadata (no base64 in the JSON).
    note = _get(home, "/api/kb/capture", {"id": [nid]})
    assert len(note["images"]) == 1

    # The image is served as raw bytes.
    raw = knowledge_route_response(home, "GET", "/api/kb/capture/image", {"id": [nid], "name": [name]}, b"", {})
    assert b"\r\n\r\n" in raw and raw.split(b"\r\n\r\n", 1)[1] == png

    deleted = json_response_body(
        knowledge_route_response(home, "DELETE", "/api/kb/capture/image", {}, json.dumps({"id": nid, "name": name}).encode(), {})
    )
    assert deleted["ok"] is True
    assert _get(home, "/api/kb/capture", {"id": [nid]})["images"] == []


def test_capture_image_rejects_non_image(tmp_path: Path):
    import base64

    home = _setup(tmp_path)
    nid = json_response_body(_post(home, "/api/kb/capture", {"text": "x"}))["note"]["id"]
    bad = "data:image/png;base64," + base64.b64encode(b"not an image").decode()
    body = json_response_body(_post(home, "/api/kb/capture/image", {"id": nid, "data_url": bad}))
    assert "unsupported image type" in body["error"]


def test_capture_distill_forwards_backend_model_and_proxy(tmp_path: Path, monkeypatch):
    import aha_cli.web.knowledge_routes as kr

    home = _setup(tmp_path)
    nid = json_response_body(_post(home, "/api/kb/capture", {"text": "raw"}))["note"]["id"]
    seen = {}

    def record_dispatch(root, cfg, note_id, backend, model, proxy_enabled=None):
        seen.update({"note_id": note_id, "backend": backend, "model": model, "proxy_enabled": proxy_enabled})

    monkeypatch.setattr(kr, "dispatch_distill_job", record_dispatch)
    json_response_body(_post(home, "/api/kb/capture/distill", {"id": nid, "backend": "claude", "model": "claude-opus-4-8", "proxy_enabled": False}))
    assert seen == {"note_id": nid, "backend": "claude", "model": "claude-opus-4-8", "proxy_enabled": False}


def test_capture_distill_missing_note_is_404(tmp_path: Path):
    home = _setup(tmp_path)
    resp = knowledge_route_response(
        home, "POST", "/api/kb/capture/distill", {}, json.dumps({"id": "cap_nope"}).encode(), {}
    )
    body = json_response_body(resp)
    assert "not found" in body["error"]


def test_capture_create_requires_text(tmp_path: Path):
    home = _setup(tmp_path)
    body = json_response_body(_post(home, "/api/kb/capture", {"text": "   "}))
    assert "required" in body["error"]


def test_pending_lists_navigation_and_general_candidates(tmp_path: Path):
    home = _setup(tmp_path)
    cfg = load_config(home)
    enqueue_candidate(home, cfg, {
        "kind": "navigation", "scope": "project", "project_key": "git-abc",
        "slug": "index", "title": "demo 项目导航", "body": "## 模块索引",
        "meta": {"type": "navigation", "confidence": 0.4},
    })
    enqueue_candidate(home, cfg, {
        "kind": "wiki", "scope": "general", "project_key": None,
        "title": "Git 教程", "body": "rebase",
        "meta": {"type": "wiki", "confidence": 0.5},
    })
    listed = _get(home, "/api/kb/pending")
    assert listed["count"] == 2
    kinds = {c["kind"] for c in listed["pending"]}
    assert kinds == {"navigation", "wiki"}
    # The general candidate is unbound from any project.
    general = next(c for c in listed["pending"] if c["kind"] == "wiki")
    assert general["scope"] == "general" and general["project_key"] is None


def test_entries_support_fuzzy_project_filter_and_search(tmp_path: Path):
    home = _setup(tmp_path)
    cfg = load_config(home)
    write_entry(home, config=cfg, scope="project", kind="solutions",
                project_key_value="aha-git-abc123", title="Fix build", body="restart the service", meta={"tags": ["deploy"]})
    write_entry(home, config=cfg, scope="project", kind="solutions",
                project_key_value="docs-git-def456", title="Write docs", body="update README", meta={"tags": ["docs"]})

    by_project = _get(home, "/api/kb/entries", {"project": ["aha"]})
    assert by_project["count"] == 1
    assert by_project["entries"][0]["project_key"] == "aha-git-abc123"

    by_body = _get(home, "/api/kb/entries", {"q": ["service"]})
    assert by_body["count"] == 1
    assert by_body["entries"][0]["title"] == "Fix build"

    by_tag = _get(home, "/api/kb/entries", {"q": ["docs"]})
    assert by_tag["count"] == 1
    assert by_tag["entries"][0]["title"] == "Write docs"


def test_entries_exclude_navigation_but_project_nav_api_lists_entry_points_and_can_read_children(tmp_path: Path):
    home = _setup(tmp_path)
    cfg = load_config(home)
    write_entry(
        home, config=cfg, scope="project", kind="navigation", project_key_value="git-abc",
        title="项目导航", body="- [store](modules/store.md)", slug="index",
        meta={"type": "navigation"},
    )
    write_entry(
        home, config=cfg, scope="project", kind="navigation", project_key_value="git-abc",
        title="store 模块", body="store details", slug="modules/store",
        meta={"type": "navigation"},
    )

    listed = _get(home, "/api/kb/entries", {"kind": ["navigation"]})
    assert listed["count"] == 0

    nav = _get(home, "/api/kb/project-nav", {"project_key": ["git-abc"]})
    assert nav["count"] == 1
    assert [entry["slug"] for entry in nav["entries"]] == ["index"]

    child = _get(home, "/api/kb/entry", {"id": ["modules/store"]})
    assert child["meta"]["slug"] == "modules/store"
    assert child["body"] == "store details"

    updated = json_response_body(_patch(home, "/api/kb/entry", {
        "id": "modules/store",
        "title": "store 模块导航",
        "body": "updated store details",
    }))
    assert updated["entry"]["meta"]["slug"] == "modules/store"
    assert updated["entry"]["meta"]["title"] == "store 模块导航"
    assert updated["entry"]["body"] == "updated store details"


def _write_project_nav_tree(home: Path, cfg: dict, project_key: str = "git-abc") -> list[str]:
    paths = [
        write_entry(
            home, config=cfg, scope="project", kind="navigation", project_key_value=project_key,
            title="项目导航", body="- [store](modules/store.md)\n- [task](flows/task.md)",
            slug="index", meta={"type": "navigation"},
        ),
        write_entry(
            home, config=cfg, scope="project", kind="navigation", project_key_value=project_key,
            title="store 模块", body="store details", slug="modules/store",
            meta={"type": "navigation"},
        ),
        write_entry(
            home, config=cfg, scope="project", kind="navigation", project_key_value=project_key,
            title="task 流程", body="task flow", slug="flows/task",
            meta={"type": "navigation"},
        ),
    ]
    return [str(path) for path in paths]


def _make_nav_workspace(tmp_path: Path) -> Path:
    ws = tmp_path / "nav-demo"
    pkg = ws / "src" / "nav_demo"
    (pkg / "core").mkdir(parents=True)
    (pkg / "core" / "__init__.py").write_text('"""Core nav module."""\n', encoding="utf-8")
    (ws / "README.md").write_text("# Nav Demo\n\nReusable nav demo.\n", encoding="utf-8")
    return ws


def _project_nav_agent_reply(project_key: str) -> str:
    return json.dumps([
        {
            "kind": "navigation",
            "scope": "project",
            "project_key": project_key,
            "slug": "index",
            "title": "Agent nav",
            "body": (
                "## 项目介绍\nNav Demo is a generated project briefing for agents.\n\n"
                "## 如何编译 / 使用\n- `python -m pytest`\n\n"
                "## 注意事项\n- Keep nav concise.\n\n"
                "## 编码规范\n- Follow existing style.\n\n"
                "## 项目结构 / 核心 Nav\n### 模块索引\n- [Core](modules/core.md)\n"
            ),
            "tags": ["navigation", "index"],
            "related_files": ["src/nav_demo/core"],
            "confidence": 0.8,
        },
        {
            "kind": "navigation",
            "scope": "project",
            "project_key": project_key,
            "slug": "modules/core",
            "title": "Core",
            "body": "## 模块职责\nagent generated core map\n",
            "tags": ["navigation", "module"],
            "related_files": ["src/nav_demo/core"],
            "confidence": 0.7,
        },
    ])


def _stub_project_nav_agent(monkeypatch):
    import aha_cli.web.knowledge_routes as kr

    def agent(context: dict) -> str:
        return _project_nav_agent_reply(str(context.get("project_key") or "demo-key"))

    monkeypatch.setattr(kr, "project_navigation_agent", agent)


def test_project_nav_generate_creates_completed_draft_without_pending_candidates(tmp_path: Path, monkeypatch):
    _sync_project_nav_jobs(monkeypatch)
    _stub_project_nav_agent(monkeypatch)
    home = _setup(tmp_path)
    ws = _make_nav_workspace(tmp_path)

    result = json_response_body(_post(home, "/api/kb/project-nav", {
        "workspace_path": str(ws),
        "project_key": "demo-key",
        "backend": "codex",
        "model": "gpt-test",
        "proxy_enabled": False,
    }))

    assert result["ok"] is True
    assert result["status"] == "running"
    assert result["draft_id"]
    assert result["project_key"] == "demo-key"
    drafts = _get(home, "/api/kb/project-nav/drafts", {"project_key": ["demo-key"]})["drafts"]
    assert drafts[0]["id"] == result["draft_id"]
    assert drafts[0]["status"] == "completed"
    assert drafts[0]["backend"] == "codex"
    assert drafts[0]["model"] == "gpt-test"
    assert drafts[0]["proxy_enabled"] is False
    assert "candidates" not in drafts[0]
    assert drafts[0]["validation"]["ok"] is True
    assert [item["stage"] for item in drafts[0]["agent_log"]] == ["queued", "running", "completed"]
    assert drafts[0]["summary"] == "2 navigation entries ready for review"
    detail = _get(home, "/api/kb/project-nav/draft", {"id": [result["draft_id"]]})["draft"]
    assert {item["slug"] for item in detail["candidates"]} == {"index", "modules/core"}
    index_candidate = next(item for item in detail["candidates"] if item["slug"] == "index")
    assert "## 项目介绍" in index_candidate["body"]
    assert "## 项目结构 / 核心 Nav" in index_candidate["body"]
    assert "Inspect the workspace in read-only mode" in detail["agent"]["prompt_excerpt"]
    assert "COMPRESSED WORKSPACE SCAN JSON" not in detail["agent"]["prompt_excerpt"]
    assert "Agent nav" in detail["agent"]["reply_excerpt"]
    assert list_pending(home, load_config(home)) == []

    accepted = json_response_body(_post(home, "/api/kb/project-nav/draft/accept", {"draft_id": result["draft_id"]}))
    assert accepted["ok"] is True
    assert accepted["draft_deleted"] is True
    assert accepted["written_count"] == 2
    assert _get(home, "/api/kb/project-nav/drafts", {"project_key": ["demo-key"]})["drafts"] == []
    missing = knowledge_route_response(home, "GET", "/api/kb/project-nav/draft", {"id": [result["draft_id"]]}, b"", {})
    assert b"404" in missing.split(b"\r\n", 1)[0]
    pending = list_pending(home, load_config(home))
    assert pending == []
    entries = _get(home, "/api/kb/project-nav", {"project_key": ["demo-key"]})["entries"]
    assert [item["slug"] for item in entries] == ["index"]


def test_project_nav_generate_uses_agent_assisted_candidates(tmp_path: Path, monkeypatch):
    import aha_cli.web.knowledge_routes as kr

    _sync_project_nav_jobs(monkeypatch)
    home = _setup(tmp_path)
    ws = _make_nav_workspace(tmp_path)

    def agent(context: dict) -> str:
        assert context["backend"] == "codex"
        assert context["model"] == "gpt-test"
        assert "Inspect the workspace in read-only mode" in context["prompt"]
        assert "COMPRESSED WORKSPACE SCAN JSON" not in context["prompt"]
        assert "`## 项目介绍`" in context["prompt"]
        assert "`## 项目结构 / 核心 Nav`" in context["prompt"]
        context["progress_callback"]("agent_command_started", {"command": "Read pyproject.toml"})
        context["progress_callback"]("agent_usage", {"usage": {"total_tokens": 456}})
        return json.dumps([
            {
                "kind": "navigation",
                "scope": "project",
                "project_key": "demo-key",
                "slug": "index",
                "title": "Agent nav",
                "body": (
                    "## 项目介绍\nAgent generated project briefing.\n\n"
                    "## 如何编译 / 使用\n- `python -m pytest`\n\n"
                    "## 注意事项\n- Keep nav concise.\n\n"
                    "## 编码规范\n- Follow existing style.\n\n"
                    "## 项目结构 / 核心 Nav\n### 模块索引\n- [Agent Core](modules/agent-core.md)\n"
                ),
                "tags": ["navigation", "index"],
                "related_files": ["src/nav_demo/core"],
                "confidence": 0.8,
            },
            {
                "kind": "navigation",
                "scope": "project",
                "project_key": "demo-key",
                "slug": "modules/agent-core",
                "title": "Agent Core",
                "body": "## 模块职责\nagent generated\n",
                "tags": ["navigation", "module"],
                "related_files": ["src/nav_demo/core"],
                "confidence": 0.7,
            },
        ])

    monkeypatch.setattr(kr, "project_navigation_agent", agent)

    result = json_response_body(_post(home, "/api/kb/project-nav", {
        "workspace_path": str(ws),
        "project_key": "demo-key",
        "backend": "codex",
        "model": "gpt-test",
        "proxy_enabled": False,
    }))

    assert result["ok"] is True
    drafts = _get(home, "/api/kb/project-nav/drafts", {"project_key": ["demo-key"]})["drafts"]
    assert drafts[0]["status"] == "completed"
    assert drafts[0]["agent"]["status"] == "used"
    assert drafts[0]["validation"]["ok"] is True
    assert any("Read pyproject.toml" in item["message"] for item in drafts[0]["agent_log"])
    assert any(item.get("total_tokens") == 456 for item in drafts[0]["agent_log"])
    assert list_pending(home, load_config(home)) == []
    accepted = json_response_body(_post(home, "/api/kb/project-nav/draft/accept", {"draft_id": result["draft_id"]}))
    assert accepted["written_count"] == 2
    assert _get(home, "/api/kb/project-nav/drafts", {"project_key": ["demo-key"]})["drafts"] == []
    entries = _get(home, "/api/kb/project-nav", {"project_key": ["demo-key"]})["entries"]
    assert [item["slug"] for item in entries] == ["index"]


def test_project_nav_generate_fails_when_agent_output_invalid(tmp_path: Path, monkeypatch):
    import aha_cli.web.knowledge_routes as kr

    _sync_project_nav_jobs(monkeypatch)
    home = _setup(tmp_path)
    ws = _make_nav_workspace(tmp_path)
    monkeypatch.setattr(kr, "project_navigation_agent", lambda _context: "not json")

    result = json_response_body(_post(home, "/api/kb/project-nav", {
        "workspace_path": str(ws),
        "project_key": "demo-key",
        "backend": "codex",
    }))

    assert result["ok"] is True
    drafts = _get(home, "/api/kb/project-nav/drafts", {"project_key": ["demo-key"]})["drafts"]
    assert drafts[0]["status"] == "failed"
    assert drafts[0]["agent"]["status"] == "invalid"
    assert "Inspect the workspace in read-only mode" in drafts[0]["agent"]["prompt_excerpt"]
    assert "COMPRESSED WORKSPACE SCAN JSON" not in drafts[0]["agent"]["prompt_excerpt"]
    assert drafts[0]["agent"]["reply_excerpt"] == "not json"
    assert [item["stage"] for item in drafts[0]["agent_log"]] == ["queued", "running", "failed"]
    assert "fallback" not in drafts[0]["agent"]
    assert drafts[0]["error"]
    assert list_pending(home, load_config(home)) == []
    accept = _post(home, "/api/kb/project-nav/draft/accept", {"draft_id": result["draft_id"]})
    assert b"400" in accept.split(b"\r\n", 1)[0]
    entries = _get(home, "/api/kb/project-nav", {"project_key": ["demo-key"]})["entries"]
    assert entries == []
    rejected = json_response_body(_post(home, "/api/kb/project-nav/draft/reject", {"draft_id": result["draft_id"]}))
    assert rejected["draft"]["status"] == "rejected"
    assert _get(home, "/api/kb/project-nav/drafts", {"project_key": ["demo-key"]})["drafts"] == []


def test_project_nav_draft_reject_discards_without_writing_entries(tmp_path: Path, monkeypatch):
    _sync_project_nav_jobs(monkeypatch)
    _stub_project_nav_agent(monkeypatch)
    home = _setup(tmp_path)
    ws = _make_nav_workspace(tmp_path)

    result = json_response_body(_post(home, "/api/kb/project-nav", {
        "workspace_path": str(ws),
        "project_key": "demo-key",
        "backend": "codex",
    }))
    rejected = json_response_body(_post(home, "/api/kb/project-nav/draft/reject", {"draft_id": result["draft_id"]}))

    assert rejected["ok"] is True
    assert rejected["draft"]["status"] == "rejected"
    assert _get(home, "/api/kb/project-nav/drafts", {"project_key": ["demo-key"]})["drafts"] == []
    retry = json_response_body(_post(home, "/api/kb/project-nav", {
        "workspace_path": str(ws),
        "project_key": "demo-key",
        "backend": "codex",
    }))
    assert retry["ok"] is True
    assert retry["draft_id"] != result["draft_id"]
    assert _get(home, "/api/kb/project-nav", {"project_key": ["demo-key"]})["entries"] == []
    assert list_pending(home, load_config(home)) == []


def test_project_nav_running_draft_can_be_stopped_and_retried(tmp_path: Path, monkeypatch):
    import aha_cli.web.knowledge_routes as kr

    monkeypatch.setattr(kr, "dispatch_project_nav_job", lambda *args, **kwargs: None)
    home = _setup(tmp_path)
    ws = _make_nav_workspace(tmp_path)

    result = json_response_body(_post(home, "/api/kb/project-nav", {
        "workspace_path": str(ws),
        "project_key": "demo-key",
        "backend": "codex",
    }))
    assert "Inspect the workspace in read-only mode" in result["draft"]["agent"]["prompt_excerpt"]

    stopped = json_response_body(_post(home, "/api/kb/project-nav/draft/stop", {"draft_id": result["draft_id"]}))

    assert stopped["ok"] is True
    assert stopped["draft"]["status"] == "stopped"
    assert stopped["stop"]["reason"] == "no process recorded"
    assert stopped["draft"]["agent_log"][-1]["stage"] == "stopped"
    drafts = _get(home, "/api/kb/project-nav/drafts", {"project_key": ["demo-key"]})["drafts"]
    assert drafts[0]["status"] == "stopped"

    retry = json_response_body(_post(home, "/api/kb/project-nav", {
        "workspace_path": str(ws),
        "project_key": "demo-key",
        "backend": "codex",
    }))
    assert retry["ok"] is True
    assert retry["draft_id"] != result["draft_id"]


def test_project_nav_generate_conflicts_when_navigation_already_exists(tmp_path: Path, monkeypatch):
    _sync_project_nav_jobs(monkeypatch)
    home = _setup(tmp_path)
    cfg = load_config(home)
    ws = _make_nav_workspace(tmp_path)
    write_entry(
        home, config=cfg, scope="project", kind="navigation", project_key_value="demo-key",
        title="已有导航", body="## 模块索引\n- existing", slug="index",
        meta={"type": "navigation"},
    )

    resp = _post(home, "/api/kb/project-nav", {
        "workspace_path": str(ws),
        "project_key": "demo-key",
    })
    assert b"409" in resp.split(b"\r\n", 1)[0]
    result = json_response_body(resp)

    assert result["ok"] is False
    assert result["status"] == "already_exists"
    assert result["already_exists"] is True
    assert _get(home, "/api/kb/project-nav/drafts", {"project_key": ["demo-key"]})["drafts"] == []
    assert list_pending(home, cfg) == []


def test_project_nav_generate_conflicts_with_running_or_completed_draft(tmp_path: Path, monkeypatch):
    _sync_project_nav_jobs(monkeypatch)
    _stub_project_nav_agent(monkeypatch)
    home = _setup(tmp_path)
    cfg = load_config(home)
    ws = _make_nav_workspace(tmp_path)
    running = nav_drafts.create_draft(home, cfg, {
        "status": "running",
        "workspace_path": str(ws),
        "project_key": "demo-key",
    })

    resp = _post(home, "/api/kb/project-nav", {
        "workspace_path": str(ws),
        "project_key": "demo-key",
        "backend": "codex",
    })
    assert b"409" in resp.split(b"\r\n", 1)[0]
    conflict = json_response_body(resp)
    assert conflict["status"] == "already_running"
    assert conflict["draft_id"] == running["id"]

    nav_drafts.update_draft(home, cfg, running["id"], status="rejected")
    completed = nav_drafts.create_draft(home, cfg, {
        "status": "completed",
        "workspace_path": str(ws),
        "project_key": "demo-key",
        "candidates": [],
    })

    resp = _post(home, "/api/kb/project-nav", {
        "workspace_path": str(ws),
        "project_key": "demo-key",
        "backend": "codex",
    })
    assert b"409" in resp.split(b"\r\n", 1)[0]
    conflict = json_response_body(resp)
    assert conflict["status"] == "already_has_draft"
    assert conflict["draft_id"] == completed["id"]


def test_project_nav_generate_uses_run_workspace_when_provided(tmp_path: Path, monkeypatch):
    _sync_project_nav_jobs(monkeypatch)
    _stub_project_nav_agent(monkeypatch)
    home = _setup(tmp_path)
    ws = _make_nav_workspace(tmp_path)
    plan = create_plan(
        home,
        "nav goal",
        1,
        "research",
        ["Map nav"],
        [],
        workspace_path=str(ws),
    )

    result = json_response_body(_post(home, "/api/kb/project-nav", {"run_id": plan["id"], "backend": "codex"}))

    assert result["ok"] is True
    assert result["run_id"] == plan["id"]
    assert result["workspace_path"] == str(ws)
    drafts = _get(home, "/api/kb/project-nav/drafts", {"run_id": [plan["id"]]})["drafts"]
    assert drafts[0]["status"] == "completed"
    assert drafts[0]["workspace_path"] == str(ws)
    assert drafts[0]["validation"]["ok"] is True


def test_project_nav_reset_deletes_entire_navigation_tree(tmp_path: Path):
    home = _setup(tmp_path)
    cfg = load_config(home)
    _write_project_nav_tree(home, cfg, "git-abc")
    _write_project_nav_tree(home, cfg, "git-other")
    write_entry(
        home, config=cfg, scope="project", kind="solutions",
        project_key_value="git-abc", title="Fix build", body="do x",
    )

    result = json_response_body(_delete(home, "/api/kb/project-nav", {"project_key": "git-abc"}))

    assert result["ok"] is True
    assert result["reset_project_nav"] is True
    assert result["project_key"] == "git-abc"
    assert result["deleted_count"] == 3
    assert list_entries(home, config=cfg, scope="project", kind="navigation", project_key_value="git-abc") == []
    assert len(list_entries(home, config=cfg, scope="project", kind="navigation", project_key_value="git-other")) == 3
    assert len(list_entries(home, config=cfg, scope="project", kind="solutions", project_key_value="git-abc")) == 1


def test_project_nav_reset_can_derive_project_from_workspace_path(tmp_path: Path):
    home = _setup(tmp_path)
    cfg = load_config(home)
    ws = _make_nav_workspace(tmp_path)
    key = project_key(ws)
    _write_project_nav_tree(home, cfg, key)

    result = json_response_body(_delete(home, "/api/kb/project-nav", {"workspace_path": str(ws)}))

    assert result["ok"] is True
    assert result["reset_project_nav"] is True
    assert result["project_key"] == key
    assert result["workspace_path"] == str(ws)
    assert result["deleted_count"] == 3
    assert list_entries(home, config=cfg, scope="project", kind="navigation", project_key_value=key) == []


def test_project_nav_list_can_derive_project_from_workspace_path(tmp_path: Path):
    home = _setup(tmp_path)
    cfg = load_config(home)
    ws = _make_nav_workspace(tmp_path)
    key = project_key(ws)
    _write_project_nav_tree(home, cfg, key)

    result = _get(home, "/api/kb/project-nav", {"workspace_path": [str(ws)]})

    assert result["project_key"] == key
    assert result["workspace_path"] == str(ws)
    assert result["count"] == 1
    assert [entry["slug"] for entry in result["entries"]] == ["index"]


def test_project_nav_list_defaults_to_all_navigation_entry_points(tmp_path: Path):
    home = _setup(tmp_path)
    cfg = load_config(home)
    _write_project_nav_tree(home, cfg, "git-abc")
    _write_project_nav_tree(home, cfg, "git-other")

    result = _get(home, "/api/kb/project-nav")

    assert result["project_key"] is None
    assert result["count"] == 2
    assert {entry["slug"] for entry in result["entries"]} == {"index"}
    assert {entry["project_key"] for entry in result["entries"]} == {"git-abc", "git-other"}


def test_project_nav_draft_list_defaults_to_all_drafts(tmp_path: Path, monkeypatch):
    _sync_project_nav_jobs(monkeypatch)
    _stub_project_nav_agent(monkeypatch)
    home = _setup(tmp_path)
    ws = _make_nav_workspace(tmp_path)

    _post(home, "/api/kb/project-nav", {
        "workspace_path": str(ws),
        "project_key": "demo-a",
        "backend": "codex",
    })
    _post(home, "/api/kb/project-nav", {
        "workspace_path": str(ws),
        "project_key": "demo-b",
        "backend": "codex",
    })

    result = _get(home, "/api/kb/project-nav/drafts")

    assert result["project_key"] is None
    assert result["count"] == 2
    assert {draft["project_key"] for draft in result["drafts"]} == {"demo-a", "demo-b"}


def test_project_nav_config_disable_does_not_delete_navigation(tmp_path: Path):
    home = _setup(tmp_path)
    cfg = load_config(home)
    _write_project_nav_tree(home, cfg, "git-abc")

    out = json_response_body(_patch(home, "/api/kb/config", {"project_nav": {"enabled": False}}))

    assert out["knowledge"]["project_nav"]["enabled"] is False
    assert len(list_entries(home, config=load_config(home), scope="project", kind="navigation", project_key_value="git-abc")) == 3


def test_delete_navigation_index_entry_cascades_project_nav(tmp_path: Path):
    home = _setup(tmp_path)
    cfg = load_config(home)
    _write_project_nav_tree(home, cfg, "git-abc")
    index_entry = next(
        entry for entry in list_entries(home, config=cfg, scope="project", kind="navigation", project_key_value="git-abc")
        if entry["meta"]["slug"] == "index"
    )

    result = json_response_body(_delete(home, "/api/kb/entry", {"id": index_entry["meta"]["id"]}))

    assert result["ok"] is True
    assert result["reset_project_nav"] is True
    assert result["deleted_count"] == 3
    assert list_entries(home, config=cfg, scope="project", kind="navigation", project_key_value="git-abc") == []


def test_entry_lookup_by_id(tmp_path: Path):
    home = _setup(tmp_path)
    cfg = load_config(home)
    write_entry(home, config=cfg, scope="general", kind="wiki", title="Topic", body="hello world")
    entry_id = _get(home, "/api/kb/entries")["entries"][0]["id"]
    entry = _get(home, "/api/kb/entry", {"id": [entry_id]})
    assert entry["meta"]["title"] == "Topic"
    assert entry["body"] == "hello world"

    missing = knowledge_route_response(home, "GET", "/api/kb/entry", {"id": ["nope"]}, b"", {})
    assert b"404" in missing.split(b"\r\n", 1)[0]


def test_entry_update_status_and_delete(tmp_path: Path):
    home = _setup(tmp_path)
    cfg = load_config(home)
    write_entry(home, config=cfg, scope="project", kind="solutions",
                project_key_value="git-abc", title="Fix build", body="do x",
                meta={"tags": ["ci"], "related_files": ["old.py"]})
    entry_id = _get(home, "/api/kb/entries")["entries"][0]["id"]

    updated = json_response_body(_patch(home, "/api/kb/entry", {
        "id": entry_id,
        "title": "Fix build safely",
        "body": "do y",
        "tags": "ci, build",
        "related_files": "src/app.py",
        "invalid_when": "build system changes",
    }))
    assert updated["ok"] is True
    assert updated["entry"]["meta"]["title"] == "Fix build safely"
    assert updated["entry"]["meta"]["slug"] == "fix-build"
    assert updated["entry"]["body"] == "do y"
    assert updated["entry"]["meta"]["tags"] == ["ci", "build"]
    assert updated["entry"]["meta"]["related_files"] == ["src/app.py"]

    stale = json_response_body(_patch(home, "/api/kb/entry", {
        "id": entry_id,
        "status": "stale",
        "mark_stale": True,
    }))
    assert stale["entry"]["meta"]["status"] == "stale"
    assert stale["entry"]["meta"]["review_after"]

    restored = json_response_body(_patch(home, "/api/kb/entry", {
        "id": entry_id,
        "status": "active",
        "review_after": "",
    }))
    assert restored["entry"]["meta"]["status"] == "active"
    assert restored["entry"]["meta"]["review_after"] is None

    deleted = json_response_body(_delete(home, "/api/kb/entry", {"id": entry_id}))
    assert deleted["ok"] is True
    assert _get(home, "/api/kb/entries")["count"] == 0


def test_pending_approve_and_reject(tmp_path: Path):
    home = _setup(tmp_path)
    cfg = load_config(home)
    p1 = enqueue_candidate(home, cfg, {
        "kind": "solutions", "scope": "project", "project_key": "git-abc",
        "title": "Cand A", "body": "x", "meta": {"confidence": 0.4},
        "source": {"run_id": "r", "task_id": "t1", "round_id": "1"},
    })
    p2 = enqueue_candidate(home, cfg, {
        "kind": "solutions", "scope": "project", "project_key": "git-abc",
        "title": "Cand B", "body": "y", "meta": {},
        "source": {"run_id": "r", "task_id": "t2", "round_id": "1"},
    })
    id1 = json.loads(p1.read_text())["id"]
    id2 = json.loads(p2.read_text())["id"]

    listed = _get(home, "/api/kb/pending")
    assert listed["count"] == 2

    approved = json_response_body(_post(home, "/api/kb/approve", {"candidate_id": id1}))
    assert approved["ok"] and approved["action"] == "created"
    assert len(list_pending(home, cfg)) == 1

    rejected = json_response_body(_post(home, "/api/kb/reject", {"candidate_id": id2}))
    assert rejected["ok"] and rejected["rejected"] == id2
    assert list_pending(home, cfg) == []

    missing = _post(home, "/api/kb/approve", {"candidate_id": "cand_x"})
    assert b"404" in missing.split(b"\r\n", 1)[0]


def test_config_get_and_patch(tmp_path: Path):
    home = _setup(tmp_path)
    cfg = _get(home, "/api/kb/config")
    assert cfg["enabled"] is True
    assert cfg["git"]["auto_push"] is False

    resp = knowledge_route_response(
        home, "PATCH", "/api/kb/config", {},
        json.dumps({
            "git": {"enabled": True, "remote": "git@h:u/r.git", "auto_push": True},
            "project_nav": {"enabled": False},
            "curation": {"gate": "auto"},
        }).encode(), {},
    )
    out = json_response_body(resp)
    assert out["ok"]
    assert out["knowledge"]["git"]["remote"] == "git@h:u/r.git"
    assert out["knowledge"]["git"]["auto_push"] is True
    assert out["knowledge"]["project_nav"]["enabled"] is False
    assert out["knowledge"]["curation"]["gate"] == "auto"
    # Persisted to disk and other defaults preserved (branch).
    saved = read_json(config_path(home))["knowledge"]
    assert saved["git"]["remote"] == "git@h:u/r.git"
    assert saved["project_nav"]["enabled"] is False
    assert load_config(home)["knowledge"]["git"]["branch"] == "main"


def test_config_patch_rejects_bad_gate(tmp_path: Path):
    home = _setup(tmp_path)
    resp = knowledge_route_response(
        home, "PATCH", "/api/kb/config", {},
        json.dumps({"curation": {"gate": "bogus"}}).encode(), {},
    )
    assert b"400" in resp.split(b"\r\n", 1)[0]


def test_config_patch_tolerates_non_dict_git_curation(tmp_path: Path):
    # Hand-written config where git/curation are not dicts must not 500.
    home = tmp_path / ".aha"
    write_json(config_path(home), {"knowledge": {"enabled": True, "git": "oops", "curation": 5, "project_nav": "oops"}})
    init_knowledge_base(home, load_config(home))
    resp = knowledge_route_response(
        home, "PATCH", "/api/kb/config", {},
        json.dumps({
            "git": {"remote": "git@h:u/r.git"},
            "project_nav": {"enabled": False},
            "curation": {"gate": "auto"},
        }).encode(), {},
    )
    out = json_response_body(resp)
    assert out["ok"]
    assert out["knowledge"]["git"]["remote"] == "git@h:u/r.git"
    assert out["knowledge"]["project_nav"]["enabled"] is False
    assert out["knowledge"]["curation"]["gate"] == "auto"


# --------------------------------------------------------------------------- #
# Server dispatch (protects web/server.py wiring), via the real UI handler.
# --------------------------------------------------------------------------- #
def test_server_serves_kb_status(tmp_path: Path):
    home = _setup(tmp_path)
    resp = asyncio.run(fetch_ui_response(home, "", "/api/kb/status"))
    assert resp.startswith(b"HTTP/1.1 200 OK")
    assert json_response_body(resp)["initialized"] is True


def test_server_serves_knowledge_console_html(tmp_path: Path):
    home = _setup(tmp_path)
    resp = asyncio.run(fetch_ui_response(home, "", "/static/knowledge.html"))
    assert resp.startswith(b"HTTP/1.1 200 OK")
    assert b"AHA" in resp and b"<!DOCTYPE html>" in resp


def test_server_patches_kb_config(tmp_path: Path):
    home = _setup(tmp_path)
    resp = asyncio.run(
        fetch_ui_response(home, "", "/api/kb/config", method="PATCH", payload={"curation": {"gate": "auto"}})
    )
    assert resp.startswith(b"HTTP/1.1 200 OK")
    assert json_response_body(resp)["knowledge"]["curation"]["gate"] == "auto"
    # Persisted through the real server path.
    assert load_config(home)["knowledge"]["curation"]["gate"] == "auto"
