from __future__ import annotations

import json
import contextlib
import io
from pathlib import Path
import sqlite3
from types import SimpleNamespace

from aha_cli.cli import main
from aha_cli.backends import opencode
from aha_cli.backends.plugin import BackendResolvedTurn, BackendTurnRequest, BackendTurnResult
from aha_cli.services import chat
from aha_cli.services.backend_runtime import _agent_chat_command
from aha_cli.store.filesystem import append_message, task_snapshot
from aha_cli.store.io import read_json
from aha_cli.store.paths import run_dir
from aha_cli.web.session_debug import backend_session_jsonl_info


class _FakeProcess:
    returncode = None

    def poll(self):
        return self.returncode

    def terminate(self):
        self.returncode = 0

    def wait(self, timeout=None):
        return self.returncode

    def kill(self):
        self.returncode = -9


def _request(tmp_path: Path, *, session: dict | None = None) -> BackendTurnRequest:
    resolved = opencode.resolve_opencode_turn(
        config={"opencode": {"agent": "build"}},
        model="opencode/gpt-5.6-sol",
        reasoning_effort="high",
        task_scoped=True,
        session=session or {},
    )
    return BackendTurnRequest(
        prompt="Return exactly OK.",
        cwd=tmp_path,
        output_file=tmp_path / "reply.md",
        events_file=tmp_path / "events.jsonl",
        run_id="run-1",
        task_id="task-1",
        source="opencode-chat",
        target="main",
        session=session or {},
        sandbox="workspace-write",
        approval="never",
        extra_args=[],
        binary="opencode",
        json_events=True,
        resolved=resolved,
        aha_home=tmp_path / ".aha",
        config={},
    )


def test_opencode_model_options_parse_provider_model_lines(monkeypatch) -> None:
    opencode._MODEL_CACHE.clear()
    opencode._MODEL_LOADING.clear()
    monkeypatch.setattr(
        opencode.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(
            returncode=0,
            stdout="opencode/gpt-5.6-sol\nopenai/gpt-5.5\ninvalid\n",
        ),
    )

    opencode._load_opencode_model_options("test-opencode")
    options = opencode.opencode_model_options({"opencode": {"bin": "test-opencode"}})

    assert [item["name"] for item in options] == [
        "",
        "opencode/gpt-5.6-sol",
        "openai/gpt-5.5",
    ]


def test_opencode_read_only_policy_denies_mutating_tools() -> None:
    permissions = opencode._permission_config("read-only")

    assert permissions["edit"] == "deny"
    assert permissions["bash"] == "deny"
    assert permissions["external_directory"] == "deny"
    assert permissions["task"] == "deny"
    assert permissions["question"] == "deny"


def test_opencode_usage_normalizes_to_aha_token_fields() -> None:
    usage = opencode.normalize_opencode_usage(
        {
            "total": 10259,
            "input": 10119,
            "output": 56,
            "reasoning": 20,
            "cache": {"read": 64, "write": 0},
        }
    )

    assert usage["input_tokens"] == 10119
    assert usage["output_tokens"] == 56
    assert usage["reasoning_output_tokens"] == 20
    assert usage["cache_read_input_tokens"] == 64
    assert usage["cache_creation_input_tokens"] == 0
    assert usage["total_tokens"] == 10259

