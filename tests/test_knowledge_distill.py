from __future__ import annotations

import io
import json
from pathlib import Path

from aha_cli.cli import main
from aha_cli.domain.models import default_knowledge_config
from aha_cli.services.knowledge_distill import (
    build_distill_context,
    distill_and_enqueue,
    heuristic_solution_candidate,
)
from aha_cli.store.config import load_config
from aha_cli.store.io import write_json
from aha_cli.store.knowledge import (
    approve_candidate,
    init_knowledge_base,
    knowledge_root,
    list_entries,
    list_pending,
)
from aha_cli.store.paths import config_path


def _cfg(gate: str = "manual", enabled: bool = True) -> dict:
    kb = default_knowledge_config()
    kb["enabled"] = enabled
    kb["curation"]["gate"] = gate
    return {"knowledge": kb}


def _context() -> dict:
    return build_distill_context(
        final_body="We fixed the build.",
        final_context={
            "summary": "Fix zipapp ModuleNotFound by bundling submodule",
            "changed_files": ["scripts/build_onebin.py"],
            "verification": ["python3 -m aha_cli --help"],
            "risks": ["only covers py3.10+"],
        },
        task_title="Package onebin",
        project_key_value="git-abc123",
        source={"run_id": "r1", "task_id": "task-001", "round_id": "1"},
    )


# --------------------------------------------------------------------------- #
def test_heuristic_candidate_shape():
    cands = heuristic_solution_candidate(_context())
    assert len(cands) == 1
    c = cands[0]
    assert c["kind"] == "solutions"
    assert c["scope"] == "project"
    assert c["project_key"] == "git-abc123"
    assert c["title"].startswith("Fix zipapp")
    assert "## 验证方式" in c["body"]
    assert "scripts/build_onebin.py" in c["body"]
    assert c["meta"]["distilled_by"] == "heuristic"
    assert c["meta"]["source_tasks"] == ["r1/task-001/1"]


def test_heuristic_uses_final_body_when_summary_missing():
    ctx = build_distill_context(
        final_body="The root cause was a stale lock file; deleting .aha/lock fixed startup.",
        final_context={"changed_files": ["src/x.py"]},  # no summary
        task_title="Startup hang",
        project_key_value="git-abc123",
        source={"run_id": "r1", "task_id": "t1", "round_id": "1"},
    )
    c = heuristic_solution_candidate(ctx)[0]
    # The final body is surfaced as the effective solution, not a placeholder.
    assert "stale lock file" in c["body"]
    assert "(见任务 final)" not in c["body"]


def test_heuristic_keeps_final_excerpt_section_when_summary_present():
    ctx = build_distill_context(
        final_body="Detailed narrative about the fix and the debugging journey.",
        final_context={"summary": "Delete stale lock on startup"},
        task_title="Startup hang",
        project_key_value="git-abc123",
        source={"run_id": "r1", "task_id": "t1", "round_id": "1"},
    )
    c = heuristic_solution_candidate(ctx)[0]
    assert "Delete stale lock on startup" in c["body"]
    assert "## final 摘录" in c["body"]
    assert "debugging journey" in c["body"]


def test_distill_initializes_skeleton_without_prior_init(tmp_path: Path):
    # No init_knowledge_base / `aha kb init` beforehand: distill must build the
    # skeleton (incl. the .gitignore that excludes .pending/) before writing.
    root = tmp_path / ".aha"
    cfg = _cfg("manual")
    res = distill_and_enqueue(root, cfg, _context())
    assert res["candidates"] == 1

    kb_root = knowledge_root(root, cfg)
    assert (kb_root / "aha-knowledge.json").is_file()
    assert (kb_root / "README.md").is_file()
    gitignore = kb_root / ".gitignore"
    assert gitignore.is_file()
    assert ".pending/" in gitignore.read_text()
    assert len(list_pending(root, cfg)) == 1


def test_manual_gate_enqueues_without_touching_tracked_tree(tmp_path: Path):
    root = tmp_path / ".aha"
    cfg = _cfg("manual")
    init_knowledge_base(root, cfg)
    res = distill_and_enqueue(root, cfg, _context())
    assert res["gate"] == "manual"
    assert res["candidates"] == 1

    pending = list_pending(root, cfg)
    assert len(pending) == 1
    # Nothing written into the tracked solutions tree yet.
    assert list_entries(root, config=cfg, scope="project", kind="solutions",
                        project_key_value="git-abc123") == []


