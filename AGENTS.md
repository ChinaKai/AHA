# Repository Guidelines

## Project Structure & Module Organization

AHA is a Python CLI and local web UI prototype. Source code lives in `src/aha_cli/`. Keep CLI argument wiring in `cli.py` and parser setup in `cli_parser.py`. Persistence belongs in `store/`, domain construction in `domain/`, backend integrations in `backends/`, long-running orchestration in `services/`, HTTP routes in `web/`, WebSocket handling in `websocket/`, and browser assets in `src/aha_cli/web/static/`.

Tests live in `tests/` and are grouped by behavior, for example `tests/test_store_state.py`, `tests/test_web_task_api.py`, and `tests/test_backend_runtime.py`. Documentation is under `docs/`; helper scripts are under `scripts/`; runnable examples are under `examples/`.

## Build, Test, and Development Commands

- `python3 -m pip install -e .`: install the package in editable mode and expose the `aha` console script.
- `python3 -m pytest`: run the full test suite.
- `python3 -m pytest tests/test_web_task_api.py`: run a focused test file while iterating.
- `python3 -m aha_cli init --portable`: initialize a local `.aha/` directory for manual testing.
- `python3 scripts/build_onebin.py --output dist/aha`: build the single-file executable zipapp.
- `aha ui <run-id>` or `python3 -m aha_cli ui <run-id>`: start the local dashboard for a run.

## Coding Style & Naming Conventions

Use Python 3.10+ with 4-space indentation, type hints where practical, and `from __future__ import annotations` for new modules. Follow existing naming: modules and functions use `snake_case`, classes use `PascalCase`, tests use `test_<behavior>.py` and `test_<expected_behavior>`. Prefer `pathlib.Path` and the structured helpers in `store/io.py` for JSON, JSONL, and text persistence. Keep source files below 1000 lines and split mixed responsibilities before adding more logic.

## Testing Guidelines

Tests use `unittest` style assertions and mocks, run through pytest. Add focused coverage for protocol changes, task/agent/session routing, archive compatibility, and old `.aha/runs/<run-id>/plan.json` behavior. Preserve the deterministic stub backend so tests do not require external agent services.

## Commit & Pull Request Guidelines

Recent history uses Conventional Commit subjects such as `fix(web): ...` and `feat(aha): ...`. Prefer `aha commit --type <type> --scope <scope> --summary "<summary>"` and validate hand-written messages with `aha commit-check .git/COMMIT_EDITMSG`.

Pull requests should describe the behavior change, list verification commands, link related issues when available, and include screenshots for visible web UI changes. Update `README.md` and relevant `docs/` files when CLI, Web API, storage layout, archive, packaging, proxy, or session behavior changes.