def test_opencode_aha_provider_selector_generates_secret_free_server_config(
    tmp_path: Path,
) -> None:
    config = {
        "providers": [{
            "id": "gateway",
            "name": "Gateway",
            "base_url": "https://gateway.test/v1",
            "auth_style": "bearer",
            "credential": "provider-secret",
        }],
        "configured_models": [{
            "provider_id": "gateway",
            "model_id": "model-a",
            "backend": "opencode",
            "wire_api": "responses",
            "context_window": 200000,
            "max_output_tokens": 64000,
        }],
        "opencode": {
            "agent": "build",
            "env": [{
                "name": "gateway-model-a",
                "AHA_PROVIDER_ID": "gateway",
                "OPENCODE_MODEL": "model-a",
                "OPENCODE_WIRE_API": "responses",
                "OPENCODE_CONTEXT_WINDOW": "200000",
            }],
        },
    }
    resolved = opencode.resolve_opencode_turn(
        config=config,
        model="env:gateway-model-a",
        reasoning_effort="high",
        task_scoped=True,
        session={},
    )
    request = _request(tmp_path)
    request.resolved = resolved

    server_config = opencode._server_config_content(request)
    provider_env = opencode._provider_environment(request)

    assert resolved.command_model == "gateway/model-a"
    assert "@ai-sdk/openai" in server_config
    assert "https://gateway.test/v1" in server_config
    assert '"context": 200000' in server_config
    assert '"output": 64000' in server_config
    assert "provider-secret" not in server_config
    assert list(provider_env.values()) == ["provider-secret"]
    assert list(provider_env)[0].startswith("AHA_OPENCODE_PROVIDER_KEY_")


def test_opencode_zen_selector_uses_native_provider_and_no_custom_config(
    tmp_path: Path,
) -> None:
    config = {
        "providers": [{
            "id": "zen",
            "name": "OpenCode Zen",
            "base_url": "https://opencode.ai/zen/v1",
            "auth_style": "bearer",
            "credential": "zen-secret",
        }],
        "configured_models": [{
            "provider_id": "zen",
            "model_id": "big-pickle",
            "backend": "opencode",
            "wire_api": "chat_completions",
        }],
        "opencode": {
            "agent": "build",
            "env": [{
                "name": "zen-big-pickle",
                "AHA_PROVIDER_ID": "zen",
                "OPENCODE_MODEL": "big-pickle",
                "OPENCODE_WIRE_API": "chat_completions",
            }],
        },
    }
    resolved = opencode.resolve_opencode_turn(
        config=config,
        model="env:zen-big-pickle",
        reasoning_effort=None,
        task_scoped=True,
        session={},
    )
    request = _request(tmp_path)
    request.resolved = resolved

    server_config = opencode._server_config_content(request)

    assert resolved.command_model == "opencode/big-pickle"
    assert '"provider"' not in server_config
    assert "zen-secret" not in server_config
    assert opencode._provider_environment(request) == {}


def test_detect_opencode_zen_models_uses_isolated_auth_and_provider_catalog(
    monkeypatch,
) -> None:
    fake_process = _FakeProcess()
    calls: list[tuple[str, str, dict | None]] = []
    monkeypatch.setattr(opencode.subprocess, "Popen", lambda *args, **kwargs: fake_process)
    monkeypatch.setattr(opencode, "assign_parent_death", lambda _process: None)

    def fake_request(base_url, path, *, method="GET", body=None, **kwargs):
        calls.append((path, method, body))
        if path == "/global/health":
            return {"healthy": True}
        if path == "/auth/opencode":
            assert body == {"type": "api", "key": "zen-secret"}
            return True
        if path == "/provider":
            return {
                "all": [{
                    "id": "opencode",
                    "models": {
                        "model-a": {
                            "status": "active",
                            "api": {"npm": "@ai-sdk/openai-compatible"},
                            "limit": {"context": 200000, "output": 64000},
                        },
                        "model-b": {
                            "status": "active",
                            "api": {"npm": "@ai-sdk/anthropic"},
                            "limit": {"context": 1000000, "output": 128000},
                        },
                        "retired": {"status": "deprecated"},
                    },
                }]
            }
        raise AssertionError(path)

    monkeypatch.setattr(opencode, "_json_request", fake_request)

    models = opencode.detect_opencode_zen_models("opencode", "zen-secret")

    assert models == [
        {
            "id": "model-a",
            "mode": "chat_completions",
            "max_input_tokens": 200000,
            "max_output_tokens": 64000,
        },
        {
            "id": "model-b",
            "mode": "anthropic_messages",
            "max_input_tokens": 1000000,
            "max_output_tokens": 128000,
        },
    ]
    assert [call[0] for call in calls] == [
        "/global/health",
        "/auth/opencode",
        "/provider",
    ]
    assert fake_process.returncode == 0


