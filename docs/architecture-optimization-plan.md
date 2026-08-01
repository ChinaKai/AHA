# AHA Architecture Optimization Plan (2026-08, pinned, strategic)

This is the **strategic** plan. It sits above two row-level documents:

- [`docs/optimization-plan.md`](optimization-plan.md) — historical maintenance
  track (Phases 0–13), kept as the format and decision reference.
- [`docs/optimization-current.md`](optimization-current.md) — current-code,
  row-level optimization (file-size limit, near-limit preventive splits).

Update this file before and after each slice so the next agent resumes from
evidence, not chat history.

## End Goal (success criterion)

> **After this plan lands, AHA runs full-featured on Windows** — the same Web
> UI, codex/claude agent orchestration, shared browser control, hardware UART
> (over COM ports), knowledge base, and run archive/import-export that run on
> Linux today.

### Scope of "full functionality on Windows"

In scope (must work on Windows):

- Web UI + HTTP/WebSocket server (`asyncio.start_server`).
- codex/claude/stub backend orchestration and chat loop.
- Shared browser control (Playwright Chromium — already cross-platform).
- Hardware UART over COM ports (cross-platform transport via pyserial).
- Knowledge base: storage, retrieval, navigation, git sync.
- Run lifecycle, archive import/export, retention, recovery.

Out of scope (optional platform-specific tooling, may stay Unix-only):

- `systemd` user service installers (`.sh`) — Windows uses a process/Startup
  shortcut or Task Scheduler wrapper instead.
- Developer/release smoke shell scripts under `scripts/*.sh`.

## Design Principles

1. **Behavior-preserving slices** — one Conventional Commit each; public
   behavior unchanged; reuse the established acceptance pattern.
2. **Disk format unchanged, typed edges** — `plan.json`/`task.json` stay
   dict/JSON for compatibility; module boundaries gain read-only typed views.
3. **Portability over feature** — no new machine-absolute paths, no platform
   hard-coding, no external-CLI hard dependency in core logic.
4. **Compatibility is a safety net laid before refactors** — freeze schema
   versions and legacy snapshots first so regressions surface immediately.

## Baseline — Windows blockers (verified in code, HEAD `2184c44`)

| Blocker | Evidence | Impact |
| --- | --- | --- |
| `fcntl.flock` everywhere | `store/io.py:3,37,48`; `store/runs.py:4,26,30`; `services/serial_lock.py`; `services/browser_runtime.py:180,205,515`; `services/browser_bookmarks.py:110`; `services/network_terminal.py:149` | **Critical** — `fcntl` does not import on Windows; even the core store write-lock fails. |
| `os.geteuid()` | `services/serial_lock.py:84,206,217` | AttributeError on Windows (serial-lock path). |
| Unix signals + process groups | `os.killpg` at `web/knowledge_routes.py:161`; `signal.SIGTERM`/`os.kill` at `services/observe_proxy.py:172`, `serial_lock.py:210,224`, `knowledge_routes.py:163` | No `SIGTERM`/process-groups on Windows; subprocess-stop (e.g. knowledge git) breaks. |
| Linux-only syscalls | `libc.prctl(_PR_SET_PDEATHSIG, signal.SIGTERM)` at `services/hardware_bridge.py:199-203`; `preexec_fn` at `:258` | Hardware bridge parent-death auto-cleanup; needs a Windows Job Object (`KILL_ON_JOB_CLOSE`) in P3.8. |
| `termios` + raw `os.open` serial I/O | `termios.tcgetattr`/`tcsetattr` + `os.open(device, O_NOCTTY\|O_NONBLOCK)` at `services/hardware_session.py:303-331` | `termios` is POSIX-only; Windows COM ports need pyserial (`serial.Serial`) instead of `os.open`. |
| Hard-coded `/tmp` | `cli_parser.py:148` (`--tmp-root default="/tmp"`); `services/run_cleanup.py:35` (`Path("/tmp"), Path("/var/tmp")`) | Wrong temp roots on Windows; `tempfile.gettempdir()` is correct. |
| `/bin/sh`, `shell=True` | `services/local_terminal.py:23` (`"/bin/sh"`); `services/run_tasks.py:115` (`shell=True`) | No `/bin/sh` on Windows; needs cmd/powershell or arg-list invocation. |
| `print()` as logging (242) | repo-wide; `logging` usage = 0 | Not portable to journald/log aggregators; no levels. |
| Machine-absolute config | `.aha/config.json` stores absolute `workspace_roots`, `codex.bin`, `claude.bin` | Copying `~/.aha` across machines breaks. |

