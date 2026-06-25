from __future__ import annotations

import contextlib
import io
import json
from pathlib import Path

import pytest

from aha_cli.cli import main
from aha_cli.domain.models import default_knowledge_config
from aha_cli.services.knowledge_capture_distill import distill_note
from aha_cli.store.io import write_json
from aha_cli.store.knowledge import (
    approve_candidate,
    entry_dir,
    init_knowledge_base,
    knowledge_root,
    list_pending,
    read_entry,
    write_entry,
)
from aha_cli.store.knowledge_capture import promote_assets_for_entry
from aha_cli.store import knowledge_capture as cap_store
from aha_cli.store.knowledge_capture import (
    CAPTURE_DIR,
    CAPTURE_DISTILL_DIR,
    LEGACY_CAPTURE_DIR,
    ImageRejected,
    add_note_image,
    create_note,
    capture_dir,
    delete_note,
    list_distill_logs,
    list_notes,
    read_distill_log,
    read_note,
    read_note_image,
    remove_note_image,
    sniff_image_mime,
    update_note,
)

_PNG = b"\x89PNG\r\n\x1a\n" + b"fake-png-body"
from aha_cli.store.paths import config_path


def _sidecar_reply(candidates_json: str) -> str:
    return f"整理完成。\n<aha_knowledge_candidates>{candidates_json}</aha_knowledge_candidates>"


def _stub(reply: str):
    return lambda ctx: reply


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
def test_capture_crud_roundtrip(tmp_path: Path):
    home = _home(tmp_path)
    cfg = _cfg()
    note = create_note(home, cfg, text="messy idea about retries", scope_hint="personal")
    assert note["status"] == "raw" and note["scope_hint"] == "personal"
    assert note["candidate_ids"] == []

    assert len(list_notes(home, cfg)) == 1
    fetched = read_note(home, cfg, note["id"])
    assert fetched["text"] == "messy idea about retries"

    updated = update_note(home, cfg, note["id"], text="cleaned up idea", scope_hint="general")
    assert updated["text"] == "cleaned up idea" and updated["scope_hint"] == "general"
    assert updated["created_at"] == note["created_at"]  # id/created_at preserved

    assert delete_note(home, cfg, note["id"]) is True
    assert list_notes(home, cfg) == []
    assert read_note(home, cfg, note["id"]) is None


def test_capture_invalid_scope_hint_falls_back_to_personal(tmp_path: Path):
    home = _home(tmp_path)
    cfg = _cfg()
    note = create_note(home, cfg, text="x", scope_hint="bogus")
    assert note["scope_hint"] == "personal"


def test_capture_assets_are_syncable_and_distill_logs_are_ignored(tmp_path: Path):
    home = _home(tmp_path)
    cfg = _cfg()
    note = create_note(home, cfg, text="raw user material")
    stored = read_note(home, cfg, note["id"])
    assert capture_dir(home, cfg).name == CAPTURE_DIR
    assert Path(stored["_path"]).parent == capture_dir(home, cfg)

    kb_root = home / "knowledge"
    gitignore = (kb_root / ".gitignore").read_text(encoding="utf-8").splitlines()
    assert f"{CAPTURE_DIR}/" not in gitignore
    assert f"{CAPTURE_DIR}/{CAPTURE_DISTILL_DIR}/" in gitignore
    assert f"{LEGACY_CAPTURE_DIR}/" not in gitignore
    assert ".pending/" in gitignore  # existing exclusion preserved


