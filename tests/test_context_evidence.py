from __future__ import annotations

import contextlib
import io
from pathlib import Path
from unittest import mock

from aha_cli.cli import main
from aha_cli.services.context_evidence import (
    append_task_context_evidence,
    distill_context_evidence_after_turn,
    list_task_context_evidence,
    record_context_pack_from_prompt_metrics,
    record_agent_kb_feedback,
    task_context_evidence_path,
)
from aha_cli.store.filesystem import append_event, load_config, update_task_token_saving_config
from aha_cli.store.io import write_json
from aha_cli.store.knowledge import list_pending
from aha_cli.store.paths import config_path


def _make_run(tmp_path: Path, *, knowledge_enabled: bool = True) -> tuple[Path, str, Path]:
    home = tmp_path / ".aha"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    with mock.patch("pathlib.Path.cwd", return_value=workspace):
        assert main(["--home", str(home), "init", "--portable"]) == 0
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            assert main(["--home", str(home), "plan", "Context evidence flow", "--agents", "1"]) == 0
    run_id = next(line.split(": ", 1)[1] for line in out.getvalue().splitlines() if line.startswith("Created run: "))
    update_task_token_saving_config(home, run_id, "task-001", enabled=True, provider="nav")
    cfg = load_config(home)
    cfg["knowledge"]["enabled"] = knowledge_enabled
    cfg["knowledge"]["curation"]["gate"] = "manual"
    write_json(config_path(home), cfg)
    return home, run_id, workspace


def _prompt_metrics(home: Path, run_id: str, evidence: dict) -> tuple[dict, dict]:
    prompt_event = append_event(
        home,
        run_id,
        "agent_prompt_metrics",
        {"source": "codex-chat", "task_id": "task-001", "target": "main"},
    )
    metrics = {
        "prompt_ref": "tasks/task-001/prompts/main-001.txt",
        "context_pack_evidence": evidence,
    }
    record_context_pack_from_prompt_metrics(
        home,
        run_id,
        task_id="task-001",
        agent_id="main",
        source="codex-chat",
        user_message=evidence.get("request"),
        prompt_event=prompt_event,
        prompt_metrics=metrics,
    )
    return prompt_event, metrics


def test_context_evidence_missing_nav_records_task_scoped_update_without_candidate(tmp_path: Path):
    home, run_id, workspace = _make_run(tmp_path)
    (workspace / "drivers" / "net").mkdir(parents=True)
    (workspace / "src" / "aha_cli").mkdir(parents=True)
    (workspace / "drivers" / "net" / "foo.c").write_text("int foo_probe(void) { return 0; }\n", encoding="utf-8")
    (workspace / "src" / "aha_cli" / "new.py").write_text("def foo_probe():\n    return 1\n", encoding="utf-8")
    evidence = {
        "request": "where is foo_probe",
        "text_sha": "pack123",
        "knowledge": {"entries": []},
        "map": {"query": "foo_probe", "total_matches": 1, "files": ["drivers/net/foo.c"]},
    }
    prompt_event, metrics = _prompt_metrics(home, run_id, evidence)
    append_event(
        home,
        run_id,
        "agent_command_finished",
        {
            "task_id": "task-001",
            "target": "main",
            "command": "sed -n 1,40p src/aha_cli/new.py && rg foo_probe drivers/net/foo.c",
            "exit_code": 0,
        },
    )

    result = distill_context_evidence_after_turn(
        home,
        run_id,
        task_id="task-001",
        agent_id="main",
        source="codex-chat",
        prompt_event=prompt_event,
        prompt_metrics=metrics,
        reply="I found the real implementation in src/aha_cli/new.py.",
        exit_code=0,
        workspace=workspace,
    )

    assert result is not None
    assert result["candidate"] is None
    records = list_task_context_evidence(home, run_id, "task-001")
    assert [record["type"] for record in records] == ["context_pack", "context_evidence_result"]
    assert "context_hit_ok" in records[-1]["signals"]
    assert "missing_nav" in records[-1]["signals"]
    assert records[-1]["crud_actions"] == ["read", "create"]
    assert "src/aha_cli/new.py" in records[-1]["actual_files"]
    assert "src/aha_cli/new.py" in records[-1]["navigation_diagnostics"]["missing_files"]
    suggestions = records[-1]["maintenance_suggestions"]
    assert {
        (item["action"], item["target"], item["reason"])
        for item in suggestions
    } == {
        ("create", "project_navigation", "missing_nav"),
        ("update", "project_navigation", "missing_nav"),
    }
    assert "src/aha_cli/new.py" in suggestions[0]["files"]
    assert suggestions[0]["commands"] == ["sed -n 1,40p src/aha_cli/new.py && rg foo_probe drivers/net/foo.c"]
    plan = records[-1]["maintenance_plan"]
    nav_plan = next(item for item in plan if item["target"] == "project_navigation" and item["action"] == "update")
    assert nav_plan["target_path"] == "navigation/index.md"
    assert nav_plan["target_kind"] == "project_navigation"
    assert nav_plan["write_policy"] == "direct_project_navigation_update"
    assert nav_plan["execution"]["state"] == "ready"
    assert nav_plan["execution"]["mode"] == "direct_edit"
    assert "src/aha_cli/new.py" in nav_plan["source_files"]
    assert nav_plan["signals"] == ["missing_nav"]
    assert records[-1]["routing_health"]["status"] == "needs_repair"
    assert "src/aha_cli/new.py" in records[-1]["routing_health"]["prioritize_paths"]
    assert records[-1]["kb_scope_policy"]["general_personal_wiki"] == "manual_candidate_review_only"
    assert records[-1]["kb_growth_state"]["status"] == "pending"
    assert records[-1]["kb_growth_state"]["pending"][0]["target_path"] == "navigation/index.md"
    assert list_pending(home, load_config(home)) == []