def test_windows_model_detection_falls_back_to_wsl_without_key_in_argv(
    tmp_path: Path,
    monkeypatch,
) -> None:
    calls: list[dict] = []
    monkeypatch.setattr(opencode.sys, "platform", "win32")
    monkeypatch.setattr(opencode, "_windows_opencode_available", lambda _binary: False)
    monkeypatch.setattr(
        opencode,
        "_wsl_distro_candidates",
        lambda _root, _config: ["Ubuntu-24.04"],
    )
    monkeypatch.setattr(
        "aha_cli.services.onebin.authoritative_onebin_path",
        lambda: Path(r"C:\Users\toope\AppData\Local\AHA\aha"),
    )
    monkeypatch.setattr(
        "aha_cli.services.wsl_backend._wsl_executable",
        lambda: r"C:\Windows\System32\wsl.exe",
    )
    monkeypatch.setattr(
        "aha_cli.services.wsl_backend.wsl_backends_for_workspace",
        lambda _root, _distro: {
            "opencode": "/home/kaikai/bin/opencode",
            "python3": "/usr/bin/python3",
        },
    )
    monkeypatch.setattr(
        "aha_cli.store.ws_target.windows_path_to_wsl",
        lambda _path: "/mnt/c/Users/toope/AppData/Local/AHA/aha",
    )
    monkeypatch.setattr(
        "aha_cli.store.ws_target.wsl_native_home",
        lambda _root: "/mnt/c/Users/toope/.aha",
    )

    def fake_run(command, **kwargs):
        calls.append({"command": command, **kwargs})
        return SimpleNamespace(
            returncode=0,
            stdout=json.dumps({
                "ok": True,
                "models": [{"id": "big-pickle", "mode": "chat_completions"}],
            }),
            stderr="",
        )

    monkeypatch.setattr(opencode.subprocess, "run", fake_run)

    models = opencode.detect_opencode_zen_models_for_runtime(
        tmp_path,
        {"opencode": {"bin": "opencode"}},
        "zen-secret",
    )

    assert models == [{"id": "big-pickle", "mode": "chat_completions"}]
    call = calls[0]
    assert call["command"][:3] == [
        r"C:\Windows\System32\wsl.exe",
        "-d",
        "Ubuntu-24.04",
    ]
    assert "/home/kaikai/bin/opencode" in call["command"]
    assert "zen-secret" not in " ".join(call["command"])
    assert json.loads(call["input"])["credential"] == "zen-secret"
    assert "zen-secret" not in json.dumps(call["env"])


def test_opencode_turn_creates_session_and_maps_response_events(
    tmp_path: Path,
    monkeypatch,
) -> None:
    request = _request(tmp_path)
    calls: list[tuple[str, str, dict | None]] = []
    fake_process = _FakeProcess()
    monkeypatch.setattr(
        opencode,
        "_start_server",
        lambda _request: (
            fake_process,
            "http://127.0.0.1:4096",
            "opencode",
            "secret",
            {"healthy": True, "version": "1.18.21"},
        ),
    )

    def fake_request(base_url, path, *, method="GET", body=None, **kwargs):
        calls.append((path, method, body))
        if path == "/session":
            assert any(
                item["permission"] == "external_directory"
                and item["action"] == "deny"
                for item in body["permission"]
            )
            return {"id": "ses-1"}
        if path == "/session/ses-1/message":
            return {
                "info": {
                    "tokens": {"input": 10, "output": 2, "reasoning": 1},
                    "cost": 0.01,
                },
                "parts": [
                    {
                        "type": "tool",
                        "tool": "bash",
                        "callID": "tool-1",
                        "state": {
                            "status": "completed",
                            "input": {"command": "pwd"},
                            "output": str(tmp_path),
                        },
                    },
                    {"type": "text", "text": "OK"},
                ],
            }
        raise AssertionError(path)

    monkeypatch.setattr(opencode, "_json_request", fake_request)

    result = opencode.run_opencode_turn(request)

    assert result.exit_code == 0
    assert result.reply == "OK"
    assert result.session["backend_session_id"] == "ses-1"
    assert request.output_file.read_text(encoding="utf-8") == "OK"
    prompt_call = calls[-1]
    assert prompt_call[0] == "/session/ses-1/message"
    assert prompt_call[2]["model"] == {
        "providerID": "opencode",
        "modelID": "gpt-5.6-sol",
    }
    assert prompt_call[2]["variant"] == "high"
    events = [
        json.loads(line)
        for line in request.events_file.read_text(encoding="utf-8").splitlines()
    ]
    assert any(event["type"] == "agent_thread" for event in events)
    assert any(event["type"] == "agent_command_started" for event in events)
    assert any(event["type"] == "agent_command_finished" for event in events)
    assert any(
        event["type"] == "agent_message" and event["data"]["text"] == "OK"
        for event in events
    )
    assert any(event["type"] == "agent_usage" for event in events)
    assert fake_process.returncode == 0