def test_legacy_capture_dir_migrates_to_syncable_capture(tmp_path: Path):
    home = _home(tmp_path)
    cfg = _cfg()
    kb_root = knowledge_root(home, cfg)
    legacy = kb_root / LEGACY_CAPTURE_DIR
    asset_dir = legacy / "assets" / "cap_old"
    asset_dir.mkdir(parents=True)
    (asset_dir / "a.png").write_bytes(_PNG)
    write_json(
        legacy / "cap_old.json",
        {
            "id": "cap_old",
            "title": "old",
            "text": "legacy note",
            "scope_hint": "personal",
            "images": [
                {
                    "name": "a.png",
                    "original": "a.png",
                    "mime": "image/png",
                    "size": len(_PNG),
                    "path": f"{LEGACY_CAPTURE_DIR}/assets/cap_old/a.png",
                }
            ],
            "status": "raw",
            "candidate_ids": [],
            "created_at": "2026-01-01T00:00:00+00:00",
            "updated_at": "2026-01-01T00:00:00+00:00",
        },
    )

    notes = list_notes(home, cfg)

    assert [note["id"] for note in notes] == ["cap_old"]
    assert not legacy.exists()
    assert (kb_root / CAPTURE_DIR / "cap_old.json").is_file()
    assert read_note_image(home, cfg, "cap_old", "a.png") == (_PNG, "image/png")


def test_cli_capture_add_list_show_rm(tmp_path: Path):
    home = _home(tmp_path)
    rc, out = _run(home, "capture", "add", "--text", "raw note text", "--json")
    assert rc == 0
    note_id = json.loads(out)["id"]

    rc, out = _run(home, "capture", "list", "--json")
    assert rc == 0 and len(json.loads(out)) == 1

    rc, out = _run(home, "capture", "show", note_id)
    assert rc == 0 and "raw note text" in out

    rc, out = _run(home, "capture", "rm", note_id, "--json")
    assert rc == 0 and json.loads(out)["ok"] is True
    rc, out = _run(home, "capture", "list", "--json")
    assert json.loads(out) == []


def test_cli_capture_add_requires_text(tmp_path: Path):
    home = _home(tmp_path)
    rc, _ = _run(home, "capture", "add")
    assert rc == 2


# --- Phase 3: distill-on-demand (deterministic stub agent) ------------------ #
_ONE_CANDIDATE = (
    '[{"kind":"solutions","title":"重试要带指数退避",'
    '"body":"## 适用场景\\n远程调用\\n## 推荐做法\\n指数退避 + 上限"}]'
)


def test_distill_note_enqueues_candidates_and_marks_distilled(tmp_path: Path):
    home = _home(tmp_path)
    cfg = _cfg()
    note = create_note(home, cfg, text="重试老失败，应该加退避", scope_hint="personal")

    result = distill_note(home, cfg, note["id"], agent=_stub(_sidecar_reply(_ONE_CANDIDATE)))
    assert result["ok"] and result["candidates"] == 1

    pending = list_pending(home, cfg)
    assert len(pending) == 1
    # capture default scope_hint flows through; personal carries no project_key.
    assert pending[0]["scope"] == "personal"
    assert pending[0]["project_key"] is None
    assert pending[0]["title"] == "重试要带指数退避"

    updated = read_note(home, cfg, note["id"])
    assert updated["status"] == "distilled"
    assert updated["candidate_ids"] == result["candidate_ids"]


def test_distill_agent_context_includes_config_and_cwd(tmp_path: Path):
    home = _home(tmp_path)
    cfg = _cfg()
    cfg["claude"] = {"model": "env:work", "env": [{"name": "work", "ANTHROPIC_MODEL": "kimi-k2.6"}]}
    note = create_note(home, cfg, text="raw retry idea", scope_hint="personal")
    seen = {}

    def _agent(ctx):
        seen.update({"config": ctx.get("config"), "cwd": ctx.get("cwd")})
        return _sidecar_reply(_ONE_CANDIDATE)

    result = distill_note(home, cfg, note["id"], agent=_agent)

    assert result["ok"]
    assert seen["config"] is cfg
    assert seen["cwd"] == home