def test_context_evidence_marks_kb_growth_applied_from_agent_feedback(tmp_path: Path):
    home, run_id, workspace = _make_run(tmp_path)
    (workspace / "src" / "aha_cli").mkdir(parents=True)
    (workspace / "src" / "aha_cli" / "new.py").write_text("def new_endpoint():\n    return True\n", encoding="utf-8")
    evidence = {
        "request": "where is new_endpoint",
        "text_sha": "pack-growth",
        "knowledge": {"entries": []},
        "map": {"query": "new_endpoint", "total_matches": 1, "files": ["src/aha_cli/old.py"]},
    }
    prompt_event, metrics = _prompt_metrics(home, run_id, evidence)
    record_agent_kb_feedback(
        home,
        run_id,
        "task-001",
        agent_id="main",
        feedback={"updated": ["navigation/index.md"]},
    )
    append_event(
        home,
        run_id,
        "agent_command_finished",
        {
            "task_id": "task-001",
            "target": "main",
            "command": "sed -n 1,40p src/aha_cli/new.py",
            "exit_code": 0,
        },
    )

    result = distill_context_evidence_after_turn(
        home,
        run_id,
        task_id="task-001",
        agent_id="main",
        source="codex-chat",
        prompt_event=prompt_event,
        prompt_metrics=metrics,
        reply="Updated navigation/index.md with the verified route.",
        exit_code=0,
        workspace=workspace,
    )

    assert result is not None
    record = list_task_context_evidence(home, run_id, "task-001")[-1]
    assert record["kb_growth_state"]["status"] == "applied"
    assert record["kb_growth_state"]["applied"][0]["matched_ref"] == "navigation/index.md"
    assert record["kb_growth_state"]["applied"][0]["source"] == "agent_kb_feedback"
    assert record["kb_growth_state"]["pending"] == []