Everything else is already portable-friendly: `pathlib.Path` throughout,
`asyncio` (works on Windows with the right event-loop policy), JSON/JSONL
storage, zero runtime dependencies, Playwright cross-platform.

## Phased Plan (ordered by risk × payoff)

### Phase 0 — Foundations (low risk; unblocks everything)

| Slice | Target | Approach | Win |
| --- | --- | --- | --- |
| **P0.1 Structured logging** | Replace 242 `print()` with stdlib `logging` (no new dep) | `services/logging_facade.py` (levels + optional JSON handler); migrate call sites; startup banner → INFO | Portable, operable |
| **P0.2 Constants + env overrides** | Centralize magic values (port `8788`, home, release URL, timeouts) with env overrides (`AHA_PORT`/`AHA_HOME`/`AHA_RELEASE_URL`) | Extend `constants.py` | Configurable, portable |
| **P0.3 Schema version + legacy snapshots** | Add `schema_version` to `plan.json`/`task.json`; freeze `tests/fixtures/legacy/`; load-and-assert tests | `migrate_record(version, record)` in-memory upgrade; disk format unchanged | **Compat safety net** |

### Phase 1 — Decouple the dispatchers (high value, medium risk)

| Slice | Target | Approach |
| --- | --- | --- |
| **P1.1 Web route registry** | Remove `handle_ui_client` if/elif chain (`server.py:186-211`) | Each `web/*.py` self-registers `ROUTES=[Route(method, prefix, handler)]`; `server.py` iterates only. Removes order-dependence; `server.py` stops importing every subsystem |
| **P1.2 Action-handler registry + split `execute_actions`** | Cut the 313-line function (`orchestrator.py`) to <80 | Mirror Phase 3/5 registry: each action type registers a handler; orchestrator becomes a dispatcher |

### Phase 2 — Typed boundaries (cohesion + compat spine)

| Slice | Target | Approach |
| --- | --- | --- |
| **P2.1 `schemas/` central field source** | Extend the Phase 2 projection helper to run/agent/config field names, defaults, projections | All consumers read through it; renames become one place |
| **P2.2 Read-only dataclass views** | store/domain edges return `TaskView`/`RunView`/`AgentView` wrapping the dict; disk stays dict | Consumers depend on named typed fields, not dict shape; no storage migration |
| **P2.3 Layering rule + check** | Document `cli → web/services → store/domain` one-way; add `scripts/check_layering.py` | Stop the orchestrator reverse-dependency tension from rotting |

### Phase 3 — Portability & Windows (the end-goal axis)