def test_opencode_turn_reuses_existing_session(tmp_path: Path, monkeypatch) -> None:
    request = _request(tmp_path, session={"backend_session_id": "ses-existing"})
    calls: list[str] = []
    monkeypatch.setattr(
        opencode,
        "_start_server",
        lambda _request: (
            _FakeProcess(),
            "http://127.0.0.1:4096",
            "opencode",
            "secret",
            {"healthy": True},
        ),
    )

    def fake_request(base_url, path, **kwargs):
        calls.append(path)
        assert path == "/session/ses-existing/message"
        return {"info": {"tokens": {}}, "parts": [{"type": "text", "text": "continued"}]}

    monkeypatch.setattr(opencode, "_json_request", fake_request)

    result = opencode.run_opencode_turn(request)

    assert result.exit_code == 0
    assert result.reply == "continued"
    assert calls == ["/session/ses-existing/message"]


def test_configured_opencode_provider_uses_stable_session_data_store(
    tmp_path: Path,
    monkeypatch,
) -> None:
    request = _request(tmp_path)
    request.resolved.backend_config["_aha_provider"] = {
        "base_url": "https://opencode.ai/zen/v1",
        "credential": "provider-secret",
    }
    captured: dict = {}
    fake_process = _FakeProcess()

    def fake_popen(command, **kwargs):
        captured["command"] = command
        captured.update(kwargs)
        return fake_process

    def fake_request(base_url, path, **kwargs):
        if path == "/global/health":
            return {"healthy": True}
        if path == "/auth/opencode":
            return {}
        raise AssertionError(path)

    monkeypatch.setattr(opencode.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(opencode, "_json_request", fake_request)

    process, *_rest = opencode._start_server(request)

    store_root = opencode.opencode_session_store_root(request)
    assert captured["env"]["XDG_DATA_HOME"] == str(store_root / "data")
    assert captured["env"]["XDG_CONFIG_HOME"] != str(store_root / "config")
    auth_file = store_root / "data" / "opencode" / "auth.json"
    auth_file.parent.mkdir(parents=True, exist_ok=True)
    auth_file.write_text("provider-secret", encoding="utf-8")
    database = store_root / "data" / "opencode" / "opencode.db"
    database.write_text("session-db", encoding="utf-8")

    opencode._stop_server(process)

    assert not auth_file.exists()
    assert database.read_text(encoding="utf-8") == "session-db"


def test_opencode_session_marker_requires_matching_persistent_database(
    tmp_path: Path,
) -> None:
    request = _request(tmp_path, session={"backend_session_id": "ses-persisted"})
    request.resolved.backend_config["_aha_provider"] = {
        "base_url": "https://opencode.ai/zen/v1",
    }

    opencode.write_opencode_session_marker(request, "ses-persisted")

    assert (
        opencode.opencode_session_resume_id(
            request.aha_home,
            request.run_id,
            request.task_id,
            request.target,
            request.session,
        )
        is None
    )
    database = (
        opencode.opencode_session_store_root(request)
        / "data"
        / "opencode"
        / "opencode.db"
    )
    database.parent.mkdir(parents=True, exist_ok=True)
    database.write_text("session-db", encoding="utf-8")

    assert (
        opencode.opencode_session_resume_id(
            request.aha_home,
            request.run_id,
            request.task_id,
            request.target,
            request.session,
        )
        == "ses-persisted"
    )
    request.session["backend_session_id"] = "ses-other"
    assert (
        opencode.opencode_session_resume_id(
            request.aha_home,
            request.run_id,
            request.task_id,
            request.target,
            request.session,
        )
        is None
    )


def test_opencode_session_artifact_info_reads_sqlite_metadata(
    tmp_path: Path,
) -> None:
    request = _request(tmp_path, session={"backend_session_id": "ses-persisted"})
    database = opencode.opencode_session_database_path(
        request.aha_home,
        request.run_id,
        request.task_id,
        request.target,
    )
    database.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(database) as connection:
        connection.executescript(
            """
            create table session (
                id text primary key,
                model text,
                tokens_input integer,
                tokens_output integer,
                tokens_reasoning integer,
                tokens_cache_read integer,
                tokens_cache_write integer,
                cost real,
                time_created integer,
                time_updated integer
            );
            create table message (id text primary key, session_id text);
            create table part (id text primary key, message_id text, session_id text);
            """
        )
        connection.execute(
            "insert into session values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "ses-persisted",
                json.dumps({
                    "id": "big-pickle",
                    "providerID": "opencode",
                    "variant": "high",
                }),
                34401,
                737,
                77,
                10752,
                0,
                0.0,
                1000,
                2000,
            ),
        )
        connection.executemany(
            "insert into message values (?, ?)",
            (("msg-1", "ses-persisted"), ("msg-2", "ses-persisted")),
        )
        connection.executemany(
            "insert into part values (?, ?, ?)",
            (
                ("part-1", "msg-1", "ses-persisted"),
                ("part-2", "msg-2", "ses-persisted"),
                ("part-3", "msg-2", "ses-persisted"),
            ),
        )

    info = opencode.opencode_session_artifact_info(
        aha_home=request.aha_home,
        run_id=request.run_id,
        task_id=request.task_id,
        target=request.target,
        session_id="ses-persisted",
    )

    assert info["exists"] is True
    assert info["artifact_type"] == "sqlite"
    assert info["path"] == str(database)
    assert info["analysis"]["message_count"] == 2
    assert info["analysis"]["part_count"] == 3
    assert info["analysis"]["model"] == {
        "id": "big-pickle",
        "providerID": "opencode",
        "variant": "high",
    }
    assert info["analysis"]["cumulative_usage"] == {
        "input_tokens": 34401,
        "output_tokens": 737,
        "reasoning_output_tokens": 77,
        "cache_read_input_tokens": 10752,
        "cache_creation_input_tokens": 0,
        "cost": 0.0,
    }
    web_info = backend_session_jsonl_info(
        {
            "run_id": request.run_id,
            "task_id": request.task_id,
            "agent_id": request.target,
            "backend": "opencode",
            "backend_session_id": "ses-persisted",
        },
        root=request.aha_home,
    )
    assert web_info["exists"] is True
    assert web_info["artifact_type"] == "sqlite"
    assert web_info["analysis"]["message_count"] == 2


