from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from aha_cli.domain.models import default_knowledge_config
from aha_cli.services import knowledge_git as kg
from aha_cli.services import knowledge_maintenance as km
from aha_cli.services import knowledge_sync_loop as ksl
from aha_cli.store.io import write_json
from aha_cli.store.knowledge import knowledge_root, write_entry
from aha_cli.store.paths import config_path

pytestmark = pytest.mark.skipif(not kg.git_available(), reason="git not available")


def _config(remote: str | None = None, **git_overrides) -> dict:
    kb = default_knowledge_config()
    kb["enabled"] = True
    kb["git"]["enabled"] = True
    kb["git"]["auto_push"] = True
    if remote is not None:
        kb["git"]["remote"] = remote
    kb["git"].update(git_overrides)
    kb["sync"]["resolve_conflicts"] = "agent"
    return {"knowledge": kb}


def _bare_remote(path: Path) -> str:
    subprocess.run(["git", "init", "--bare", "-b", "main", str(path)], check=True, capture_output=True)
    return str(path)


def _git(repo: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@t", *args],
        check=True,
        capture_output=True,
        text=True,
    )
    return proc.stdout.strip()


def _diverged_repo(tmp_path: Path, *, remote_is_agent: bool = True) -> tuple[Path, dict, Path, Path]:
    """Create a repo whose remote and local both edit the same entry.

    Returns (root, config, repo, local_entry_path). The remote edit is an
    agent-distilled entry (unless ``remote_is_agent``), the local edit is a
    plain user edit. A ``sync`` in agent mode leaves a rebase conflict.
    """
    root = tmp_path / ".aha"
    remote = _bare_remote(tmp_path / "remote.git")
    cfg = _config(remote)
    # Persist so config-reading paths (scheduled loop, web routes) see the remote.
    write_json(config_path(root), {"knowledge": cfg["knowledge"]})
    repo = knowledge_root(root, cfg)
    kg.commit_all(root, "init", cfg)
    kg.push(root, cfg)

    other = tmp_path / "other"
    subprocess.run(["git", "clone", remote, str(other)], check=True, capture_output=True)
    remote_entry = other / "general" / "wiki" / "concept.md"
    remote_entry.parent.mkdir(parents=True, exist_ok=True)
    if remote_is_agent:
        remote_entry.write_text("---\ndistilled_by: heuristic\n---\nAGENT REMOTE VERSION\n", encoding="utf-8")
    else:
        remote_entry.write_text("USER REMOTE VERSION\n", encoding="utf-8")
    _git(other, "add", "-A")
    _git(other, "commit", "-m", "remote change")
    _git(other, "push", "origin", "main")

    local_entry = repo / "general" / "wiki" / "concept.md"
    local_entry.parent.mkdir(parents=True, exist_ok=True)
    local_entry.write_text("USER LOCAL VERSION\n", encoding="utf-8")
    kg.commit_all(root, "local user edit", cfg)
    return root, cfg, repo, local_entry


# --------------------------------------------------------------------------- #
# Conflict detection
# --------------------------------------------------------------------------- #
def test_sync_status_reports_conflict_state(tmp_path: Path):
    root, cfg, repo, local_entry = _diverged_repo(tmp_path)
    result = kg.sync(root, cfg, message="manual sync")
    assert result.get("conflict") is True
    assert result["unmerged"] == ["general/wiki/concept.md"]

    status = kg.sync_status(root, cfg)
    assert status["state"] == "conflict"
    assert status["unmerged"] == ["general/wiki/concept.md"]
    assert status["conflict_files"] == ["general/wiki/concept.md"]
    assert status["rebase_in_progress"] is True