def test_auto_gate_writes_entry_directly(tmp_path: Path):
    root = tmp_path / ".aha"
    cfg = _cfg("auto")
    init_knowledge_base(root, cfg)
    res = distill_and_enqueue(root, cfg, _context())
    assert res["gate"] == "auto"
    assert len(res["written"]) == 1
    entries = list_entries(root, config=cfg, scope="project", kind="solutions",
                           project_key_value="git-abc123")
    assert len(entries) == 1
    # git disabled -> commit step is skipped, not failed.
    assert res["git"].get("skipped")


def test_gate_off_and_disabled_skip(tmp_path: Path):
    root = tmp_path / ".aha"
    assert distill_and_enqueue(root, _cfg("off"), _context())["candidates"] == 0
    assert "skipped" in distill_and_enqueue(root, _cfg(enabled=False), _context())


def test_approve_promotes_pending_to_entry(tmp_path: Path):
    root = tmp_path / ".aha"
    cfg = _cfg("manual")
    init_knowledge_base(root, cfg)
    distill_and_enqueue(root, cfg, _context())
    pending = list_pending(root, cfg)
    assert len(pending) == 1
    cid = pending[0]["id"]

    entry_path = approve_candidate(root, cfg, cid)
    assert entry_path.exists()
    assert list_pending(root, cfg) == []
    entries = list_entries(root, config=cfg, scope="project", kind="solutions",
                           project_key_value="git-abc123")
    assert len(entries) == 1
    assert entries[0]["meta"]["outcome"] == "success"


def test_pluggable_distiller_overrides_heuristic(tmp_path: Path):
    root = tmp_path / ".aha"
    cfg = _cfg("manual")
    init_knowledge_base(root, cfg)

    def fake(ctx: dict) -> list[dict]:
        return [{"kind": "solutions", "scope": "general", "title": "Custom", "body": "x", "meta": {}}]

    res = distill_and_enqueue(root, cfg, _context(), distiller=fake)
    assert res["candidates"] == 1
    assert list_pending(root, cfg)[0]["title"] == "Custom"


# --------------------------------------------------------------------------- #
# Integration: a real finalize triggers distillation when enabled.
# --------------------------------------------------------------------------- #
def test_finalize_triggers_distill_when_enabled(tmp_path: Path):
    home = tmp_path / ".aha"
    rc = main(["--home", str(home), "init"])
    assert rc == 0
    out = io.StringIO()
    import contextlib

    with contextlib.redirect_stdout(out):
        rc = main(["--home", str(home), "plan", "Build onebin", "--agents", "1"])
    assert rc == 0
    run_id = out.getvalue().splitlines()[0].split(": ", 1)[1].strip()

    # Enable knowledge distillation in this home's config.
    cfg = load_config(home)
    cfg["knowledge"]["enabled"] = True
    write_json(config_path(home), cfg)

    from aha_cli.store.finals import write_task_result

    write_task_result(
        home,
        run_id,
        "task-001",
        "Final report body.",
        policy="finalize",
        final_context={
            "summary": "Bundle submodule into the zipapp",
            "changed_files": ["scripts/build_onebin.py"],
            "verification": ["aha --help"],
            "risks": ["py3.10+ only"],
        },
    )

    pending = list_pending(home, load_config(home))
    assert len(pending) == 1
    assert pending[0]["meta"]["distilled_by"] == "heuristic"
    assert pending[0]["title"].startswith("Bundle submodule")


def test_finalize_distill_is_noop_when_disabled(tmp_path: Path):
    home = tmp_path / ".aha"
    main(["--home", str(home), "init"])
    import contextlib

    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        main(["--home", str(home), "plan", "X", "--agents", "1"])
    run_id = out.getvalue().splitlines()[0].split(": ", 1)[1].strip()

    from aha_cli.store.finals import write_task_result

    # knowledge.enabled defaults to False -> finalize must not create a KB.
    write_task_result(home, run_id, "task-001", "body", policy="finalize",
                      final_context={"summary": "s"})
    assert list_pending(home, load_config(home)) == []


def test_cli_kb_pending_lists_candidates(tmp_path: Path):
    root = tmp_path / ".aha"
    cfg = _cfg("manual")
    init_knowledge_base(root, cfg)
    distill_and_enqueue(root, cfg, _context())
    # Persist config so the CLI sees the same knowledge home.
    write_json(config_path(root), cfg)

    import contextlib

    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        rc = main(["--home", str(root), "kb", "pending", "--json"])
    assert rc == 0
    listed = json.loads(out.getvalue())
    assert len(listed) == 1
    assert listed[0]["title"].startswith("Fix zipapp")
