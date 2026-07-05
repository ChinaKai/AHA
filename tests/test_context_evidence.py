from __future__ import annotations

import contextlib
import io
from pathlib import Path
from unittest import mock

from aha_cli.cli import main
from aha_cli.services.context_evidence import (
    distill_context_evidence_after_turn,
    list_task_context_evidence,
    record_context_pack_from_prompt_metrics,
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
    update_task_token_saving_config(home, run_id, "task-001", enabled=True, provider="map")
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


def test_context_evidence_map_miss_records_task_scoped_update_without_candidate(tmp_path: Path):
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
    assert "map_miss" in records[-1]["signals"]
    assert "map_coverage_gap" in records[-1]["signals"]
    assert "update" in records[-1]["crud_actions"]
    assert "repair" in records[-1]["crud_actions"]
    assert "src/aha_cli/new.py" in records[-1]["actual_files"]
    assert records[-1]["map_diagnostics"]["gap_signals"] == ["map_coverage_gap"]
    assert "src/aha_cli/new.py" in records[-1]["map_diagnostics"]["missing_files"]
    assert list_pending(home, load_config(home)) == []


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
    assert "map_stale_cache" in record["signals"]
    assert "repair" in record["crud_actions"]
    assert "refresh" in record["crud_actions"]
    assert "deprecate" in record["crud_actions"]
    assert "docs/old-guide.md" in record["stale_references"]
    assert "map_stale_cache" in record["map_diagnostics"]["gap_signals"]
    assert list_pending(home, load_config(home)) == []


def test_context_evidence_map_empty_query_records_extractor_and_query_gaps(tmp_path: Path):
    home, run_id, workspace = _make_run(tmp_path)
    (workspace / "src" / "aha_cli").mkdir(parents=True)
    (workspace / "src" / "aha_cli" / "hidden.py").write_text("def hidden_endpoint():\n    return True\n", encoding="utf-8")
    evidence = {
        "request": "where is hidden_endpoint",
        "text_sha": "pack-empty-map",
        "knowledge": {"entries": []},
        "map": {"query": "hidden endpoint", "status": "fresh", "total_matches": 0, "files": []},
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
        reply="The map query had no result, but rg found src/aha_cli/hidden.py.",
        exit_code=0,
        workspace=workspace,
    )

    assert result is not None
    record = list_task_context_evidence(home, run_id, "task-001")[-1]
    assert "missing_nav" in record["signals"]
    assert "map_extractor_gap" in record["signals"]
    assert "map_query_expansion_gap" in record["signals"]
    assert "update" in record["crud_actions"]
    assert "repair" in record["crud_actions"]
    assert record["map_diagnostics"]["query"] == "hidden endpoint"
    assert record["map_diagnostics"]["total_matches"] == 0
    assert record["map_diagnostics"]["gap_signals"] == ["map_extractor_gap", "map_query_expansion_gap"]
    assert "src/aha_cli/hidden.py" in record["map_diagnostics"]["missing_files"]
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
    assert list_pending(home, load_config(home)) == []