def test_distill_note_writes_agent_log_success_and_error(tmp_path: Path):
    from aha_cli.services.knowledge_capture_distill import CaptureAgentError

    home = _home(tmp_path)
    cfg = _cfg()
    note = create_note(home, cfg, text="raw retry idea", scope_hint="personal")

    def _agent(ctx):
        ctx["progress_callback"]("agent_command_started", {"command": "Read raw note"})
        ctx["progress_callback"]("agent_usage", {"usage": {"total_tokens": 123}})
        return _sidecar_reply(_ONE_CANDIDATE)

    result = distill_note(home, cfg, note["id"], backend="claude", model="env:work", agent=_agent)
    assert result["ok"] and result["log_id"]

    log = read_distill_log(home, cfg, note["id"])
    assert log["id"] == result["log_id"]
    assert log["status"] == "distilled"
    assert log["backend"] == "claude" and log["model"] == "env:work"
    assert "raw retry idea" in log["prompt"]
    assert "aha_knowledge_candidates" in log["reply"]
    assert log["candidate_ids"] == result["candidate_ids"]
    assert [item["stage"] for item in log["agent_log"]][:2] == ["running", "tool"]
    assert any("Read raw note" in item["message"] for item in log["agent_log"])
    assert any(item.get("total_tokens") == 123 for item in log["agent_log"])
    assert log["agent_log"][-1]["stage"] == "completed"

    bad_note = create_note(home, cfg, text="backend fails", scope_hint="personal")

    def _boom(ctx):
        raise CaptureAgentError("backend unavailable")

    bad = distill_note(home, cfg, bad_note["id"], agent=_boom)
    assert bad["ok"] is False and bad["log_id"]
    bad_log = read_distill_log(home, cfg, bad_note["id"])
    assert bad_log["status"] == "error"
    assert "backend unavailable" in bad_log["error"]
    assert "backend fails" in bad_log["prompt"]

    assert delete_note(home, cfg, bad_note["id"]) is True
    assert list_distill_logs(home, cfg, bad_note["id"]) == []


def test_capture_navigation_distill_log_includes_nav_summary_and_parent(tmp_path: Path):
    home = _home(tmp_path)
    cfg = _cfg()
    write_entry(
        home,
        config=cfg,
        scope="project",
        kind="navigation",
        project_key_value="git-abc",
        title="项目导航",
        body="## 模块索引\n",
        slug="index",
        meta={"type": "navigation"},
    )
    note = create_note(home, cfg, text="微信通知排查链路", scope_hint="project")
    reply = _sidecar_reply(
        '[{"kind":"navigation","scope":"project","project_key":"git-abc",'
        '"title":"AHA 微信通知模块导航","slug":"modules/weixin-notifications",'
        '"responsibility":"负责微信通知状态和主动推送上下文。",'
        '"diagnostic_paths":["先看 notification_status.ready，再看 send_context.state。"]}]'
    )

    result = distill_note(home, cfg, note["id"], agent=_stub(reply))

    assert result["ok"]
    assert result["navigation"]["candidates"] == 2
    pending = list_pending(home, cfg)
    by_slug = {item["slug"]: item for item in pending}
    assert set(by_slug) == {"modules/weixin-notifications", "index"}
    assert "notification_status.ready" in by_slug["modules/weixin-notifications"]["body"]
    log = read_distill_log(home, cfg, note["id"])
    assert log["navigation"]["candidates"] == 2
    assert "modules/weixin-notifications" in log["navigation"]["slugs"]