def test_context_evidence_stale_reference_records_repair_deprecate_without_candidate(tmp_path: Path):
    home, run_id, workspace = _make_run(tmp_path)
    (workspace / "docs").mkdir(parents=True)
    (workspace / "docs" / "new-guide.md").write_text("# New guide\n", encoding="utf-8")
    evidence = {
        "request": "where is the guide",
        "text_sha": "pack456",
        "knowledge": {"entries": [{"title": "Old guide", "slug": "modules/docs"}]},
        "map": {"query": "guide", "total_matches": 1, "files": ["docs/old-guide.md"]},
    }
    prompt_event, metrics = _prompt_metrics(home, run_id, evidence)
    append_event(
        home,
        run_id,
        "agent_command_finished",
        {
            "task_id": "task-001",
            "target": "main",
            "command": "sed -n 1,40p docs/new-guide.md",
            "exit_code": 0,
        },
    )

    result = distill_context_evidence_after_turn(
        home,
        run_id,
        task_id="task-001",
        agent_id="main",
        source="codex-chat",
        prompt_event=prompt_event,
        prompt_metrics=metrics,
        reply="The old guide moved to docs/new-guide.md.",
        exit_code=0,
        workspace=workspace,
    )

    assert result is not None
    assert result["candidate"] is None
    record = list_task_context_evidence(home, run_id, "task-001")[-1]
    assert "nav_stale" in record["signals"]
    assert "missing_nav" in record["signals"]
    assert "create" in record["crud_actions"]
    assert "repair" in record["crud_actions"]
    assert "deprecate" in record["crud_actions"]
    assert "docs/old-guide.md" in record["stale_references"]
    suggestions = record["maintenance_suggestions"]
    suggestion_keys = {
        (item["action"], item["target"], item["reason"])
        for item in suggestions
    }
    assert ("create", "project_navigation", "missing_nav") in suggestion_keys
    assert ("update", "project_navigation", "missing_nav") in suggestion_keys
    assert ("repair", "project_navigation", "nav_stale") in suggestion_keys
    plan = record["maintenance_plan"]
    nav_plan = next(item for item in plan if item["target"] == "project_navigation")
    assert nav_plan["target_path"] == "navigation/index.md"
    assert record["routing_health"]["status"] == "stale"
    assert "docs/old-guide.md" in record["routing_health"]["downrank_paths"]
    assert record["navigation_diagnostics"]["gap_reasons"][0]["reason"] == "referenced_file_missing"
    assert list_pending(home, load_config(home)) == []


def test_context_evidence_missing_nav_when_navigation_index_absent(tmp_path: Path):
    home, run_id, workspace = _make_run(tmp_path)
    (workspace / "src" / "aha_cli").mkdir(parents=True)
    (workspace / "src" / "aha_cli" / "hidden.py").write_text("def hidden_endpoint():\n    return True\n", encoding="utf-8")
    evidence = {
        "request": "where is hidden_endpoint",
        "text_sha": "pack-missing-nav",
        "knowledge": {"entries": [], "navigation_index_exists": False},
    }
    prompt_event, metrics = _prompt_metrics(home, run_id, evidence)
    append_event(
        home,
        run_id,
        "agent_command_finished",
        {
            "task_id": "task-001",
            "target": "main",
            "command": "sed -n 1,40p src/aha_cli/hidden.py",
            "exit_code": 0,
        },
    )

    result = distill_context_evidence_after_turn(
        home,
        run_id,
        task_id="task-001",
        agent_id="main",
        source="codex-chat",
        prompt_event=prompt_event,
        prompt_metrics=metrics,
        reply="The navigation index was absent, but source search found src/aha_cli/hidden.py.",
        exit_code=0,
        workspace=workspace,
    )

    assert result is not None
    record = list_task_context_evidence(home, run_id, "task-001")[-1]
    assert record["signals"] == ["missing_nav"]
    assert record["crud_actions"] == ["create"]
    assert "src/aha_cli/hidden.py" in record["navigation_diagnostics"]["missing_files"]
    suggestions = record["maintenance_suggestions"]
    assert {
        (item["action"], item["target"], item["reason"])
        for item in suggestions
    } == {
        ("create", "project_navigation", "missing_nav"),
    }
    plan = record["maintenance_plan"]
    nav_plan = next(item for item in plan if item["target"] == "project_navigation")
    assert nav_plan["target_path"] == "navigation/index.md"
    assert nav_plan["signals"] == ["missing_nav"]
    assert record["routing_health"]["status"] == "needs_repair"
    assert list_pending(home, load_config(home)) == []