| Slice | Target | Approach | Unblocks |
| --- | --- | --- | --- |
| **P3.1 Cross-platform file locking** ⭐ | One locking abstraction replacing all `fcntl.flock` | `services/locking.py`: `fcntl` on POSIX, `msvcrt.locking`/portalocker-style on Windows; `store/io.py`, `store/runs.py`, `browser_runtime.py`, `browser_bookmarks.py`, `network_terminal.py`, `serial_lock.py` use it | **Core store works on Windows** |
| **P3.2 Cross-platform process control** | Replace `os.killpg`/`SIGTERM`/`geteuid` | `services/process_control.py`: POSIX signals/process-groups vs Windows `TerminateProcess`/`taskkill`; `observe_proxy`, `knowledge_routes`, `serial_lock` use it | Subprocess stop works on Windows |
| **P3.3 Platform module** | Abstract temp dir, default home, shell, OS detection, loopback | `services/platform.py`: `temp_dir()` (no hard `/tmp`), default shell (cmd/powershell on Windows), service-manager probe; fix `cli_parser.py:148` + `run_cleanup.py:35` + `local_terminal.py:23` | No platform literals in business logic |
| **P3.4 Portable config split** | Separate machine-local (bin paths, absolute workspace, proxy) from portable `config.json` | `local.json`/env layer; `~`/`$VAR` path tokens; env overrides `AHA_CODEX_BIN`/`AHA_WORKSPACE_ROOTS`; resolve at load | `~/.aha` copyable across machines |
| **P3.5 Backend provider resolver** | Decouple "codex/claude CLI must be on host PATH" | Extend backend registry into a provider interface + per-machine resolver (path/docker/remote) | No external-CLI hard dependency |
| **P3.6 Run-archive workspace portability** | Rewrite `workspace_path` to a token on export; resolve on import | Extend `run_archive.py` (currently rewrites run_id/session only) | Archives usable cross-machine |
| **P3.7 asyncio Windows policy** | Ensure subprocess-capable loop on Windows | Set `WindowsProactorEventLoopPolicy` on Windows at entry (subprocess support); no-op on POSIX | Backend subprocess launch on Windows |
| **P3.8 Hardware UART on COM ports** | Cross-platform serial transport + parent-death cleanup | Replace `termios`+`os.open` in `hardware_session.py` with **pyserial** (`serial.Serial`, auto `/dev/tty*`↔`COM*`); add pyserial as an optional `[hardware]` extra; replace `prctl/PDEATHSIG`+`preexec_fn` with a Windows **Job Object** (`JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE`) and keep the POSIX `PDEATHSIG` fallback; reuse the P3.1 lock + P3.2 process control | Hardware UART works on Windows |

### Phase 4 — Hardening

- **P4.1 Narrow the 71 broad `except Exception`** in background loops → `log.warning` + continue (no silent swallows).
- **P4.2 Compatibility migration shim** formalized; `aha runs diagnose` reports deprecated fields.
- **P4.3 Windows smoke** — a `scripts/smoke_windows.py` (or CI matrix job) that boots a temp home on Windows and exercises UI/health/task-create; systemd paths stay Unix-only.
- **P4.4 Docs/nav sync** — `architecture.md`, `repository-management.md`, KB nav reflect layering rule, config split, platform abstraction, Windows support.

## Windows Full-Functionality Readiness Checklist

| Capability | Blocking slice(s) | Status |
| --- | --- | --- |
| Process boots, store read/write | P3.1 + P3.3 + P3.7 ✅ done | **Boots on Windows** — package imports green under fcntl/pty/termios-blocked simulation; CLI `--help`/`init`/`runs cleanup` smoke pass |
| Backend subprocess launch | P3.7 ✅ done; P3.5 (resolver) pending | asyncio Windows policy set; external CLI resolution still Unix-path-bound |
| Stop/cleanup subprocesses | P3.2 ✅ done; local_terminal killpg (P3.9) | backend/browser/observe/headroom/serial/knowledge stop now cross-platform |
| Temp dirs / cleanup | P3.3 ✅ done | ready |
| Local terminal shell | P3.3 ✅ import-safe; runtime ConPTY (P3.9) | module imports on Windows; `start()` raises clear error until ConPTY |
| Config portable across machines | P3.4 ✅ done | `~`/`$VAR` tokens + `AHA_WORKSPACE_ROOTS`/`AHA_CODEX_BIN`/`AHA_CLAUDE_BIN` env overrides resolve at `load_config` |
| Run archive cross-machine | P3.6 ✅ done | import-time best-effort `workspace_path` relocation (backward compatible; schema unchanged) |
| Logging operable | P0.1 | blocked by `print` |
| Browser control | — (Playwright cross-platform) | ready |
| Hardware UART | P3.8 + P3.1 + P3.2 | blocked by `termios`/`os.open`/`prctl` |