def test_pull_default_aborts_but_agent_mode_keeps_rebase(tmp_path: Path):
    root, cfg, repo, local_entry = _diverged_repo(tmp_path)
    local_head = _git(repo, "rev-parse", "HEAD")

    # Default pull aborts on conflict (historic behavior).
    aborted = kg.pull(root, cfg)
    assert aborted["ok"] is False and aborted["conflict"] is True
    assert kg.rebase_in_progress(repo) is False
    assert _git(repo, "rev-parse", "HEAD") == local_head

    # Re-create the divergence, then agent-mode pull keeps the rebase in progress.
    local_entry.write_text("USER LOCAL VERSION 2\n", encoding="utf-8")
    kg.commit_all(root, "local user edit 2", cfg)
    kept = kg.pull(root, cfg, keep_rebase_on_conflict=True)
    assert kept["ok"] is False and kept["conflict"] is True
    assert kept.get("rebase_in_progress") is True
    assert kg.rebase_in_progress(repo) is True
    assert kg.unmerged_paths(repo) == ["general/wiki/concept.md"]


def test_conflict_detail_exposes_local_remote_base_and_agent_flags(tmp_path: Path):
    root, cfg, repo, local_entry = _diverged_repo(tmp_path, remote_is_agent=True)
    kg.sync(root, cfg, message="manual sync")

    detail = kg.conflict_detail(root, cfg)
    assert detail["ok"] is True
    conflicts = detail["conflicts"]
    assert len(conflicts) == 1
    conflict = conflicts[0]
    assert conflict["path"] == "general/wiki/concept.md"
    assert "AGENT REMOTE VERSION" in conflict["remote"]
    assert "USER LOCAL VERSION" in conflict["local"]
    assert conflict["remote_agent"] is True
    assert conflict["local_agent"] is False


# --------------------------------------------------------------------------- #
# Resolution
# --------------------------------------------------------------------------- #
def test_resolve_unmerged_takes_local(tmp_path: Path):
    root, cfg, repo, local_entry = _diverged_repo(tmp_path)
    kg.sync(root, cfg, message="manual sync")
    resolved = kg.resolve_unmerged(root, cfg, decisions={"general/wiki/concept.md": {"action": "local"}})
    assert resolved["ok"] is True
    assert resolved["resolved"] == ["general/wiki/concept.md"]
    assert "USER LOCAL VERSION" in local_entry.read_text()
    # Still mid-rebase until we continue.
    assert kg.rebase_in_progress(repo) is True


def test_resolve_unmerged_takes_remote(tmp_path: Path):
    root, cfg, repo, local_entry = _diverged_repo(tmp_path)
    kg.sync(root, cfg, message="manual sync")
    resolved = kg.resolve_unmerged(root, cfg, decisions={"general/wiki/concept.md": {"action": "remote"}})
    assert resolved["ok"] is True
    assert "AGENT REMOTE VERSION" in local_entry.read_text()


def test_resolve_unmerged_default_user_priority_prefers_user_side(tmp_path: Path):
    root, cfg, repo, local_entry = _diverged_repo(tmp_path, remote_is_agent=True)
    kg.sync(root, cfg, message="manual sync")
    # No decisions: user-priority default keeps the user's local version over the agent's remote.
    resolved = kg.resolve_unmerged(root, cfg)
    assert resolved["ok"] is True
    assert "USER LOCAL VERSION" in local_entry.read_text()


def test_rebase_continue_finishes_rebase_and_push(tmp_path: Path):
    root, cfg, repo, local_entry = _diverged_repo(tmp_path)
    kg.sync(root, cfg, message="manual sync")
    kg.resolve_unmerged(root, cfg, decisions={"general/wiki/concept.md": {"action": "local"}})
    cont = kg.rebase_continue(root, cfg)
    assert cont["ok"] is True and cont["continued"] is True
    assert kg.rebase_in_progress(repo) is False
    assert kg.unmerged_paths(repo) == []
    assert _git(repo, "status", "--porcelain") == ""
    # Remote now has the resolved commit after a push.
    kg.push(root, cfg)
    other = tmp_path / "other"
    _git(other, "fetch", "origin")
    assert _git(other, "rev-parse", "origin/main") == _git(repo, "rev-parse", "HEAD")


def test_default_resolutions_covers_every_unmerged_path(tmp_path: Path):
    root, cfg, repo, local_entry = _diverged_repo(tmp_path, remote_is_agent=True)
    kg.sync(root, cfg, message="manual sync")
    decisions = kg.default_resolutions(root, cfg)
    assert decisions == {"general/wiki/concept.md": {"action": "local"}}