def test_default_capture_agent_uses_claude_env_group_config(tmp_path: Path, monkeypatch):
    from aha_cli.services.knowledge_capture_distill import default_capture_agent

    cfg = {
        "claude": {
            "bin": "/opt/claude/bin/claude",
            "model": "env:work",
            "proxy": {
                "enabled": True,
                "http_proxy": "http://claude.proxy:7890",
                "https_proxy": "http://claude.proxy:7890",
                "no_proxy": "localhost,127.0.0.1",
            },
            "env": [
                {
                    "name": "work",
                    "ANTHROPIC_API_KEY": "work-key",
                    "ANTHROPIC_BASE_URL": "https://claude.test",
                    "ANTHROPIC_MODEL": "kimi-k2.6",
                }
            ],
        }
    }
    seen = {}

    def fake_run(prompt, **kwargs):
        seen.update({"prompt": prompt, **kwargs})
        return 0, _sidecar_reply(_ONE_CANDIDATE), None

    monkeypatch.setattr("aha_cli.backends.claude.run_claude_exec", fake_run)
    reply = default_capture_agent({"prompt": "organize", "backend": "claude", "model": "env:work", "config": cfg, "cwd": tmp_path})

    assert "aha_knowledge_candidates" in reply
    assert seen["claude_bin"] == "/opt/claude/bin/claude"
    assert seen["model"] is None
    assert seen["claude_config"]["env_active"] == "work"
    assert seen["claude_config"]["env"][0]["ANTHROPIC_API_KEY"] == "work-key"
    assert seen["proxy_env"]["HTTPS_PROXY"] == "http://claude.proxy:7890"
    assert seen["proxy_env"]["https_proxy"] == "http://claude.proxy:7890"
    assert seen["proxy_env"]["NO_PROXY"] == "localhost,127.0.0.1"
    seen.clear()
    default_capture_agent({"prompt": "organize", "backend": "claude", "model": "env:work", "proxy_enabled": False, "config": cfg, "cwd": tmp_path})
    assert seen["proxy_env"] == {}


def test_default_capture_agent_passes_codex_env_group_config(tmp_path: Path, monkeypatch):
    from aha_cli.services.knowledge_capture_distill import default_capture_agent

    cfg = {
        "codex": {
            "bin": "/opt/codex/bin/codex",
            "model": "env:openai",
            "proxy": {
                "enabled": True,
                "http_proxy": "http://codex.proxy:7890",
                "https_proxy": "http://codex.proxy:7890",
                "no_proxy": "localhost,127.0.0.1",
            },
            "env": [
                {
                    "name": "openai",
                    "OPENAI_API_KEY": "openai-key",
                    "OPENAI_BASE_URL": "https://openai.test/v1",
                    "OPENAI_MODEL": "gpt-5.5",
                }
            ],
        }
    }
    seen = {}

    def fake_run(prompt, **kwargs):
        seen.update({"prompt": prompt, **kwargs})
        return 0, _sidecar_reply(_ONE_CANDIDATE), None

    monkeypatch.setattr("aha_cli.backends.codex.run_codex_exec", fake_run)
    reply = default_capture_agent({"prompt": "organize", "backend": "codex", "model": "env:openai", "config": cfg, "cwd": tmp_path})

    assert "aha_knowledge_candidates" in reply
    assert seen["codex_bin"] == "/opt/codex/bin/codex"
    assert seen["model"] == "env:openai"
    assert seen["codex_config"]["env"][0]["OPENAI_API_KEY"] == "openai-key"
    assert seen["proxy_env"]["HTTP_PROXY"] == "http://codex.proxy:7890"
    assert seen["proxy_env"]["http_proxy"] == "http://codex.proxy:7890"
    assert seen["proxy_env"]["NO_PROXY"] == "localhost,127.0.0.1"
    seen.clear()
    default_capture_agent({"prompt": "organize", "backend": "codex", "model": "env:openai", "proxy_enabled": False, "config": cfg, "cwd": tmp_path})
    assert seen["proxy_env"] == {}


def test_distill_note_passes_effective_backend_and_model_to_agent(tmp_path: Path):
    home = _home(tmp_path)
    cfg = _cfg()
    cfg["backend"] = "codex"
    cfg["codex"] = {"model": "env:openai", "proxy": {"enabled": True, "http_proxy": "http://codex.proxy:7890"}}
    note = create_note(home, cfg, text="raw", scope_hint="personal")
    seen = {}

    def agent(ctx):
        seen.update(ctx)
        return _sidecar_reply(_ONE_CANDIDATE)

    result = distill_note(home, cfg, note["id"], proxy_enabled=False, agent=agent)

    assert result["ok"]
    assert seen["backend"] == "codex"
    assert seen["model"] == "env:openai"
    assert seen["proxy_enabled"] is False
    log = read_distill_log(home, cfg, note["id"])
    assert log["backend"] == "codex"
    assert log["model"] == "env:openai"
    assert log["proxy_enabled"] is False


