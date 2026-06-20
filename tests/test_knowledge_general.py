from __future__ import annotations

import contextlib
import io
import json
from pathlib import Path

from aha_cli.cli import main
from aha_cli.domain.models import default_knowledge_config
from aha_cli.services.knowledge_distill import (
    general_tutorial_candidate,
    normalize_sidecar_candidates,
)
from aha_cli.services.knowledge_retrieval import retrieve_for_task
from aha_cli.store.io import write_json
from aha_cli.store.knowledge import (
    approve_candidate,
    init_knowledge_base,
    list_entries,
    list_pending,
    write_entry,
)
from aha_cli.store.paths import config_path


def _cfg(gate: str = "manual") -> dict:
    kb = default_knowledge_config()
    kb["enabled"] = True
    kb["curation"]["gate"] = gate
    return {"knowledge": kb}


def _home(tmp_path: Path, gate: str = "manual") -> Path:
    home = tmp_path / ".aha"
    cfg = _cfg(gate)
    write_json(config_path(home), cfg)
    init_knowledge_base(home, cfg)
    return home


def _run(home: Path, *args: str) -> tuple[int, str]:
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        rc = main(["--home", str(home), "kb", *args])
    return rc, out.getvalue()


# --------------------------------------------------------------------------- #
def test_general_tutorial_candidate_is_unbound_from_project():
    cand = general_tutorial_candidate(title="Git rebase 教程", body="how to rebase", tags=["git"])
    assert cand["scope"] == "general"
    assert cand["project_key"] is None
    assert cand["kind"] == "wiki"
    assert cand["meta"]["type"] == "wiki"
    assert cand["meta"]["distilled_by"] == "manual"


def test_sidecar_general_scope_drops_inherited_project_key():
    # Even when distillation runs inside a project, a general candidate must not
    # inherit that project's key.
    raw = [{"kind": "wiki", "scope": "general", "title": "通用教程", "body": "x"}]
    out = normalize_sidecar_candidates({"project_key": "proj-A"}, raw)
    assert len(out) == 1
    assert out[0]["scope"] == "general"
    assert out[0]["project_key"] is None


def test_cli_tutorial_add_enqueues_pending_then_approves_to_general(tmp_path: Path):
    home = _home(tmp_path, gate="manual")
    rc, out = _run(home, "tutorial", "add", "--title", "Docker 入门", "--body", "images and containers", "--json")
    assert rc == 0
    result = json.loads(out)
    assert result["gate"] == "manual"

    pending = list_pending(home, _cfg())
    assert len(pending) == 1
    assert pending[0]["scope"] == "general"
    assert pending[0]["project_key"] is None

    approve_candidate(home, _cfg(), pending[0]["id"])
    # Lands in general/wiki, not under any project.
    entries = list_entries(home, config=_cfg(), scope="general", kind="wiki")
    assert len(entries) == 1
    assert entries[0]["meta"]["title"] == "Docker 入门"
    assert entries[0]["meta"]["project_key"] is None
    assert entries[0]["path"].endswith("general/wiki/docker.md") or "general/wiki/" in entries[0]["path"]


def test_cli_tutorial_add_requires_body(tmp_path: Path):
    home = _home(tmp_path)
    rc, _ = _run(home, "tutorial", "add", "--title", "Empty")
    assert rc == 2
    assert list_pending(home, _cfg()) == []


def test_project_and_general_knowledge_do_not_pollute_each_other(tmp_path: Path):
    home = tmp_path / ".aha"
    cfg = _cfg()
    init_knowledge_base(home, cfg)
    # Project A private solution + a shared general tutorial that shares a term.
    write_entry(home, config=cfg, scope="project", kind="solutions", project_key_value="proj-A",
                title="proj A cache fix", body="clear the cache layer", meta={"type": "solution"})
    write_entry(home, config=cfg, scope="general", kind="wiki",
                title="cache concepts", body="general cache tutorial", meta={"type": "wiki"})
    # An unrelated general tutorial that must NOT flood unrelated tasks.
    write_entry(home, config=cfg, scope="general", kind="wiki",
                title="kubernetes basics", body="pods and deployments", meta={"type": "wiki"})

    # Task in project A about "cache": project entry first, then the relevant
    # general tutorial; the unrelated k8s tutorial does not appear.
    hits = retrieve_for_task(home, cfg, project_key="proj-A", terms=["cache"], max_entries=5)
    titles = [h["meta"]["title"] for h in hits]
    assert titles[0] == "proj A cache fix"          # project knowledge ranks first
    assert "cache concepts" in titles               # relevant general supplements
    assert "kubernetes basics" not in titles        # irrelevant general is filtered out

    # A different project B never sees project A's private knowledge, but shares general.
    hits_b = retrieve_for_task(home, cfg, project_key="proj-B", terms=["cache"], max_entries=5)
    titles_b = [h["meta"]["title"] for h in hits_b]
    assert "proj A cache fix" not in titles_b
    assert "cache concepts" in titles_b

    # No term match -> project recency fallback only, general excluded (no flooding).
    fallback = retrieve_for_task(home, cfg, project_key="proj-A", terms=["zzz"], max_entries=5)
    fb_titles = [h["meta"]["title"] for h in fallback]
    assert fb_titles == ["proj A cache fix"]
    assert "cache concepts" not in fb_titles
