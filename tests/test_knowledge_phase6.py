from __future__ import annotations

import contextlib
import io
import json
from pathlib import Path

from aha_cli.cli import main
from aha_cli.domain.models import default_knowledge_config
from aha_cli.services.knowledge_retrieval import retrieve_for_task
from aha_cli.store.io import write_json
from aha_cli.store.knowledge import (
    future_iso,
    init_knowledge_base,
    list_stale_entries,
    read_entry,
    write_entry,
)
from aha_cli.store.paths import config_path


def _home(tmp_path: Path) -> Path:
    home = tmp_path / ".aha"
    kb = default_knowledge_config()
    kb["enabled"] = True
    write_json(config_path(home), {"knowledge": kb})
    init_knowledge_base(home, {"knowledge": kb})
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


# --- general-scope knowledge is retrievable ---------------------------------
def test_general_knowledge_is_retrieved(tmp_path: Path):
    home = _home(tmp_path)
    cfg = _cfg()
    write_entry(home, config=cfg, scope="general", kind="wiki",
                title="Zipapp packaging gotcha", body="bundle submodules", meta={"tags": ["build"]})
    hits = retrieve_for_task(home, cfg, project_key="git-none", terms=["zipapp", "packaging"], max_entries=5)
    assert any(h["meta"]["title"] == "Zipapp packaging gotcha" for h in hits)


# --- aha kb add: create / update / append -----------------------------------
def test_kb_add_create_update_append(tmp_path: Path):
    home = _home(tmp_path)
    rc, out = _run(home, "add", "--kind", "wiki", "--title", "Serial bridge", "--body", "v1 body", "--json")
    assert rc == 0 and json.loads(out)["action"] == "created"

    rc, out = _run(home, "add", "--kind", "wiki", "--title", "Serial bridge", "--body", "v2 body", "--json")
    assert json.loads(out)["action"] == "updated"
    path = Path(json.loads(out)["path"])
    assert read_entry(path)["body"] == "v2 body"

    rc, out = _run(home, "add", "--kind", "wiki", "--title", "Serial bridge", "--body", "more notes", "--append", "--json")
    assert json.loads(out)["action"] == "appended"
    body = read_entry(path)["body"]
    assert "v2 body" in body and "more notes" in body and "追加于" in body


def test_kb_add_project_requires_project_key(tmp_path: Path):
    home = _home(tmp_path)
    rc, _ = _run(home, "add", "--scope", "project", "--title", "X", "--body", "y")
    assert rc == 2


def test_kb_add_update_preserves_metadata(tmp_path: Path):
    home = _home(tmp_path)
    _run(home, "add", "--kind", "wiki", "--title", "Meta", "--body", "v1",
         "--tag", "alpha", "--review-days", "30")
    rc, out = _run(home, "add", "--kind", "wiki", "--title", "Meta", "--body", "v2", "--json")
    path = Path(json.loads(out)["path"])
    meta = read_entry(path)["meta"]
    # Plain update without --tag/--review-days must keep the existing ones.
    assert meta.get("tags") == ["alpha"]
    assert meta.get("review_after")
    # But passing --tag overrides.
    _run(home, "add", "--kind", "wiki", "--title", "Meta", "--body", "v3", "--tag", "beta")
    assert read_entry(path)["meta"]["tags"] == ["beta"]


# --- review_after / stale ---------------------------------------------------
def test_list_stale_entries(tmp_path: Path):
    home = _home(tmp_path)
    cfg = _cfg()
    write_entry(home, config=cfg, scope="general", kind="wiki", title="Old", body="x",
                meta={"review_after": "2000-01-01T00:00:00+00:00"})
    write_entry(home, config=cfg, scope="general", kind="wiki", title="Fresh", body="y",
                meta={"review_after": future_iso(30)})
    write_entry(home, config=cfg, scope="general", kind="wiki", title="NoReview", body="z")

    stale = list_stale_entries(home, cfg)
    titles = {e["meta"]["title"] for e in stale}
    assert "Old" in titles
    assert "Fresh" not in titles and "NoReview" not in titles


def test_cli_add_review_days_and_stale(tmp_path: Path):
    home = _home(tmp_path)
    # review_days=0 -> review_after is now -> immediately stale.
    rc, _ = _run(home, "add", "--kind", "wiki", "--title", "DueNow", "--body", "b", "--review-days", "0")
    assert rc == 0
    rc, out = _run(home, "stale", "--json")
    assert rc == 0
    stale = json.loads(out)
    assert any(e["title"] == "DueNow" for e in stale)

    # stale --json summaries carry review_after for UI/script consumption.
    assert all("review_after" in e for e in stale)
    assert any(e["review_after"] for e in stale)

    # status reports stale count
    rc, out = _run(home, "status", "--json")
    assert json.loads(out)["stale"] >= 1