# --------------------------------------------------------------------------- #
# Maintenance job
# --------------------------------------------------------------------------- #
def _stub_agent(plan: str | None = None):
    def agent(context: dict) -> str:
        return plan if plan is not None else "no plan"
    return agent


def test_maintenance_job_resolves_via_agent_plan(tmp_path: Path):
    root, cfg, repo, local_entry = _diverged_repo(tmp_path)
    kg.sync(root, cfg, message="manual sync")
    plan = json.dumps([{"path": "general/wiki/concept.md", "action": "merge", "content": "MERGED BY AGENT\n"}])
    record = km.run_kb_maintenance_job(root, cfg, agent=_stub_agent(plan))
    assert record["status"] == "resolved"
    assert record["fallback_used"] is False
    assert record["pushed"] is True
    assert record["resolutions"]["general/wiki/concept.md"] == "merge"
    assert local_entry.read_text() == "MERGED BY AGENT\n"
    assert kg.rebase_in_progress(repo) is False
    assert kg.unmerged_paths(repo) == []
    # Persisted state exposes the record.
    assert km.maintenance_record(root)["status"] == "resolved"


def test_maintenance_job_passes_saved_agent_profile_to_agent(tmp_path: Path):
    root, cfg, repo, local_entry = _diverged_repo(tmp_path)
    cfg["knowledge"]["agent"] = {
        "backend": "claude",
        "model": "claude-sonnet-4-6",
        "reasoning_effort": "high",
        "proxy_enabled": True,
    }
    kg.sync(root, cfg, message="manual sync")
    contexts: list[dict] = []

    def agent(context: dict) -> str:
        contexts.append(context)
        return json.dumps([{"path": "general/wiki/concept.md", "action": "merge", "content": "PROFILE MERGE\n"}])

    record = km.run_kb_maintenance_job(root, cfg, agent=agent, do_push=False)

    assert record["status"] == "resolved"
    assert len(contexts) == 1
    assert contexts[0]["backend"] == "claude"
    assert contexts[0]["model"] == "claude-sonnet-4-6"
    assert contexts[0]["reasoning_effort"] == "high"
    assert contexts[0]["proxy_enabled"] is True
    assert local_entry.read_text(encoding="utf-8") == "PROFILE MERGE\n"


def test_maintenance_job_falls_back_to_user_priority_when_agent_empty(tmp_path: Path):
    root, cfg, repo, local_entry = _diverged_repo(tmp_path, remote_is_agent=True)
    kg.sync(root, cfg, message="manual sync")
    record = km.run_kb_maintenance_job(root, cfg, agent=_stub_agent(None))
    assert record["status"] == "resolved"
    assert record["fallback_used"] is True
    # User-priority: the local user version beats the remote agent version.
    assert "USER LOCAL VERSION" in local_entry.read_text()
    assert record["resolutions"]["general/wiki/concept.md"] == "local"


def test_maintenance_job_preserves_user_user_conflict_when_agent_unavailable(tmp_path: Path):
    root, cfg, repo, local_entry = _diverged_repo(tmp_path, remote_is_agent=False)
    local_head = _git(repo, "rev-parse", "HEAD")
    kg.sync(root, cfg, message="manual sync")

    record = km.run_kb_maintenance_job(root, cfg, agent=_stub_agent(None))

    assert record["status"] == "failed"
    assert record["fallback_used"] is True
    assert record["pushed"] is False
    assert record["resolutions"] == {}
    assert "unresolved user-owned conflicts" in record["error"]
    assert "用户双端冲突已保留" in record["summary"]
    assert kg.rebase_in_progress(repo) is False
    assert kg.unmerged_paths(repo) == []
    assert _git(repo, "rev-parse", "HEAD") == local_head
    assert local_entry.read_text(encoding="utf-8") == "USER LOCAL VERSION\n"


def test_maintenance_job_archive_preserves_local_outside_repo(tmp_path: Path):
    root, cfg, repo, local_entry = _diverged_repo(tmp_path)
    kg.sync(root, cfg, message="manual sync")
    plan = json.dumps([{"path": "general/wiki/concept.md", "action": "archive"}])
    record = km.run_kb_maintenance_job(root, cfg, agent=_stub_agent(plan))
    assert record["status"] == "resolved"
    archive = root / "conflicts" / "general" / "wiki" / "concept.md.local.md"
    assert archive.exists()
    assert "USER LOCAL VERSION" in archive.read_text()
    assert "AGENT REMOTE VERSION" in local_entry.read_text()