def test_distill_rerun_replaces_previous_candidates(tmp_path: Path):
    home = _home(tmp_path)
    cfg = _cfg()
    note = create_note(home, cfg, text="raw", scope_hint="personal")

    distill_note(home, cfg, note["id"], agent=_stub(_sidecar_reply(_ONE_CANDIDATE)))
    assert len(list_pending(home, cfg)) == 1

    second = '[{"kind":"wiki","title":"另一条","body":"## 结论\\n改了主意"}]'
    distill_note(home, cfg, note["id"], agent=_stub(_sidecar_reply(second)))
    pending = list_pending(home, cfg)
    assert len(pending) == 1  # old candidate replaced, not accumulated
    assert pending[0]["title"] == "另一条"


def test_redistill_replaces_pending_and_targets_existing_entry(tmp_path: Path):
    home = _home(tmp_path)
    cfg = _cfg()
    note = create_note(home, cfg, text="raw", scope_hint="personal")

    distill_note(home, cfg, note["id"], agent=_stub(_sidecar_reply(_ONE_CANDIDATE)))
    first = list_pending(home, cfg)[0]
    entry_path = approve_candidate(home, cfg, first["id"])
    entry = read_entry(entry_path)
    assert entry["meta"]["source_note_id"] == note["id"]

    second = '[{"kind":"solutions","title":"新的退避标题","body":"## 适用场景\\n远程调用\\n## 推荐做法\\n新版"}]'
    distill_note(home, cfg, note["id"], agent=_stub(_sidecar_reply(second)))
    pending = list_pending(home, cfg)
    assert len(pending) == 1
    assert pending[0]["title"] == "新的退避标题"
    assert pending[0]["action"] == "update"
    assert pending[0]["updates_entry_id"] == entry["meta"]["id"]
    assert pending[0]["slug"] == entry["meta"]["slug"]

    third = '[{"kind":"solutions","title":"第三版退避标题","body":"## 适用场景\\n远程调用\\n## 推荐做法\\n第三版"}]'
    distill_note(home, cfg, note["id"], agent=_stub(_sidecar_reply(third)))
    pending = list_pending(home, cfg)
    assert len(pending) == 1
    assert pending[0]["title"] == "第三版退避标题"
    assert pending[0]["updates_entry_id"] == entry["meta"]["id"]


def test_distill_unbound_project_candidate_downgrades_to_personal(tmp_path: Path):
    home = _home(tmp_path)
    cfg = _cfg()
    note = create_note(home, cfg, text="raw", scope_hint="personal")
    # Agent suggests project scope but gives no project_key -> nothing to bind to.
    reply = _sidecar_reply('[{"kind":"wiki","scope":"project","title":"X","body":"## 结论\\nok"}]')
    distill_note(home, cfg, note["id"], agent=_stub(reply))
    pending = list_pending(home, cfg)
    assert pending[0]["scope"] == "personal" and pending[0]["project_key"] is None


def test_distill_empty_candidates_marks_distilled_with_none(tmp_path: Path):
    home = _home(tmp_path)
    cfg = _cfg()
    note = create_note(home, cfg, text="纯闲聊没知识", scope_hint="personal")
    result = distill_note(home, cfg, note["id"], agent=_stub(_sidecar_reply("[]")))
    assert result["ok"] and result["candidates"] == 0
    assert list_pending(home, cfg) == []
    assert read_note(home, cfg, note["id"])["status"] == "distilled"


def test_distill_missing_note_returns_error(tmp_path: Path):
    home = _home(tmp_path)
    cfg = _cfg()
    result = distill_note(home, cfg, "cap_nope", agent=_stub(_sidecar_reply(_ONE_CANDIDATE)))
    assert result["ok"] is False and "not found" in result["error"]


def test_sniff_image_mime():
    assert sniff_image_mime(_PNG) == "image/png"
    assert sniff_image_mime(b"\xff\xd8\xff\xe0abc") == "image/jpeg"
    assert sniff_image_mime(b"RIFF\x00\x00\x00\x00WEBPxx") == "image/webp"
    assert sniff_image_mime(b"not an image at all") is None