def test_unresumable_configured_opencode_session_is_reset_before_prompt(
    tmp_path: Path,
    monkeypatch,
) -> None:
    session = {
        "backend_session_id": "ses-missing",
        "backend": "opencode",
    }
    resolved = BackendResolvedTurn(
        backend_config={
            "_aha_provider": {
                "base_url": "https://opencode.ai/zen/v1",
            }
        }
    )
    reset_session = {
        **session,
        "backend_session_id": None,
        "status": "reset",
    }
    reset_calls: list[dict] = []
    monkeypatch.setattr(
        opencode,
        "opencode_session_resume_id",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        "aha_cli.services.session_compact.compact_reset_backend_session",
        lambda *_args, **kwargs: (
            reset_calls.append(kwargs)
            or {"session": reset_session}
        ),
    )

    chat._compact_unresumable_session(
        tmp_path,
        "run-1",
        "task-1",
        "main",
        "opencode",
        session,
        resolved=resolved,
    )

    assert session["backend_session_id"] is None
    assert session["status"] == "reset"
    assert reset_calls[0]["stop_backend_before_reset"] is False


def test_opencode_sse_maps_live_text_and_tool_events(tmp_path: Path) -> None:
    request = _request(tmp_path, session={"backend_session_id": "ses-live"})
    stream = opencode._OpenCodeEventStream(
        request,
        base_url="http://127.0.0.1:4096",
        username="opencode",
        password="secret",
        session_id="ses-live",
    )
    stream._handle_event({
        "payload": {
            "type": "message.part.updated",
            "properties": {
                "delta": "working",
                "part": {
                    "type": "text",
                    "sessionID": "ses-live",
                },
            },
        }
    })
    tool_part = {
        "type": "tool",
        "sessionID": "ses-live",
        "tool": "bash",
        "callID": "tool-live",
        "state": {
            "status": "running",
            "input": {"command": "pwd"},
        },
    }
    stream._handle_event({
        "payload": {
            "type": "message.part.updated",
            "properties": {"part": tool_part},
        }
    })
    tool_part["state"] = {
        "status": "completed",
        "input": {"command": "pwd"},
        "output": str(tmp_path),
    }
    stream._handle_event({
        "payload": {
            "type": "message.part.updated",
            "properties": {"part": tool_part},
        }
    })

    events = [
        json.loads(line)
        for line in request.events_file.read_text(encoding="utf-8").splitlines()
    ]
    assert stream.text_emitted is True
    assert stream.started_tools == {"tool-live"}
    assert stream.finished_tools == {"tool-live"}
    assert [event["type"] for event in events] == [
        "agent_message",
        "agent_command_started",
        "agent_command_finished",
    ]
    assert events[0]["data"]["partial"] is True