def test_context_evidence_hit_only_records_without_pending_candidate(tmp_path: Path):
    home, run_id, workspace = _make_run(tmp_path)
    (workspace / "drivers" / "net").mkdir(parents=True)
    (workspace / "drivers" / "net" / "foo.c").write_text("int foo_probe(void) { return 0; }\n", encoding="utf-8")
    evidence = {
        "request": "where is foo_probe",
        "text_sha": "pack789",
        "knowledge": {"entries": []},
        "map": {"query": "foo_probe", "total_matches": 1, "files": ["drivers/net/foo.c"]},
    }
    prompt_event, metrics = _prompt_metrics(home, run_id, evidence)
    append_event(
        home,
        run_id,
        "agent_command_finished",
        {
            "task_id": "task-001",
            "target": "main",
            "command": "sed -n 1,40p drivers/net/foo.c",
            "exit_code": 0,
        },
    )

    result = distill_context_evidence_after_turn(
        home,
        run_id,
        task_id="task-001",
        agent_id="main",
        source="codex-chat",
        prompt_event=prompt_event,
        prompt_metrics=metrics,
        reply="The Context Pack was correct.",
        exit_code=0,
        workspace=workspace,
    )

    assert result is not None
    assert result["candidate"] is None
    records = list_task_context_evidence(home, run_id, "task-001")
    assert records[-1]["signals"] == ["context_hit_ok"]
    assert records[-1]["crud_actions"] == ["read"]
    assert records[-1]["maintenance_suggestions"] == []
    assert records[-1]["maintenance_plan"] == []
    assert records[-1]["routing_health"]["status"] == "healthy"
    assert list_pending(home, load_config(home)) == []


def test_context_evidence_entrypoint_only_pack_does_not_emit_false_missing_nav(tmp_path: Path):
    home, run_id, workspace = _make_run(tmp_path)
    (workspace / "src" / "aha_cli").mkdir(parents=True)
    (workspace / "src" / "aha_cli" / "context_planner.py").write_text("def build():\n    return True\n", encoding="utf-8")
    evidence = {
        "request": "inspect token saving planner",
        "text_sha": "pack-entrypoint-only",
        "knowledge": {
            "mode": "agent_pull",
            "navigation_index": "projects/demo/navigation/index.md",
            "navigation_index_exists": True,
            "entries": [],
        },
        "map": {"mode": "agent_pull", "status": "fresh", "files": []},
    }
    prompt_event, metrics = _prompt_metrics(home, run_id, evidence)
    append_event(
        home,
        run_id,
        "agent_command_finished",
        {
            "task_id": "task-001",
            "target": "main",
            "command": "sed -n 1,40p src/aha_cli/context_planner.py",
            "exit_code": 0,
        },
    )

    result = distill_context_evidence_after_turn(
        home,
        run_id,
        task_id="task-001",
        agent_id="main",
        source="codex-chat",
        prompt_event=prompt_event,
        prompt_metrics=metrics,
        reply="I inspected the planner entrypoint from the pull contract.",
        exit_code=0,
        workspace=workspace,
    )

    assert result is not None
    record = list_task_context_evidence(home, run_id, "task-001")[-1]
    assert record["signals"] == []
    assert record["crud_actions"] == []
    assert record["maintenance_suggestions"] == []
    assert record["maintenance_plan"] == []
    assert record["routing_health"]["status"] == "unobserved"
    assert "src/aha_cli/context_planner.py" in record["actual_files"]
    assert "missing_nav" not in record["signals"]
    assert "missing_entry" not in record["signals"]
    assert list_pending(home, load_config(home)) == []