## Compatibility Strategy (cross-cutting)

1. **Version stamps** — `schema_version` on plan/task; unknown future fields ignored (forward), missing fields defaulted (backward).
2. **Central field source** — `schemas/` is the single field-name origin.
3. **Frozen legacy snapshots** — `tests/fixtures/legacy/` + load assertions.
4. **Deprecation policy** — deprecate (keep + ignore) ≥1 release before removal; `runs diagnose` reports deprecated fields.
5. **Protocol contract tests** — extend `tests/test_protocol_contract.py` to action-envelope versioning.

## Verification (per slice + gate)

```bash
PYTHONPATH=src python3 -m pytest                       # full suite
PYTHONPATH=src python3 -m pytest tests/test_legacy_snapshots.py   # P0.3 compat net
PYTHONPATH=src python3 scripts/smoke_onebin_cli.py --json
PYTHONPATH=src python3 scripts/smoke_dual_ui_homes.py
PYTHONPATH=src python3 scripts/smoke_playwright_ui.py --require-playwright
PYTHONPATH=src python3 scripts/check_layering.py      # P2.3
# Windows gate (P4.3): run the same suite on a Windows host/CI runner
```

Acceptance gate for the end goal: the full `pytest` suite plus onebin/dual-home
smokes pass on **both** Linux and Windows, and a manual Windows run confirms
Web UI + codex/claude task + shared browser + hardware UART over a COM port +
KB + archive import/export.

## Sequencing & Quick Wins

1. **P0.3** (schema version + legacy snapshots) — zero-risk safety net first.
2. **P0.1 + P0.2** (logging + constants) — low risk, immediate ops/portability gain.
3. **P3.1** (cross-platform file lock) — **the keystone for Windows**; also a clean cohesion win. Can start in parallel with Phase 0 since it is self-contained.
4. **P1.1** (web route registry) — single-slice coupling reduction.
5. Then Phase 1 → 2 → remaining Phase 3 → 4 in order.

## Working Log

