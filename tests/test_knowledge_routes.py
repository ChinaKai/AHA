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