def test_context_evidence_filters_command_path_noise_and_keeps_kb_paths_separate(tmp_path: Path):
    home, run_id, workspace = _make_run(tmp_path)
    (workspace / "src" / "aha_cli").mkdir(parents=True)
    (workspace / "src" / "aha_cli" / "panel.py").write_text("def render():\n    return True\n", encoding="utf-8")
    kb_path = home / "knowledge" / "projects" / "demo" / "navigation" / "index.md"
    kb_path.parent.mkdir(parents=True)
    kb_path.write_text("# Demo nav\n", encoding="utf-8")
    evidence = {
        "request": "inspect evidence panel",
        "text_sha": "pack-noise",
        "knowledge": {"mode": "agent_pull", "navigation_index_exists": True, "entries": []},
        "map": {"query": "evidence panel", "status": "fresh", "total_matches": 0, "files": []},
    }
    prompt_event, metrics = _prompt_metrics(home, run_id, evidence)
    append_event(
        home,
        run_id,
        "agent_command_finished",
        {
            "task_id": "task-001",
            "target": "main",
            "command": f"/bin/bash -lc 'sed -n 1,40p src/aha_cli/panel.py {workspace / 'src' / 'aha_cli' / 'panel.py'} {kb_path} tmp/noise'",
            "exit_code": 0,
        },
    )

    result = distill_context_evidence_after_turn(
        home,
        run_id,
        task_id="task-001",
        agent_id="main",
        source="codex-chat",
        prompt_event=prompt_event,
        prompt_metrics=metrics,
        reply="Only src/aha_cli/panel.py is task source; the KB path is reference material.",
        exit_code=0,
        workspace=workspace,
    )

    assert result is not None
    record = list_task_context_evidence(home, run_id, "task-001")[-1]
    assert record["actual_files"] == ["src/aha_cli/panel.py"]
    assert "src/aha_cli/panel.py" in record["navigation_diagnostics"]["missing_files"]
    assert "bin/bash" not in record["actual_files"]
    assert "tmp/noise" not in record["actual_files"]
    assert "tmp/noise" not in record["navigation_diagnostics"]["missing_files"]
    assert any(path.endswith(".aha/knowledge/projects/demo/navigation/index.md") for path in record["knowledge_files"])
    assert "bin/bash" in record["ignored_command_paths"]


def test_context_evidence_read_side_cleans_historical_path_noise_without_rewriting_jsonl(tmp_path: Path):
    home, run_id, workspace = _make_run(tmp_path)
    source = workspace / "src" / "aha_cli" / "panel.py"
    source.parent.mkdir(parents=True)
    source.write_text("def render():\n    return True\n", encoding="utf-8")
    kb_path = home / "knowledge" / "projects" / "demo" / "navigation" / "index.md"
    kb_path.parent.mkdir(parents=True)
    kb_path.write_text("# Demo nav\n", encoding="utf-8")
    noisy_paths = ["/bin/bash", "tmp/noise", str(kb_path), str(source)]
    append_task_context_evidence(
        home,
        run_id,
        "task-001",
        {
            "type": "context_evidence_result",
            "agent_id": "main",
            "signals": ["missing_nav"],
            "actual_files": noisy_paths,
            "navigation_diagnostics": {
                "actual_files": noisy_paths,
                "missing_files": noisy_paths,
                "gap_reasons": [{"reason": "navigation_referenced_wrong_files", "paths": noisy_paths}],
            },
            "routing_health": {
                "status": "needs_repair",
                "prioritize_paths": noisy_paths,
                "score_adjustments": [
                    {"path": path, "direction": "prioritize", "reason": "verified_task_source"}
                    for path in noisy_paths
                ],
            },
            "maintenance_suggestions": [
                {"action": "update", "target": "project_navigation", "reason": "missing_nav", "files": noisy_paths}
            ],
            "maintenance_plan": [
                {
                    "action": "update",
                    "target": "project_navigation",
                    "reason": "missing_nav",
                    "files": noisy_paths,
                    "source_files": noisy_paths,
                }
            ],
        },
    )

    raw = task_context_evidence_path(home, run_id, "task-001").read_text(encoding="utf-8")
    record = list_task_context_evidence(home, run_id, "task-001")[-1]

    assert "tmp/noise" in raw
    assert str(kb_path) in raw
    assert record["actual_files"] == ["src/aha_cli/panel.py"]
    assert record["navigation_diagnostics"]["missing_files"] == ["src/aha_cli/panel.py"]
    assert record["navigation_diagnostics"]["gap_reasons"][0]["paths"] == ["src/aha_cli/panel.py"]
    assert record["routing_health"]["prioritize_paths"] == ["src/aha_cli/panel.py"]
    assert [item["path"] for item in record["routing_health"]["score_adjustments"]] == ["src/aha_cli/panel.py"]
    assert record["maintenance_suggestions"][0]["files"] == ["src/aha_cli/panel.py"]
    assert record["maintenance_plan"][0]["source_files"] == ["src/aha_cli/panel.py"]
    assert any(path.endswith(".aha/knowledge/projects/demo/navigation/index.md") for path in record["knowledge_files"])
    assert "bin/bash" in record["ignored_command_paths"]
    assert "tmp/noise" in record["ignored_command_paths"]