def test_runtime_builds_opencode_worker_command_from_descriptor(tmp_path: Path) -> None:
    command = _agent_chat_command(
        "run-1",
        "main",
        backend="opencode",
        aha_home=tmp_path,
        backend_bin="/usr/bin/opencode",
        model="opencode/gpt-5.6-sol",
        reasoning_effort="high",
        task_id="task-1",
    )

    assert "opencode-chat" in command
    assert "--opencode-bin" in command
    assert "/usr/bin/opencode" in command
    assert "--model" in command


def test_opencode_chat_runs_through_generic_aha_worker(
    tmp_path: Path,
    monkeypatch,
) -> None:
    def run_cli(*args: str) -> tuple[int, str]:
        output = io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
            code = main(["--home", str(tmp_path / ".aha"), *args])
        return code, output.getvalue()

    monkeypatch.setattr(Path, "cwd", lambda: tmp_path)
    code, _ = run_cli("init", "--force", "--backend", "opencode")
    assert code == 0
    code, output = run_cli("plan", "OpenCode generic worker", "--agents", "1")
    assert code == 0
    run_id = output.splitlines()[0].split(": ", 1)[1]
    append_message(
        tmp_path / ".aha",
        run_id,
        "main",
        "finish this task",
        sender="browser",
        task_id="task-001",
        role="main",
    )

    def fake_turn(request: BackendTurnRequest) -> BackendTurnResult:
        request.session["backend_session_id"] = "ses-opencode-test"
        return BackendTurnResult(
            exit_code=0,
            reply='{"actions":[],"response":"OpenCode completed."}',
            session=request.session,
        )

    monkeypatch.setattr(opencode, "run_opencode_turn", fake_turn)

    code, _ = run_cli(
        "opencode-chat",
        run_id,
        "main",
        "--task-id",
        "task-001",
        "--from-start",
        "--once",
        "--model",
        "opencode/big-pickle",
    )

    assert code == 0
    detail = task_snapshot(tmp_path / ".aha", run_id, "task-001")
    assert detail["task"]["status"] == "awaiting_user"
    assert detail["task"]["agents"][0]["backend"] == "opencode"
    session = read_json(
        run_dir(tmp_path / ".aha", run_id)
        / "tasks"
        / "task-001"
        / "sessions"
        / "main.json"
    )
    assert session["backend_session_id"] == "ses-opencode-test"