def test_maintenance_job_noop_when_clean(tmp_path: Path):
    root = tmp_path / ".aha"
    cfg = _config()
    repo = knowledge_root(root, cfg)
    kg.commit_all(root, "init", cfg)
    record = km.run_kb_maintenance_job(root, cfg, agent=_stub_agent())
    assert record["status"] == "resolved"
    assert "No conflicts" in record["summary"]


def test_maintenance_job_reports_failed_when_conflict_resolution_push_fails(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    root, cfg, repo, local_entry = _diverged_repo(tmp_path)
    kg.sync(root, cfg, message="manual sync")
    monkeypatch.setattr(km, "push", lambda *_args, **_kwargs: {"ok": False, "pushed": False, "error": "network down"})

    record = km.run_kb_maintenance_job(root, cfg, agent=_stub_agent(None))

    assert record["status"] == "failed"
    assert record["pushed"] is False
    assert record["summary"] == "冲突已解决，但推送到远端失败。"
    assert "network down" in record["error"]
    assert kg.rebase_in_progress(repo) is False
    assert kg.unmerged_paths(repo) == []


def test_parse_resolution_plan_extracts_json_from_prose(tmp_path: Path):
    reply = "Here is my plan:\n```json\n[{\"path\": \"a.md\", \"action\": \"local\"}]\n```\nDone."
    assert km.parse_resolution_plan(reply) == [{"path": "a.md", "action": "local"}]
    assert km.parse_resolution_plan("no json here") == []
    single = '{"path": "a.md", "action": "remote"}'
    assert km.parse_resolution_plan(single) == [{"path": "a.md", "action": "remote"}]


def test_normalize_decisions_maps_legacy_actions(tmp_path: Path):
    decisions = km.normalize_decisions([{"path": "a", "action": "ours"}, {"path": "b", "action": "theirs"}])
    assert decisions["a"] == {"action": "local"}
    assert decisions["b"] == {"action": "remote"}


# --------------------------------------------------------------------------- #
# Scheduled sync loop
# --------------------------------------------------------------------------- #
def test_sync_lock_is_single_flight(tmp_path: Path):
    root = tmp_path / ".aha"
    assert ksl._try_acquire_sync_lock(root) is True
    assert ksl._try_acquire_sync_lock(root) is False
    ksl._release_sync_lock(root)
    assert ksl._try_acquire_sync_lock(root) is True
    ksl._release_sync_lock(root)


def test_scheduled_sync_no_remote_is_noop(tmp_path: Path):
    root = tmp_path / ".aha"
    cfg = _config()  # no remote
    repo = knowledge_root(root, cfg)
    kg.commit_all(root, "init", cfg)
    ksl._run_scheduled_sync(root)
    state = km.read_sync_state(root)
    assert state["loop"]["last_sync_ok"] is True


@pytest.mark.parametrize("mode", ["manual", "off"])
def test_scheduled_sync_skips_non_auto_modes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str):
    root = tmp_path / ".aha"
    cfg = _config()
    cfg["knowledge"]["sync"]["mode"] = mode
    write_json(config_path(root), cfg)
    calls: list[bool] = []
    monkeypatch.setattr(ksl, "knowledge_sync", lambda *_args, **_kwargs: calls.append(True))

    ksl._run_scheduled_sync(root)

    assert calls == []


