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
    find_entry,
    init_knowledge_base,
    iter_all_entries,
    knowledge_status,
    search_entries,
    write_entry,
)
from aha_cli.store.paths import config_path


def _cfg() -> dict:
    kb = default_knowledge_config()
    kb["enabled"] = True
    return {"knowledge": kb}


def _home(tmp_path: Path) -> Path:
    home = tmp_path / ".aha"
    cfg = _cfg()
    write_json(config_path(home), cfg)
    init_knowledge_base(home, cfg)
    return home


def _run(home: Path, *args: str) -> tuple[int, str]:
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        rc = main(["--home", str(home), "kb", *args])
    return rc, out.getvalue()


# --------------------------------------------------------------------------- #
def test_personal_entry_storage_and_status(tmp_path: Path):
    home = _home(tmp_path)
    cfg = _cfg()
    path = write_entry(
        home, config=cfg, scope="personal", kind="wiki",
        title="My scratch note", body="random idea about caching", meta={"type": "wiki"},
    )
    assert "personal/wiki/" in str(path)
    entry = find_entry(home, cfg, "my-scratch-note")
    assert entry is not None
    assert entry["meta"]["scope"] == "personal"
    assert entry["meta"]["project_key"] is None

    # Counted in iter_all_entries and status.
    assert any(e["meta"]["scope"] == "personal" for e in iter_all_entries(home, cfg))
    status = knowledge_status(home, cfg)
    assert status["personal"]["wiki"] == 1
    assert status["total_entries"] == 1


def test_personal_is_not_injected_but_is_searchable(tmp_path: Path):
    home = _home(tmp_path)
    cfg = _cfg()
    # A project entry and a personal entry that BOTH match the query terms.
    write_entry(home, config=cfg, scope="project", kind="solutions", project_key_value="k",
                title="cache fix", body="clear the cache", meta={"type": "solution"})
    write_entry(home, config=cfg, scope="personal", kind="wiki", project_key_value=None,
                title="cache musings", body="personal cache notes", meta={"type": "wiki"})

    # Injection must EXCLUDE personal even though it matches the terms.
    hits = retrieve_for_task(home, cfg, project_key="k", terms=["cache"], max_entries=10)
    titles = [h["meta"]["title"] for h in hits]
    assert "cache fix" in titles
    assert "cache musings" not in titles
    assert all(h["meta"]["scope"] != "personal" for h in hits)

    # But on-demand search/recall DOES find personal knowledge.
    found = [e["meta"]["title"] for e in search_entries(home, cfg, "cache")]
    assert "cache musings" in found


def test_cli_add_and_list_personal_scope(tmp_path: Path):
    home = _home(tmp_path)
    rc, _ = _run(home, "add", "--scope", "personal", "--kind", "wiki",
                 "--title", "Personal tip", "--body", "remember this")
    assert rc == 0
    rc, out = _run(home, "list", "--scope", "personal", "--json")
    assert rc == 0
    listed = json.loads(out)
    assert len(listed) == 1
    assert listed[0]["scope"] == "personal"
    assert listed[0]["title"] == "Personal tip"