def test_add_read_remove_note_image(tmp_path: Path):
    home = _home(tmp_path)
    cfg = _cfg()
    note = create_note(home, cfg, text="screenshot of the error", scope_hint="personal")

    img = add_note_image(home, cfg, note["id"], data=_PNG, filename="err shot.png")
    assert img["mime"] == "image/png" and img["size"] == len(_PNG)
    assert img["path"].startswith(f"{CAPTURE_DIR}/assets/{note['id']}/")
    # File persisted on disk (not base64 in the note JSON).
    asset = home / "knowledge" / img["path"]
    assert asset.is_file() and asset.read_bytes() == _PNG
    stored = read_note(home, cfg, note["id"])
    assert len(stored["images"]) == 1 and "data" not in stored["images"][0]

    got = read_note_image(home, cfg, note["id"], img["name"])
    assert got is not None and got[0] == _PNG and got[1] == "image/png"

    assert remove_note_image(home, cfg, note["id"], img["name"]) is True
    assert read_note(home, cfg, note["id"])["images"] == []
    assert not asset.exists()


def test_add_note_image_embeds_markdown_ref_in_text(tmp_path: Path):
    home = _home(tmp_path)
    cfg = _cfg()
    note = create_note(home, cfg, text="see this:", scope_hint="personal")
    img = add_note_image(home, cfg, note["id"], data=_PNG, filename="shot.png")

    updated = read_note(home, cfg, note["id"])
    # Image is referenced inline in the note body (memo-style) AND registered.
    assert f"](/api/kb/capture/image?id={note['id']}&name={img['name']})" in updated["text"]
    assert "see this:" in updated["text"]
    assert len(updated["images"]) == 1


def test_add_note_image_rejects_bad_type_and_oversize(tmp_path: Path, monkeypatch):
    home = _home(tmp_path)
    cfg = _cfg()
    note = create_note(home, cfg, text="x")

    with pytest.raises(ImageRejected):
        add_note_image(home, cfg, note["id"], data=b"totally not an image", filename="x.png")

    monkeypatch.setattr(cap_store, "MAX_IMAGE_BYTES", 4)
    with pytest.raises(ImageRejected):
        add_note_image(home, cfg, note["id"], data=_PNG, filename="big.png")


def test_distill_prompt_lists_images_without_pretending_to_see_them(tmp_path: Path):
    from aha_cli.services.knowledge_capture_distill import build_capture_prompt

    note = {"text": "see attached", "scope_hint": "personal",
            "images": [{"name": "a.png", "original": "a.png", "mime": "image/png", "size": 10, "path": "capture/assets/n/a.png"}]}
    prompt = build_capture_prompt(note)
    assert "a.png" in prompt
    assert "NOT visually analyzed" in prompt
    assert "Do NOT invent image contents" in prompt


def test_distill_prompt_uses_title_and_body_as_one_clean_article(tmp_path: Path):
    from aha_cli.services.knowledge_capture_distill import build_capture_prompt

    prompt = build_capture_prompt({"title": "微信通知", "text": "只读排查发现微信通知入口", "scope_hint": "project"})

    assert "整理成一篇逻辑清晰的文章" in prompt
    assert "不要拓展" in prompt
    assert "不要修改核心内容" in prompt
    assert "不要拆成多条候选" in prompt
    assert "不搜索、不读取知识库" in prompt
    assert "--- NOTE TITLE ---" in prompt
    assert "微信通知" in prompt
    assert "--- NOTE BODY ---" in prompt
    assert "只读排查发现微信通知入口" in prompt
    assert '"kind":"wiki"' in prompt
    assert "aha_knowledge_candidates" in prompt