def test_scheduled_auto_sync_always_pushes_and_dispatches_external_failures(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    root = tmp_path / ".aha"
    cfg = _config("git@example.com:kb.git", auto_push=False)
    write_json(config_path(root), cfg)
    calls: list[dict] = []
    dispatched: list[dict] = []

    def fake_sync(_root, _cfg, **kwargs):
        calls.append(kwargs)
        return {"ok": False, "steps": {"push": {"ok": False, "error": "network timed out"}}}

    monkeypatch.setattr(ksl, "knowledge_sync", fake_sync)
    monkeypatch.setattr(ksl, "dispatch_maintenance_job", lambda _root, _cfg, **kwargs: dispatched.append(kwargs))
    ksl._run_scheduled_sync(root)

    assert calls[0]["do_pull"] is True
    assert calls[0]["do_push"] is True
    assert dispatched[0]["source"] == "scheduled"
    assert dispatched[0]["sync_result"]["ok"] is False


def test_scheduled_sync_conflict_dispatches_maintenance(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    root, cfg, repo, local_entry = _diverged_repo(tmp_path)
    dispatched: list[tuple[Path, dict]] = []

    def fake_dispatch(r, c):
        dispatched.append((r, c))

    # The loop holds a module-level import of dispatch_maintenance_job.
    monkeypatch.setattr(ksl, "dispatch_maintenance_job", fake_dispatch)
    # Run the scheduled sync: mode is auto, KB enabled, so it pulls and hits the conflict.
    ksl._run_scheduled_sync(root)
    assert len(dispatched) == 1
    assert dispatched[0][0] == root
    state = km.read_sync_state(root)
    assert state["loop"]["last_sync_state"] == "conflict"
    assert state["loop"]["last_unmerged"] == ["general/wiki/concept.md"]


def test_scheduled_sync_resolves_conflict_end_to_end(tmp_path: Path):
    root, cfg, repo, local_entry = _diverged_repo(tmp_path)
    kg.sync(root, cfg, message="manual sync")
    record = km.run_kb_maintenance_job(root, cfg, agent=_stub_agent(None))
    assert record["status"] == "resolved"
    assert kg.sync_status(root, cfg)["state"] in {"clean", "ahead"}


# --------------------------------------------------------------------------- #
# Routes
# --------------------------------------------------------------------------- #
def test_sync_status_route_includes_maintenance_and_loop(tmp_path: Path):
    from aha_cli.domain.models import default_knowledge_config
    from aha_cli.store.config import load_config
    from aha_cli.store.io import write_json
    from aha_cli.store.paths import config_path
    from aha_cli.store.knowledge import init_knowledge_base
    from aha_cli.web.knowledge_routes import knowledge_route_response
    from tests.helpers import json_response_body

    home = tmp_path / ".aha"
    kb = default_knowledge_config()
    kb["enabled"] = True
    write_json(config_path(home), {"knowledge": kb})
    init_knowledge_base(home, {"knowledge": kb})
    km.write_sync_state(home, {"loop": {"last_sync_state": "clean", "interval_minutes": 60}})

    response = json_response_body(knowledge_route_response(home, "GET", "/api/kb/sync-status", {}, b"", {}))
    assert "maintenance" in response
    assert response["maintenance"]["status"] == "idle"
    assert response["sync_loop"]["last_sync_state"] == "clean"
    assert response["sync_loop"]["interval_minutes"] == 60


def test_sync_route_resolve_dispatches_maintenance(tmp_path: Path):
    from aha_cli.web.knowledge_routes import knowledge_route_response
    from tests.helpers import json_response_body

    root, cfg, repo, local_entry = _diverged_repo(tmp_path)
    # The route itself triggers the sync that hits the agent-mode conflict.
    dispatched: list[tuple[Path, dict]] = []

    def fake_dispatch(r, c):
        dispatched.append((r, c))
        # Mirror the real dispatcher: record the running job so the route's
        # maintenance_record() lookup sees it.
        state = km.read_sync_state(r)
        state["maintenance"] = {"status": "running", "conflict_files": []}
        km.write_sync_state(r, state)

    # The route imports dispatch_maintenance_job at call time, so patch the
    # source module attribute.
    original = km.dispatch_maintenance_job
    km.dispatch_maintenance_job = fake_dispatch
    try:
        body = json.dumps({"resolve": True, "pull": True, "push": True}).encode()
        response = json_response_body(knowledge_route_response(root, "POST", "/api/kb/sync", {}, body, {}))
    finally:
        km.dispatch_maintenance_job = original
    assert response["sync"]["conflict"] is True
    assert response["maintenance"]["status"] == "running"
    assert len(dispatched) == 1