| Date | Status | Notes |
| --- | --- | --- |
| 2026-08-01 | Done | Authored strategic plan; set **Windows full-functionality** as end goal; baseline Windows blockers verified in code (`fcntl`, `geteuid`, `killpg`/`SIGTERM`, `/tmp`, `/bin/sh`); pinned from `docs/optimization-plan.md`. |
| 2026-08-01 | Done | Pulled hardware UART back into Windows scope: confirmed COM port is the UART equivalent, but current code uses `termios`+`os.open` (not pyserial) and `prctl/PDEATHSIG`; added P3.8 (pyserial transport + Windows Job Object parent-death) and a `termios`/`os.open` baseline blocker row; updated scope, readiness checklist, and acceptance gate. |
| 2026-08-01 | Done | **P3.1 cross-platform file locking landed.** New `src/aha_cli/locking.py` (POSIX `fcntl.flock` / Windows `msvcrt.locking`, uniform `BlockingIOError` on contended non-blocking acquire); converted all 7 `flock` sites — `store/io.py`, `store/runs.py`, `services/backend_runtime.py`, `services/browser_bookmarks.py`, `services/browser_runtime.py`, `services/network_terminal.py`, `services/hardware_bridge.py`. Added `tests/test_locking.py` (4 tests). Verified: `1060 passed` (baseline 1056 + 4), `compileall` clean, `git diff --check` clean. Remaining POSIX-only `fcntl` is `local_terminal.py` terminal control (`fcntl.fcntl`/`ioctl` + `pty`/`termios`), deferred to P3.9 (ConPTY). |
| 2026-08-01 | Done | **P3.2 cross-platform process control landed.** New `src/aha_cli/process_control.py` (`process_exists`, `current_uid`, `send_signal`, `signal_process_group`, `process_group_id`, `terminate_process`): POSIX path byte-identical to prior `os.kill`/`os.killpg`/`os.getpgid`; Windows path via `ctypes` (`TerminateProcess`/`OpenProcess`/`GetExitCodeProcess`) + `taskkill /T`. Converted 6 modules — `backend_runtime`, `browser_runtime`, `observe_proxy`, `headroom_integration`, `serial_lock`, `knowledge_routes` — including liveness probes (fixes the latent `os.kill(pid,0)` Windows-kill bug). Added `tests/test_process_control.py` (5 tests incl. real-child terminate). Verified: `1065 passed`, `compileall` clean, `git diff --check` clean. Remaining POSIX-only: `local_terminal.py` `killpg` (P3.9 ConPTY) and `browser_bridge.py` signal-handler registration. |
| 2026-08-01 | Done | **P3.3 + P3.7 + local_terminal import-safety landed — AHA now boots on Windows.** New `src/aha_cli/platform.py` (`is_windows`, `temp_root`, `candidate_temp_roots`, `default_shell`); removed hard-coded `/tmp`/`/var/tmp` (`run_cleanup`, `cli_parser`) and `/bin/sh` (`local_terminal`); made `local_terminal`'s `fcntl`/`pty`/`termios` lazy with a clear `start()` guard until ConPTY (P3.9); set `WindowsProactorEventLoopPolicy` at CLI entry (P3.7). Verified: **Windows import simulation green** (`sys.modules[fcntl/pty/termios]=None` → `aha_cli.cli`/`web.server`/`local_terminal` import clean), CLI `--help`/`init`/`runs cleanup` smoke pass, `1069 passed` (+4 `test_platform`), `compileall` + `git diff --check` clean. No module-level POSIX-only imports remain in the package. |
| 2026-08-01 | Done | **Cross-platform parent-death / bridge spawn landed (P3.8 parent-death half).** `process_control` gained `parent_death_preexec()` (POSIX `prctl PDEATHSIG` callable / Windows `None`) and `assign_parent_death(proc)` (Windows kill-on-close Job Object via `ctypes`; POSIX no-op). Converted all 4 `preexec_fn` bridge-spawn sites — `browser_external`, `browser_runtime`, `network_terminal`, `hardware_bridge` — removing the Windows `preexec_fn is not supported` `ValueError` crash. `hardware_bridge.set_parent_death_signal` kept as a backward-compatible alias delegating to `process_control`. Verified: `1071 passed` (+2 `test_process_control`), Windows import sim still green, `compileall` + `git diff --check` clean. (Windows Job Object runtime pending a Windows runner; POSIX path fully covered.) |
| 2026-08-01 | Done | **P3.4 portable config landed.** `platform.expand_path` (cross-platform `expanduser`+`expandvars`); `load_config` now resolves `~`/`$VAR` tokens in `workspace_roots`, `codex.bin`, `claude.bin` and applies env overrides `AHA_WORKSPACE_ROOTS` (pathsep-split), `AHA_CODEX_BIN`, `AHA_CLAUDE_BIN`. Saves use form input (not the resolved dict), so tokens never leak to disk. Added `tests/test_portable_config.py` (3) + `test_platform` expand tests. Verified: `1080 passed`. |
| 2026-08-01 | Done | **P3.6 run-archive workspace portability landed.** Import now relocates an imported task's `workspace_path` when the absolute path is absent on the target machine and exactly one configured `workspace_root` subdir shares its basename; otherwise value preserved (backward compatible, `ARCHIVE_SCHEMA` unchanged). Threaded a `_make_workspace_resolver` through `_transform_import`. Added `tests/test_run_archive_workspace.py` (5). Verified: `1080 passed`, Windows import sim green, `compileall` + `git diff --check` clean. |