def test_approve_promotes_capture_note_assets_to_entry(tmp_path: Path):
    home = _home(tmp_path)
    cfg = _cfg()
    note = create_note(home, cfg, text="bug repro", scope_hint="personal")
    add_note_image(home, cfg, note["id"], data=_PNG, filename="repro.png")

    distill_note(home, cfg, note["id"], agent=_stub(_sidecar_reply(_ONE_CANDIDATE)))
    pending = list_pending(home, cfg)
    assert len(pending) == 1 and pending[0]["source_note_id"] == note["id"]

    entry_path = approve_candidate(home, cfg, pending[0]["id"])
    entry = read_entry(entry_path)
    # Traceable references in body + metadata.
    assert "## 附图" in entry["body"] and "assets/" in entry["body"]
    assert entry["meta"]["source_note_id"] == note["id"]
    assert len(entry["meta"]["assets"]) == 1

    # Asset copied into the entry's assets dir (now in the tracked tree).
    img_name = entry["meta"]["assets"][0]["name"]
    copied = Path(entry_path).parent / "assets" / Path(entry_path).stem / img_name
    assert copied.is_file() and copied.read_bytes() == _PNG

    # Raw capture asset is left intact (only removed when the note is deleted).
    stored = read_note(home, cfg, note["id"])
    raw_asset = knowledge_root(home, cfg) / stored["images"][0]["path"]
    assert raw_asset.is_file() and raw_asset.read_bytes() == _PNG


def test_promote_assets_idempotent_never_overwrites(tmp_path: Path):
    home = _home(tmp_path)
    cfg = _cfg()
    note = create_note(home, cfg, text="x", scope_hint="personal")
    add_note_image(home, cfg, note["id"], data=_PNG, filename="a.png")
    cand = {"id": "cand_x", "source_note_id": note["id"]}

    p1 = promote_assets_for_entry(home, cfg, cand, scope="personal", kind="wiki", project_key=None, slug="s")
    assert p1 and len(p1["assets"]) == 1
    target = entry_dir(knowledge_root(home, cfg), "personal", "wiki", None) / "assets" / "s" / p1["assets"][0]["name"]
    target.write_bytes(b"SENTINEL")  # tamper

    p2 = promote_assets_for_entry(home, cfg, cand, scope="personal", kind="wiki", project_key=None, slug="s")
    assert p2 and target.read_bytes() == b"SENTINEL"  # second copy did not overwrite


def test_promote_assets_reverse_lookup_via_candidate_ids(tmp_path: Path):
    home = _home(tmp_path)
    cfg = _cfg()
    note = create_note(home, cfg, text="x", scope_hint="personal")
    add_note_image(home, cfg, note["id"], data=_PNG, filename="a.png")
    update_note(home, cfg, note["id"], candidate_ids=["cand_rev"])

    cand = {"id": "cand_rev"}  # no explicit source_note_id
    promo = promote_assets_for_entry(home, cfg, cand, scope="personal", kind="wiki", project_key=None, slug="s")
    assert promo and promo["source_note_id"] == note["id"]


def test_promote_assets_returns_none_when_no_source_note(tmp_path: Path):
    home = _home(tmp_path)
    cfg = _cfg()
    assert promote_assets_for_entry(home, cfg, {"id": "cand_none"}, scope="general", kind="wiki", project_key=None, slug="s") is None


def test_run_distill_job_status_machine_success_and_error(tmp_path: Path):
    from aha_cli.services.knowledge_capture_distill import CaptureAgentError, run_distill_job

    home = _home(tmp_path)
    cfg = _cfg()
    note = create_note(home, cfg, text="raw", scope_hint="personal")

    # Success: raw -> distilled.
    ok = run_distill_job(home, cfg, note["id"], agent=_stub(_sidecar_reply(_ONE_CANDIDATE)))
    assert ok["ok"] and read_note(home, cfg, note["id"])["status"] == "distilled"

    # Failure: agent raises -> note marked error with last_error recorded.
    def _boom(ctx):
        raise CaptureAgentError("backend unavailable")

    bad = run_distill_job(home, cfg, note["id"], agent=_boom)
    assert bad["ok"] is False
    failed = read_note(home, cfg, note["id"])
    assert failed["status"] == "error" and "backend unavailable" in failed["last_error"]
