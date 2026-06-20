from __future__ import annotations

import asyncio
import json
from pathlib import Path

from aha_cli.domain.models import default_knowledge_config
from aha_cli.store.config import load_config
from aha_cli.store.io import read_json, write_json
from aha_cli.store.knowledge import enqueue_candidate, init_knowledge_base, list_pending, write_entry
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
                project_key_value="git-abc", title="demo 项目地图", body="## 模块索引\n- **store**", slug="map")
    write_entry(home, config=cfg, scope="general", kind="wiki", title="Docker 教程", body="containers")

    # The project map is browsable via the navigation kind filter.
    nav = _get(home, "/api/kb/entries", {"kind": ["navigation"]})
    assert nav["count"] == 1 and nav["entries"][0]["type"] == "navigation"

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

    def sync_dispatch(root, cfg, note_id, backend, model):
        run_distill_job(root, cfg, note_id, agent=lambda ctx: reply)

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


def test_capture_distill_forwards_backend_and_model(tmp_path: Path, monkeypatch):
    import aha_cli.web.knowledge_routes as kr

    home = _setup(tmp_path)
    nid = json_response_body(_post(home, "/api/kb/capture", {"text": "raw"}))["note"]["id"]
    seen = {}

    def record_dispatch(root, cfg, note_id, backend, model):
        seen.update({"note_id": note_id, "backend": backend, "model": model})

    monkeypatch.setattr(kr, "dispatch_distill_job", record_dispatch)
    json_response_body(_post(home, "/api/kb/capture/distill", {"id": nid, "backend": "claude", "model": "claude-opus-4-8"}))
    assert seen == {"note_id": nid, "backend": "claude", "model": "claude-opus-4-8"}


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
        "slug": "map", "title": "demo 项目地图", "body": "## 模块索引",
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
            "curation": {"gate": "auto"},
        }).encode(), {},
    )
    out = json_response_body(resp)
    assert out["ok"]
    assert out["knowledge"]["git"]["remote"] == "git@h:u/r.git"
    assert out["knowledge"]["git"]["auto_push"] is True
    assert out["knowledge"]["curation"]["gate"] == "auto"
    # Persisted to disk and other defaults preserved (branch).
    saved = read_json(config_path(home))["knowledge"]
    assert saved["git"]["remote"] == "git@h:u/r.git"
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
    write_json(config_path(home), {"knowledge": {"enabled": True, "git": "oops", "curation": 5}})
    init_knowledge_base(home, load_config(home))
    resp = knowledge_route_response(
        home, "PATCH", "/api/kb/config", {},
        json.dumps({"git": {"remote": "git@h:u/r.git"}, "curation": {"gate": "auto"}}).encode(), {},
    )
    out = json_response_body(resp)
    assert out["ok"]
    assert out["knowledge"]["git"]["remote"] == "git@h:u/r.git"
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
