from __future__ import annotations

import contextlib
import io
import json
from pathlib import Path

from aha_cli.cli import main
from aha_cli.domain.models import default_knowledge_config
from aha_cli.store.io import write_json
from aha_cli.store.knowledge import (
    _legacy_candidate_identity,
    enqueue_candidate,
    init_knowledge_base,
    list_pending,
    write_entry,
)
from aha_cli.store.paths import config_path


def _home(tmp_path: Path) -> Path:
    home = tmp_path / ".aha"
    kb = default_knowledge_config()
    kb["enabled"] = True
    write_json(config_path(home), {"knowledge": kb})
    cfg = {"knowledge": kb}
    init_knowledge_base(home, cfg)
    return home


def _cfg() -> dict:
    kb = default_knowledge_config()
    kb["enabled"] = True
    return {"knowledge": kb}


def _run(home: Path, *args: str) -> tuple[int, str]:
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        rc = main(["--home", str(home), "kb", *args])
    return rc, out.getvalue()


def test_kb_list_show_search(tmp_path: Path):
    home = _home(tmp_path)
    cfg = _cfg()
    write_entry(home, config=cfg, scope="project", kind="solutions",
                project_key_value="git-abc", title="Fix flaky build",
                body="rerun with clean cache", meta={"tags": ["build", "ci"]})
    write_entry(home, config=cfg, scope="general", kind="wiki",
                title="Serial bridge overview", body="how the bridge works")

    rc, out = _run(home, "list", "--json")
    assert rc == 0
    assert len(json.loads(out)) == 2

    rc, out = _run(home, "list", "--scope", "general", "--json")
    assert len(json.loads(out)) == 1

    rc, out = _run(home, "list", "--kind", "solutions", "--json")
    listed = json.loads(out)
    assert len(listed) == 1 and listed[0]["title"] == "Fix flaky build"

    # show by slug
    rc, out = _run(home, "show", "fix-flaky-build")
    assert rc == 0 and "rerun with clean cache" in out

    rc, _ = _run(home, "show", "does-not-exist")
    assert rc == 1

    # search by tag/body text
    rc, out = _run(home, "search", "bridge", "--json")
    hits = json.loads(out)
    assert len(hits) == 1 and hits[0]["title"] == "Serial bridge overview"


def test_kb_show_by_id(tmp_path: Path):
    home = _home(tmp_path)
    cfg = _cfg()
    write_entry(home, config=cfg, scope="project", kind="solutions",
                project_key_value="git-abc", title="Fix flaky build",
                body="rerun with clean cache")
    rc, out = _run(home, "list", "--json")
    entry_id = json.loads(out)[0]["id"]
    assert entry_id.startswith("kb_")
    rc, out = _run(home, "show", entry_id)
    assert rc == 0 and "rerun with clean cache" in out


def test_kb_approve_cross_scope_same_slug_is_created(tmp_path: Path):
    home = _home(tmp_path)
    cfg = _cfg()
    # An existing entry in a DIFFERENT project, same title/slug.
    write_entry(home, config=cfg, scope="project", kind="solutions",
                project_key_value="git-OTHER", title="Same Title", body="x")
    cand = enqueue_candidate(home, cfg, {
        "kind": "solutions", "scope": "project", "project_key": "git-abc",
        "title": "Same Title", "body": "y", "meta": {},
        "source": {"run_id": "r1", "task_id": "t1", "round_id": "1"},
    })
    rc, out = _run(home, "approve", json.loads(cand.read_text())["id"], "--json")
    assert rc == 0
    # Same slug but different project -> a distinct entry -> created, not updated.
    assert json.loads(out)["action"] == "created"


def test_kb_reject_json_outputs_json(tmp_path: Path):
    home = _home(tmp_path)
    cfg = _cfg()
    cand = enqueue_candidate(home, cfg, {
        "kind": "solutions", "scope": "project", "project_key": "git-abc",
        "title": "Discard me", "body": "z", "meta": {},
        "source": {"run_id": "r1", "task_id": "t1", "round_id": "1"},
    })
    cid = json.loads(cand.read_text())["id"]
    rc, out = _run(home, "reject", cid, "--json")
    assert rc == 0
    payload = json.loads(out)
    assert payload["ok"] is True and payload["rejected"] == cid


def test_kb_approve_and_reject(tmp_path: Path):
    home = _home(tmp_path)
    cfg = _cfg()
    p1 = enqueue_candidate(home, cfg, {
        "kind": "solutions", "scope": "project", "project_key": "git-abc",
        "title": "Solve A", "body": "do x", "meta": {"outcome": "success"},
        "source": {"run_id": "r1", "task_id": "t1", "round_id": "1"},
    })
    p2 = enqueue_candidate(home, cfg, {
        "kind": "solutions", "scope": "project", "project_key": "git-abc",
        "title": "Solve B", "body": "do y", "meta": {"outcome": "success"},
        "source": {"run_id": "r1", "task_id": "t2", "round_id": "1"},
    })
    id1 = json.loads(p1.read_text())["id"]
    id2 = json.loads(p2.read_text())["id"]
    assert len(list_pending(home, cfg)) == 2

    rc, out = _run(home, "approve", id1, "--json")
    assert rc == 0
    result = json.loads(out)
    assert result["action"] == "created"
    assert Path(result["path"]).exists()
    assert len(list_pending(home, cfg)) == 1

    rc, out = _run(home, "reject", id2)
    assert rc == 0 and "rejected" in out
    assert list_pending(home, cfg) == []

    rc, _ = _run(home, "approve", "cand_missing")
    assert rc == 1


def test_kb_approve_dedup_updates_existing(tmp_path: Path):
    home = _home(tmp_path)
    cfg = _cfg()
    first = enqueue_candidate(home, cfg, {
        "kind": "solutions", "scope": "project", "project_key": "git-abc",
        "title": "Same Title", "body": "v1", "meta": {},
        "source": {"run_id": "r1", "task_id": "t1", "round_id": "1"},
    })
    rc, out = _run(home, "approve", json.loads(first.read_text())["id"], "--json")
    assert json.loads(out)["action"] == "created"

    # A different candidate (different source) with the same title -> same slug.
    second = enqueue_candidate(home, cfg, {
        "kind": "solutions", "scope": "project", "project_key": "git-abc",
        "title": "Same Title", "body": "v2", "meta": {},
        "source": {"run_id": "r2", "task_id": "t9", "round_id": "1"},
    })
    rc, out = _run(home, "approve", json.loads(second.read_text())["id"], "--json")
    assert rc == 0
    assert json.loads(out)["action"] == "updated"


def test_pending_identity_keeps_legacy_same_title_without_chinese_slug_collision(tmp_path: Path):
    home = _home(tmp_path)
    cfg = _cfg()
    source = {"run_id": "r1", "task_id": "t1"}
    existing = {
        "kind": "solutions",
        "scope": "project",
        "project_key": "git-abc",
        "title": "Wyze 云存业务层时间戳对齐逻辑",
        "body": "old",
        "source": source,
    }
    legacy_id = _legacy_candidate_identity(existing)
    legacy_path = enqueue_candidate(home, cfg, {**existing, "id": legacy_id})

    updated_path = enqueue_candidate(home, cfg, {**existing, "body": "new"})
    other_path = enqueue_candidate(home, cfg, {
        "kind": "solutions",
        "scope": "project",
        "project_key": "git-abc",
        "title": "Wyze 云存上传双路视频排查要点",
        "body": "other",
        "source": source,
    })

    assert updated_path == legacy_path
    assert other_path != legacy_path
    pending = list_pending(home, cfg)
    assert len(pending) == 2
    assert {item["title"] for item in pending} == {
        "Wyze 云存业务层时间戳对齐逻辑",
        "Wyze 云存上传双路视频排查要点",
    }
    assert next(item for item in pending if item["title"] == existing["title"])["body"] == "new"
