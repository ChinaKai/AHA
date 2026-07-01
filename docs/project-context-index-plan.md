# AHA Project Context Index Plan

This document tracks the proposed AHA best-practice path for coding-oriented
token savings across embedded Linux, C/C++, Python, and future cloud stacks.
It is a live discussion document: when the design discussion makes a concrete
decision, changes scope, adds constraints, or reprioritizes implementation
steps, update this file in the same task turn.

## Goal

Reduce coding-agent token use by helping agents read less but read more
accurately. The preferred strategy is:

```text
Project navigation + generated project context index + just-in-time file reads
```

This should replace "compress everything" as the primary coding workflow.
Headroom remains useful as an optional provider/proxy experiment, but it should
not be the default token-saving strategy for coding tasks where exact code,
configuration, build rules, and call paths matter.

## Current AHA Fit

AHA already has the right foundation:

- Knowledge retrieval is reference-first: it injects KB roots, project keys,
  navigation paths, and short summaries, then asks the agent to read full files
  by path when needed.
- Project navigation provides `navigation/index.md` plus module and flow docs
  that agents can read on demand before broad code search.
- Sticky prompt handling tracks delivered context fingerprints and only
  reinjects changing context during sticky sessions.
- Context pressure metrics already separate current AHA prompt cost from
  backend sticky-history cost.

The missing layer is a generated, multi-language code and build index that can
rank the most relevant paths, symbols, build files, config keys, and device-tree
entries for the current task without injecting the full repository.

## Design Principles

- Do not bind the solution to Python. Python AST can be one extractor, but
  embedded Linux and C/C++ must be first-class.
- Prefer deterministic local tooling before model-generated summaries.
- Keep generated indexes out of normal prompts by default. When task token
  saving is enabled and an existing map is available, inject only a compact map
  path plus usage rules; do not auto-inject query results.
- Treat generated indexes as cache/evidence, not curated long-lived knowledge.
  Human- or agent-curated navigation docs remain the durable KB surface.
- Degrade gracefully when optional tools such as `universal-ctags`, `cscope`,
  GNU GLOBAL, or clang tooling are unavailable.
- Preserve coding accuracy over aggressive token reduction.

## Proposed Architecture

Add a `Project Context Index` layer beside the existing knowledge navigation
layer:

```text
Knowledge navigation
  Human/agent-readable route map stored in KB navigation markdown.

Project Context Index
  Machine-generated local cache with files, symbols, build rules, config keys,
  device-tree entries, tests, and entry points.

Repository profiles
  Repo-type-aware generation, budgeting, ranking, and reference formatting.
  A Buildroot external tree, Linux kernel tree, U-Boot tree, C application,
  Python service, and cloud repo should not share one generic map template.

Prompt injection
  Compact map capability note containing the local map entry point and rules
  for focused query/read flow.
```

Suggested module:

```text
src/aha_cli/services/project_context_index.py
```

Suggested public functions:

```python
detect_project_flavor(workspace) -> list[str]
collect_project_files(workspace, config) -> list[dict]
build_project_context_index(root, workspace, project_key, config) -> dict
load_project_context_index(root, project_key) -> dict
query_project_context_index(index, terms, limits) -> dict
format_project_context_reference(matches, budget_chars) -> str
```

## Repository Profiles

Map generation should be profile-driven, not one-size-fits-all. Project flavor
detection should choose one or more repository profiles, then each profile
controls extractors, path priorities, budgets, relationships, ranking weights,
and reference formatting.

Initial profiles:

- `buildroot-external`: `platform/`, `configs/*_defconfig`, package
  `Config.in`, package `.mk`, board image scripts, overlays, prebuild metadata,
  and app/BSP inputs are first-class. Upstream `buildroot-dist/` is useful but
  should not dominate local platform records.
- `linux-kernel`: prioritize `drivers/`, `arch/`, `include/`, Kconfig,
  Kbuild/Makefile, DTS/DTSI, defconfigs, MAINTAINERS, and subsystem-local
  tests/tools.
- `uboot`: prioritize board configs, `include/configs`, `configs/*_defconfig`,
  `board/`, `arch/`, driver model paths, DTS, Kconfig, and Makefiles.
- `embedded-c-app`: prioritize application entry points, service registration,
  product variants, local SDK headers, build scripts, and platform abstraction
  layers.
- `python-service` / `node-cloud` / `go-service`: prioritize package metadata,
  imports/exports, service entry points, Docker/Compose/Kubernetes/Terraform,
  CI workflows, and tests.

Profiles can stack. For `fw_omni_builder`, the effective profile is closer to
`buildroot-external + embedded-c-app + submodule-aware vendor inputs` than plain
Buildroot. This is why path budgets and package aggregation must be
repo-specific instead of global.

Current implementation:

- profile detection runs during file collection and records `profiles` in the
  manifest
- path priorities and file/record budgets are profile-aware
- status output shows active profiles so the user can tell why generation and
  ranking behaved a certain way
- implemented profiles currently include `buildroot-external`, `linux-kernel`,
  `uboot`, `embedded-c-app`, `python-service`, and `cloud`

Current detection rules are deliberately simple and inspect the collected
project paths plus detected file kinds:

- `buildroot-external`: the repo has `platform/` and Buildroot-style `.mk` or
  `Config.in` files under `platform/` or `buildroot-dist/`
- `linux-kernel`: the repo has `MAINTAINERS`, `drivers/`, and `arch/`
- `uboot`: the repo has `configs/*_defconfig` plus either `board/` or
  `include/configs/`
- `embedded-c-app`: collected files include C/C++/assembly, or `app_source/`
  contains C-family sources
- `python-service`: collected files include Python, or `pyproject.toml` exists
- `cloud`: collected files include cloud/deploy markers, or
  `.github/workflows/` exists
- fallback is `generic`

Profiles can stack, and the Web Map tab displays the generated profile list
beside project, workspace id, and flavors.

Map tab count presentation should also be profile-aware. The backend should keep
one stable count schema (`files`, `packages`, `symbols`, `build`, `configs`,
`device_tree`, `tests`, `entry_points`, `extractor_errors`), while the UI chooses
labels, ordering, and visibility from the active profiles:

- `buildroot-external`: emphasize Packages, Defconfigs/Config, Build vars,
  App/BSP files, DTS, Symbols, and Errors
- `linux-kernel`: emphasize Drivers/files, Kconfig, DTS, Symbols, Kbuild, Tests,
  and Errors
- `uboot`: emphasize Board configs, DTS, Kconfig, Drivers/files, Build, and
  Errors
- `embedded-c-app`: emphasize App files, Entry points, Symbols, Build, Tests,
  and Errors
- `cloud`: emphasize Services/entry points, Deploy/CI records once indexed,
  Tests, Files, and Errors

This is display logic only. It should not create a different storage schema per
profile. Current implementation chooses one display-primary profile in this
order to avoid duplicate labels when profiles stack:

```text
buildroot-external > linux-kernel > uboot > embedded-c-app > cloud > python-service > generic
```

For example, a repo with `buildroot-external + embedded-c-app` uses the
Buildroot-oriented count layout because package/config/build visibility is more
important for that map status view.

## Index Schema

First version:

```json
{
  "schema_version": 1,
  "project_key": "...",
  "workspace": "...",
  "git_head": "...",
  "generated_at": "...",
  "flavors": ["embedded-linux", "c", "kconfig"],
  "tools": {
    "ctags": {"available": true, "version": "..."},
    "cscope": {"available": false},
    "global": {"available": false}
  },
  "files": [],
  "packages": [],
  "symbols": [],
  "build": [],
  "configs": [],
  "device_tree": [],
  "tests": [],
  "entry_points": []
}
```

Generated cache location should avoid KB churn. Store it under AHA home runtime,
not inside reviewed knowledge entries and not inside a specific run:

```text
<aha-home>/runtime/project_context/<project-key>/<workspace-id>/index.json
<aha-home>/runtime/project_context/<project-key>/<workspace-id>/summary.md
```

`project-key` keeps the cache aligned with project knowledge. `workspace-id` is
a short hash of the resolved local workspace path, so two checkouts of the same
remote do not overwrite each other. `index.json` is the canonical machine
format. `summary.md` is generated from the JSON for human/debug inspection and
must be treated as disposable.

If the index later needs to survive runtime cleanup, add a dedicated ignored
cache directory under the knowledge root rather than storing generated JSON as
reviewed KB entries.

## Embedded Extractors

Prioritize the user's common embedded Linux stack.

### C/C++

- Prefer `universal-ctags --output-format=json` for functions, macros, structs,
  enums, typedefs, globals, and file locations.
- If ctags is unavailable, fall back to simple local regex extractors for:
  function definitions, `#define`, `struct`, `enum`, `typedef`, and obvious
  global symbols.
- Optionally record cscope/GNU GLOBAL availability and database paths when
  present.
- Use `compile_commands.json` when available for future clangd/clang-tooling
  integration, but do not require it for kernel/u-boot/buildroot projects.

### Kernel / U-Boot

- Parse `Kconfig` and `Config.in` for `config`, `menuconfig`, `source`,
  `depends on`, `select`, `default`, and help blocks.
- Parse `Makefile` and `Kbuild` for `obj-y`, `obj-m`, targets, and includes.
- Parse `defconfig` files for `CONFIG_*` values.
- Parse `.dts` and `.dtsi` files for include relationships, node names, labels,
  and `compatible` strings.

### Buildroot

- Parse `package/*/*.mk`, `package/*/Config.in`, `board/*`, and
  `configs/*_defconfig`.
- Extract package names, dependencies, selected configs, board names, image
  scripts, and overlay paths when detectable.

## Future Extractors

Add these after the embedded/C path is useful:

- Python: stdlib `ast` for classes/functions/imports; optional pyright later.
- Go: `go list`/`gopls` metadata when available.
- TypeScript/JavaScript: tsserver/tree-sitter style file and export mapping.
- Rust: `cargo metadata` and rust-analyzer-compatible structure.
- Cloud: Dockerfile, Compose, Kubernetes YAML, Helm, Terraform, CI workflow
  files, service manifests, and deployment entry points.

## Query And Ranking

Input terms should come from the task title, task description, current user
message, and optionally selected files from the current round.

Search should be fuzzy enough for coding workflows. Treat exact match as the
highest-confidence case, but do not require full-token equality. Embedded
queries often mix package names, board names, config symbols, abbreviations,
and partial paths.

Candidate matching should include:

- exact symbol/config/file-name match
- case-insensitive substring match
- snake-case, kebab-case, slash-path, and camel-case tokenization
- prefix and acronym-ish matches, for terms such as `ss306`, `wvs`, `fwlocal`,
  or `sigmastar wyze`
- path segment match
- optional typo-tolerant fuzzy score using a no-dependency algorithm first
  (for example character n-gram overlap or `difflib`), with external fuzzy
  libraries only as optional accelerators

Rank matches by:

- exact package/symbol/config/file-name match
- profile-specific record type priority
- path segment match
- module/navigation relevance
- build/config/device-tree relationship
- test proximity
- recent changed files when available

Output should be capped aggressively:

```text
max files: 8
max symbols/configs: 12
max device-tree/build entries: 8
max injected chars: 1200 by default
```

## Prompt Injection

Do not choose between fully manual reference adoption and automatic injection
for every user message. Prefer agent-triggered retrieval.

The normal task prompt should include only a tiny stable capability note:

```text
Project map is available. If repository structure, symbols, Buildroot/Kconfig,
or DTS context is needed, request an AHA map query with focused terms.
```

The agent can then request retrieval when it decides the task needs map
context:

```json
{"type": "map_query", "terms": "sigmastar_wyze_app"}
```

AHA handles the request, runs the shard-native map query, and returns a compact
reference block only if there are matches. No-match queries should not inject a
reference into the agent prompt.

When a reference is returned, it should be compact and path-oriented, not the
index body:

```text
Project context index:
- flavor: embedded-linux, c, kconfig, dts
- index: <aha-home>/runtime/project_context/<project-key>/<workspace-id>/index.json
- matched files:
  - drivers/net/foo.c
  - drivers/net/Kconfig
  - arch/arm/boot/dts/foo.dtsi
- matched symbols/configs:
  - foo_probe() drivers/net/foo.c:123
  - CONFIG_FOO_NET drivers/net/Kconfig:18
完整内容请按 path 主动读取。
```

For sticky sessions, use a fingerprint of the matched reference block and avoid
reinjecting it unless the index or match set changed.

## CLI Integration

Extend project navigation commands rather than adding an unrelated workflow:

```bash
aha kb map build --workspace . --with-code-index
aha kb map build --workspace . --refresh-index
aha kb map build --workspace . --flavor embedded
```

Potential dedicated diagnostics command:

```bash
aha kb map index --workspace . --status
aha kb map index --workspace . --refresh
aha kb map index --workspace . --query "eth phy reset"
```

The final command shape should follow the existing CLI parser style before
implementation.

## Slash Commands

Keep the existing task-conversation slash commands unchanged:

```text
/aha kb <message>
/aha nav <message>
```

Those commands ask the current backend agent to emit KB/navigation candidates
from its sticky session context. They intentionally tell the agent not to scan
the workspace. Project Context Index is different: it is an AHA runtime
operation that scans files and writes a local generated cache.

If slash support is added, use a separate runtime-handled command family named
`/aha map`. The term "map" matches project navigation and repo-map usage better
than "index", which can be confused with many unrelated UI, search, or database
concepts.

```text
/aha map status
/aha map refresh
/aha map query <terms>
```

Default workspace should be the selected task workspace. Later variants can
accept an explicit workspace:

```text
/aha map status --workspace /path/to/repo
/aha map refresh --workspace /path/to/repo
/aha map query "eth phy reset"
```

These commands should not route to the backend agent. They should be handled by
AHA itself, append an `aha_command_handled` event, and return a concise browser
message with status/job/query results. `refresh` should start a background job
instead of blocking the chat turn.

Do not overload `/aha kb map ...` or `/aha kb index ...` in the first version.
The existing `/aha kb` shape takes arbitrary natural-language feedback, so
adding subcommands under it would be ambiguous and could accidentally route map
operations to the agent.

## Web Flow

Use the existing Knowledge console instead of adding a new top-level app. The
main dashboard already opens `/static/knowledge.html` as the Knowledge view, and
the Knowledge console already has a Project navigation tab with workspace
selection, project-nav generation, draft polling, preview, approve, reject, and
stop actions.

Add Project Context Index as a panel inside the Project navigation tab.

### User Flow

1. User opens AHA Web UI.
2. User switches to Knowledge.
3. User opens Project navigation.
4. User selects a workspace.
5. UI loads index status for that workspace.
6. If no index exists or it is stale, UI shows `Build index` / `Refresh index`.
7. User starts index build.
8. Backend creates a deterministic background index job and returns a job id.
9. UI polls job/status while showing progress, current phase, counts, and errors.
10. On completion, UI shows flavors, git head, generated time, counts, tool
    availability, cache path, and a short generated summary.
11. User can run a test query to see matched files/symbols/configs without
    injecting anything into task prompts.
12. Later, Project nav generation can optionally use the existing fresh index to
    produce better navigation drafts.

### First Web Slice

The first Web slice should expose status and manual refresh only:

- workspace selector reused from Project navigation
- status fields: `missing`, `fresh`, `stale`, `building`, `failed`
- project key, workspace id, git head, generated at, index path
- flavors and count summary
- optional tool status
- `Build index` / `Refresh index`
- background job poll
- `View summary` using generated `summary.md`

Do not wire the index into normal task prompt injection in the first Web slice.

### Suggested API Shape

```text
GET  /api/kb/project-context-index?workspace_path=...
POST /api/kb/project-context-index/refresh
GET  /api/kb/project-context-index/job?id=...
POST /api/kb/project-context-index/job/stop
POST /api/kb/project-context-index/query
```

`query` can be added after the basic status/build flow if that keeps the first
slice smaller.

### Background Job Model

Index generation should not run inline in the HTTP request. Kernel/buildroot
trees can be large, so the route should create a job record and dispatch a
background worker, mirroring the current project-navigation draft polling
pattern. The job record can live under:

```text
<aha-home>/runtime/project_context_jobs/<job-id>.json
```

The canonical output remains:

```text
<aha-home>/runtime/project_context/<project-key>/<workspace-id>/index.json
```

### Project Navigation Integration

After the status/build flow exists, add an optional checkbox near `Generate
project nav`:

```text
Use Project Context Index
```

When checked, project-nav generation reads the fresh index summary and selected
high-level routes as evidence for the navigation agent. It should not paste a
symbol dump into the prompt.

## Knowledge Navigation Integration

Use the generated index to improve navigation candidates:

- enrich `navigation/index.md` with better first-level module routes
- enrich `navigation/modules/*.md` with key files, entry points, tests, caveats,
  Kconfig/Makefile/DTS links, and diagnostic paths
- avoid writing generated symbol dumps into reviewed navigation docs
- keep ordinary task deltas incremental: only update affected module/flow docs

## Relationship To Navigation Entries

Project Context Index is not a second kind of KB entry and should not be stored
as `projects/<project-key>/navigation/*.md`.

Treat the two layers differently:

```text
navigation/*.md
  Curated, durable, human/agent-readable route map. Reviewed through the KB
  candidate flow and safe to sync through the knowledge git repo.

runtime/project_context/.../index.json
  Generated, local, disposable machine index. Not reviewed, not synced as KB,
  not counted as a navigation entry, and not manually edited.
```

The navigation index may mention the concept of a generated context index in a
stable way if needed, but it should not contain machine-local cache paths such
as `<aha-home>/runtime/...`; those paths are non-portable and churn-prone.

Prompt assembly can show a separate "Project context map" capability block
beside the normal task context when task token saving is enabled and an existing
map is present for the task workspace. The block should include the map path,
project/workspace identity, freshness metadata, and instructions to query or
inspect the map before broad repository search. It must not inject query
results automatically, and if a query has no useful result the agent should
continue normally without adding reference text.

Project-nav generation can also use the generated index as evidence to produce
better curated navigation candidates. In that case, the durable nav docs should
keep only stable, useful routes such as key files, build/config entry points,
diagnostic paths, and caveats, not raw symbol dumps or cache links.

## Metrics-Driven Context Strategy

Keep Headroom separate from AHA-native context control.

Use existing context pressure fields:

- `aha_overhead_ratio` high: optimize current AHA prompt injection.
- `estimated_backend_history_tokens` high: suggest phase checkpoint or explicit
  compact/reset.
- `runtime_percent >= 70`: mark watch.
- `runtime_percent >= 85`: warn and suggest explicit checkpoint/reset.

Do not restore turn-end implicit compact/reset. Coding continuity is more
important than automatic aggressive token reduction.

## Headroom Policy

Headroom should remain available, but not as the default coding policy:

- useful for long research, long documents, and low-risk summarization
- risky for kernel/u-boot/buildroot/C tasks where exact build/config/code
  context matters
- should be labeled as an optional experimental provider/proxy integration
- should not be selected by task token saving; task token saving now selects
  project-map prompt guidance

## Accepted Token-Saving Prompt Integration

Task token saving is now map-linked:

- Default task token saving provider is `map`.
- Enabling task token saving does not require Headroom and does not wrap Codex
  through a Headroom proxy.
- If task token saving is enabled and the task workspace has an existing
  project context map, the normal prompt receives a compact map capability
  block.
- Sticky sessions receive the map capability block only through the existing
  context fingerprint delta path when the block is newly enabled or changed.
- The prompt includes map path, project key, workspace id, status,
  generated-at, counts, flavors, profiles, and the rule to use focused
  `/aha map query <terms>` searches or inspect the map index/shards directly.
- No map query results are auto-applied to the user's next message. If no map
  result is relevant, AHA should send the user message normally.

## Immediate First Step

The first implementation step should be P0/P1 scaffolding, not ctags, prompt
injection, or Headroom changes.

Implement the smallest vertical slice that can build and inspect a deterministic
index cache without changing normal task prompts:

- add project-context-index config defaults, disabled by default
- add `src/aha_cli/services/project_context_index.py`
- define cache paths under
  `<aha-home>/runtime/project_context/<project-key>/<workspace-id>/`
- implement project flavor detection and `git ls-files` based file collection
- implement basic ignore and file-size limits
- write `index.json` with schema/version/git head/flavors/files/tool status
- add a CLI/debug path or service-level API that can build/query the index in
  tests without wiring it into prompt injection yet
- add tests for a mini embedded-style tree and cache invalidation basics

This first slice proves the storage shape, invalidation model, and repository
walking behavior before adding C/Kconfig/DTS/Buildroot extractors. Prompt
injection should wait until the index is useful enough to rank references.

### Current Implementation Slice

The first inspectable slice is implemented as of this task:

- `src/aha_cli/services/project_context_index.py` builds the local runtime cache
  and writes `index.json` plus `summary.md`.
- Config defaults live under `knowledge.project_context_index`, with prompt
  injection disabled.
- `/aha map status`, `/aha map refresh`, and `/aha map query <terms>` are
  handled by AHA in the task conversation instead of being routed to the
  backend agent.
- The Web slash-command menu exposes the concrete subcommands directly:
  `/aha map status`, `/aha map refresh`, and `/aha map query`. It does not show
  a separate `/aha map` parent item or default to `status`.
- Tests cover an embedded-style mini tree with C, Kconfig, DTS, Makefile, cache
  files, status, path query, and stale detection when git worktree state
  changes.

This slice is intentionally conservative: `refresh` is still synchronous in
the slash command, and `query` ranks only file path/kind/extension matches.
Symbol extraction, Kconfig/DTS/Buildroot parsing, background Web jobs, and
normal prompt-reference injection remain follow-up work.

### Framework Backbone Status

P1 framework backbone is now in place:

- `create_project_context_index_document()` builds the in-memory index document.
- `write_project_context_index()` persists `index.json` and generated
  `summary.md`.
- `run_project_context_extractors()` executes a flavor-matched extractor
  registry and records per-extractor `ok`, `skipped`, or `failed` status.
- Built-in C/Kconfig/build/DTS/Python/cloud extractors currently return empty
  results by design; they are extension points, not accuracy work yet.
- Stale detection covers schema version, git head, dirty git worktree state,
  and bounded file fingerprints for non-git workspaces.
- Tests cover registry wiring, empty extractor behavior, extractor failure
  isolation, unreadable index failure status, git stale detection, and non-git
  stale detection.

## Framework-First Roadmap

The next implementation direction is framework-first. Do not optimize extractor
accuracy or prompt injection until the generated-index lifecycle is clean,
observable, and easy to extend.

### P1 Framework Backbone

Build the stable internal shape before adding deep language intelligence:

- split the service into explicit stages: discover files, detect flavors, run
  extractors, build records, write cache, query cache
- define an extractor registry with per-flavor extractors that can return empty
  results safely
- make status/invalidation metadata consistent across git repos, dirty
  worktrees, and non-git workspaces
- keep schema versioning and migration/error handling explicit
- keep `/aha map status|refresh|query` as the thin manual debug surface
- add tests for registry wiring, empty extractors, stale/failed status, and
  non-git fallback behavior

### P2 Web And Runtime Job Flow

After the service boundary is stable, move long-running work out of chat turns:

- add project-context-index API routes for status, refresh job, job polling,
  stop, and query
- add runtime job records under
  `<aha-home>/runtime/project_context_jobs/<job-id>.json`
- add a Knowledge console Project navigation panel for status, refresh,
  progress, summary view, and query preview
- keep slash commands as quick diagnostics, backed by the same service/API
  model where practical

P2 minimum framework is now implemented:

- API routes:
  - `GET /api/kb/project-context-index`
  - `POST /api/kb/project-context-index/refresh`
  - `GET /api/kb/project-context-index/job`
  - `POST /api/kb/project-context-index/job/stop`
  - `POST /api/kb/project-context-index/query`
- refresh uses pollable runtime job records under
  `<aha-home>/runtime/project_context_jobs/<job-id>.json`
- job records store only summary, counts, paths, and status metadata; they do
  not duplicate the full generated index
- Knowledge console has an independent Map tab for status, refresh, stop, and
  query preview. It is intentionally separate from Project navigation, because
  Map is a local runtime cache/debug surface while Project navigation produces
  durable KB navigation entries.
- tests cover the API refresh/job/query path and static UI wiring

Performance guardrail added after Web latency feedback:

- regular Web status calls use a fast `head-only` check: read existing
  `index.json` metadata and compare schema/git head only
- Web status must not run `git status`, ctags, source parsing, or bounded
  filesystem scans unless the user starts refresh or passes an explicit deep
  diagnostic flag
- full worktree stale detection remains available for CLI/slash diagnostics and
  `deep=1` API calls
- the Knowledge console waits for workspace options before entering the active
  Map/Project navigation tab, avoiding duplicate initialization fetches and the
  transient "select workspace" state
- refresh remains explicit and backgrounded; job records keep only summary,
  counts, paths, and status metadata

Follow-up Web performance pass:

- main dashboard bootstrap reuses one `list_run_summaries()` result instead of
  scanning run plans twice
- run summaries read AHA config once per run-list operation instead of once per
  run
- Knowledge status keeps `stale` counting metadata-only so it does not read all
  entry bodies on page open
- Knowledge entries API supports `limit` and `offset`; the Web entries tab uses
  a bounded first page and a `Load more` action
- Knowledge entries without search read only frontmatter summaries; full body
  reads are reserved for explicit search or opening/editing a specific entry
- Task memo default list requests skip building a full filtered copy when there
  is no search/filter/include id; they enrich only the returned first page and
  return the total from the loaded memo list

### P3 Extractor Accuracy

Only after the framework is usable, add embedded-focused extractors:

- C/C++ regex fallback first, optional ctags later
- Kconfig and Config.in records
- Makefile, Kbuild, Buildroot `.mk` records
- DTS/DTSI labels, includes, nodes, and compatible strings
- ranking that combines path, symbol/config/build/device-tree evidence

First embedded extractor pass is implemented:

- C/C++/headers use conservative regex extraction for functions, macros,
  structs, enums, and unions. This is a fallback-quality parser, not a full C
  parser; real kernel/u-boot test data should drive the next round of tuning.
- Kconfig/Config.in extraction records `config`/`menuconfig` names, prompt,
  `depends on`, and `select` relationships.
- Makefile/Kbuild/Buildroot `.mk` extraction records includes, `obj-*`/`lib-*`
  style Kbuild assignments, and common Buildroot package variables.
- DTS/DTSI extraction records includes, compatible strings, labels, and simple
  node declarations.
- Query now ranks files plus `symbols`, `configs`, `build`, `device_tree`,
  `tests`, and `entry_points`; slash/Web output can show section matches.
- A `max_records_per_extractor` limit protects the single-file JSON format
  during first real-repo testing.

### P4 Prompt Reference Injection

Prompt injection is last. It should inject only compact matched references, not
raw index content, and should remain disabled by default until query/ranking is
good enough to help real coding tasks.

## Storage And Generation Decisions

### Storage Format

Use `index.json` as the only canonical data source for generated context. The
first version should be a single JSON object with stable top-level sections:

```text
schema_version, project_key, workspace, workspace_id, git_head, generated_at,
flavors, tools, limits, files, packages, symbols, build, configs, device_tree,
tests, entry_points
```

Write JSON with deterministic ordering where practical so tests can compare it.
Do not store full source contents in the index. Store paths, line numbers,
symbol/config names, short snippets only when needed, file size, mtime, and
hashes for invalidation/debugging.

Generate `summary.md` from `index.json` for humans and UI diagnostics. It is not
loaded into prompts and should never be manually edited.

The first framework slice used one `index.json` so the service/API/UI contract
could stabilize quickly. Real `fw_omni_builder` testing proved that
Buildroot-scale repositories need a multi-file layout: the generated index was
still readable, but one section-level cap let `buildroot-dist/` crowd out
project-specific `platform/` build records.

Current storage decision:

- keep one canonical manifest named `index.json` for status, counts,
  fingerprints, and shard pointers
- keep the schema split-ready by preserving stable logical sections:
  `files`, `symbols`, `build`, `configs`, `device_tree`, `tests`,
  `entry_points`, and `packages`
- do not require Web status to read record shards; status must read only the
  manifest and optional generated summary
- split records by repository and section once real-repo extraction is enabled
- preferred split shape:
  - `index.json`: manifest, schema version, project/workspace identity,
    fingerprints, counts, flavors, tool status, section file pointers
  - `files.jsonl`: file records
  - `packages.jsonl`: Buildroot/package-level aggregate records
  - `symbols.jsonl`: C/C++/Python/Go/Rust/etc. symbol records
  - `build.jsonl`: Makefile/Kbuild/Buildroot build relationships
  - `configs.jsonl`: Kconfig/Config.in/defconfig records
  - `device_tree.jsonl`: DTS/DTSI labels, compatibles, includes, nodes
  - `summary.md`: human diagnostic summary generated from the manifest/records
- query/prompt code should depend on service APIs, not direct JSON layout, so
  the storage can move from single-file to sharded JSONL without changing UI or
  prompt callers

### Large Repository And Multi-Repo Layout

For large embedded repositories, treat the workspace as a repo graph rather than
one flat file list.

Discovery should run before extraction:

1. detect whether the selected workspace is inside a git repo with
   `git rev-parse --show-toplevel`
2. read `.gitmodules` and `git submodule status --recursive` when present
3. detect nested `.git` repositories that are not declared submodules and mark
   them as nested repos
4. create a repo manifest with root repo, submodules, nested repos, git heads,
   dirty flags, paths, and per-repo file counts

The runtime directory should keep one workspace manifest and one record area per
repo:

```text
project_context/<project-key>/<workspace-id>/
  index.json                  # workspace manifest only
  summary.md                  # generated human summary
  repos/root/files.jsonl
  repos/root/symbols.jsonl
  repos/root/build.jsonl
  repos/root/configs.jsonl
  repos/<submodule-id>/files.jsonl
  repos/<submodule-id>/symbols.jsonl
  ...
```

The manifest should describe all shards and counts. Query code should first use
the manifest and lightweight per-repo metadata to choose likely shards, then
load only those shards. Prompt injection should reference matched records, not
raw shards.

Budgeting must be per repo and per top-level path, not only per extractor. For
Buildroot-style trees, prefer local product and platform paths before vendor
or upstream distribution paths:

```text
platform/
app_source/
fw_bsp/
prebuild metadata
docs/
buildroot-dist/
third_party/vendor trees
```

This avoids `buildroot-dist/` consuming all build/config capacity before
project-specific external-tree packages are indexed.

### Clean Scan Strategy

Do not scan arbitrary filesystem contents directly for large git repositories.
The default generation mode should be `git-tracked-scan`:

- use `git ls-files -z` per repo/submodule
- exclude ignored, untracked, build output, and temporary files by construction
- include tracked dirty files from the current working tree so the map remains
  useful during active coding
- record dirty state and file fingerprints so status can explain staleness

Creating a separate clean worktree is useful, but should be an explicit
deterministic snapshot mode rather than the default:

```text
git-clean-snapshot
  Build from HEAD in a temporary detached worktree.
  Excludes local edits and generated files.
  More reproducible, but slower and disk-heavy for Buildroot/submodule repos.
```

Use clean snapshot mode for CI-style indexing, expensive full refreshes, or
debugging stale maps. Use git-tracked-scan for normal interactive AHA coding
because it is cheaper and can see the tracked files the engineer is currently
editing.

### Implemented Large-Repo Slice

The first large-repository slice is implemented and `fw_omni_builder` is now
the acceptance repository for map behavior.

Implemented behavior:

- default git generation mode is `git-tracked-scan`; untracked build outputs
  are excluded unless config explicitly enables them
- repo discovery records the root repo plus initialized and uninitialized
  recursive submodules from `git submodule status --recursive`
- generated storage is now a small `index.json` manifest plus JSONL shards under
  `repos/<repo-id>/<section>.jsonl`
- service callers still receive the logical index shape through
  `load_project_context_index(..., hydrate=True)` only when explicitly needed
  for diagnostics
- Web and slash query paths use shard-native cache query and stream JSONL
  records directly; they do not hydrate all shards back into a large in-memory
  index first
- query responses now include a compact reference preview generated by
  `format_project_context_reference()`; this is intended for future prompt
  injection but is only displayed/returned for inspection for now
- reference previews are generated only when the query returned matches;
  no-match queries should not create or inject a reference, and should only
  tell the user to refresh the map or broaden the terms
- file collection uses per-top-level-directory budgeting so one large directory
  cannot consume the whole file cap
- section records use raw over-collection plus final per-top-level-directory
  budgeting so `platform/`, `fw_bsp/`, `app_source/`, and `buildroot-dist/`
  can all be represented under the same record cap
- Kconfig extraction now also parses `*_defconfig`, `.config`, and `.fragment`
  assignments such as `BR2_PACKAGE_FOO=y` and `# CONFIG_FOO is not set`
- Buildroot-style package aggregation creates `packages` records from
  `Config.in`, `.mk`, defconfig enables, dependencies, and selected package
  variables so query output can show a package-level view before raw
  config/build rows
- query now uses fuzzy token scoring instead of only exact all-word matching;
  path/name/config/build/package fields are tokenized, package `enabled_in`
  defconfig paths and variables participate in scoring, and JSONL shard queries
  use a raw-line prefilter before parsing candidate records

Current `fw_omni_builder` validation after this slice:

- refresh writes to
  `<aha-home>/runtime/project_context/fw-omni-builder-git-ce424b2a4fa8/41b60b3332b2/`
- refresh with the current implementation takes about 9 seconds on this
  checkout; the manifest stays small and records are stored in JSONL shards
- repo graph contains 38 repos/submodules
- selected file records are spread across `app_source`, `buildroot-dist`,
  `fw_bsp`, `prebuild`, and `platform` instead of being dominated by one path
- generated profile detection reports `buildroot-external`, `embedded-c-app`,
  `python-service`, and `cloud`; detected flavors include `c`, `kconfig`,
  `dts`, `python`, `node`, and `cloud`
- current record counts are roughly 19.8k files, 486 packages, 20k symbols,
  8.9k build records, 20k configs, and 3.9k DTS records
- `resource_probe` resolves to
  `app_source/wyze_app/Stormcore_Abyss_V3/icamera/core/resource.c`
- `sigmastar_wyze_app` resolves to platform `Config.in`, defconfig enables,
  and `.mk` variables such as `SIGMASTAR_WYZE_APP_VERSION`
- `sigmastar_wyze_app` also resolves to a package record with its package
  directory, `.mk` path, config symbols, defconfig enable count, and cleaned
  dependencies
- Web search displays semantic sections before the broad file inventory for
  keyword queries. For `ss306 wyze app`, the user sees the
  `sigmastar_wyze_app` package first, then `ss306` defconfig assignments and
  `SIGMASTAR_WYZE_APP_*` build variables; the files section can still contain
  lower-value broad hits such as documentation or `.gitignore`, but it appears
  after the semantic sections.
- current shard-native fuzzy query time on this repo is roughly 0.5 to 1.6
  seconds for tested terms such as `resource probe`, `fw local`,
  `sigmastar wyze`, `ss306 wyze app`, `motor`, and `sigmastar_wyze_app`
- `sigmastar_wyze_app` reference preview fits in about 1 KiB and includes the
  package record, key files, defconfig enables, and the reminder to read exact
  files by path before editing

### Storage Path

Use AHA home runtime:

```text
aha_home_path(root) / "runtime" / "project_context" / project_key / workspace_id
```

Files:

```text
index.json
summary.md
build.log       # optional, only when diagnostics are useful
repos/<repo-id>/<section>.jsonl
```

`workspace_id` is `sha1(str(workspace.resolve())).hexdigest()[:12]`.
The index itself also records the absolute workspace path and git metadata so
stale or moved checkouts are easy to diagnose.

The map should have a unified browse entry similar to the navigation tab, but it
should mirror the actual map directory rather than a semantic manifest view.
The main Map tab list should show generated maps, not every file from the
currently selected workspace. Each generated map gets one card, similar to one
project navigation entry. Opening a map then shows its actual runtime directory
tree, and each file in that tree can be clicked to view its raw content.
The Map tab should follow the same page rhythm as the navigation tab:

```text
project/workspace selection + generate action
generated map list with status and action buttons
selected map View modal with search and tree link index
raw file modal
```

There should be no standalone status block showing `missing` for the currently
selected project. Missing selected workspaces are handled by the refresh action
and empty-list copy. The generated map list mirrors navigation entries: each map
card shows project key, workspace, workspace id, generated time, profiles,
counts, and `View`, `Stop`, and `Delete` actions. `Stop` is visible but disabled
by default; it is enabled only while the matching map refresh job is queued or
running. `Delete` removes the generated runtime map after a confirmation prompt
and is disabled while that map has an active refresh job. The project selection
area should only generate a map, matching the navigation tab's generate flow.
File search belongs inside each map `View` modal, not in the main Map tab
toolbar.
Knowledge entry and capture-note `View` actions should follow the same
modal-first interaction style as Nav and Map. Entry list rows should not expand
the full body inline for normal viewing; they open the entry modal. Capture
notes should also open a dedicated view modal. Entry and capture lists should
behave like title indexes: cards/rows show the title only, and status, timing,
association, and body details move into the modal. Per-item operations such as
edit, delete, status changes, distill, agent log, and close belong in one modal
action row before the body. Editing forms render inside the modal so viewing
and editing share the same focused workflow.
The map `View` modal should behave like a navigation entry point: it shows a
tree-style link index only. Clicking a file link opens the raw file in its own
modal instead of expanding content below the tree.
The tree index should show one directory layer at a time. Directory and file
items should look like ordinary blue web links; clicking a directory navigates
the modal to that directory's layer by replacing the current listing rather
than expanding children inline, while clicking a file opens the raw file modal.
The search box inside the `View` modal should use the same fuzzy project-map
query path as `/aha map query <terms>`. It must search generated map records
such as files, packages, symbols, configs, build hints, DTS records, tests, and
entry points, not just filter runtime map file names. An empty search restores
the one-layer directory browser. For normal keyword searches, Web results
should show semantic sections first, such as packages, configs, build records,
DTS records, and symbols before the broad file inventory. Path-like searches
that contain `/` or `.` can show files first.
`fw_omni_builder` baseline shows that fuzzy search must weight fields by
semantic strength: matches in names, paths, package directories, Kconfig
symbols, DTS nodes, and C signatures are strong; matches only in Buildroot
download URLs, generic variable values, or `enabled_in` payloads are weak. This
keeps terms such as `snapshot` or `debug` from ranking unrelated packages above
source, config, DTS, or package records that actually carry the user's terms.
Remaining accuracy work should focus on section-specific thresholds for broad
single-term package/build matches, because queries like `jpeg snapshot` can
still surface packages that merely depend on JPEG.

```text
Project maps
├── <project-key>/<workspace-id>  # View
└── <project-key>/<workspace-id>  # View

Selected map files
├── index.json                    # View raw
├── summary.md                    # View raw
└── repos/<repo-id>/*.jsonl       # View raw
```

This is implemented as two bounded APIs over the runtime directory:

- `GET /api/kb/project-context-index/maps`: returns one row per generated map
- `GET /api/kb/project-context-index/tree`: returns directory/file entries under
  the selected map directory
- `GET /api/kb/project-context-index/file?path=<relative-path>`: reads a file
  only if the resolved path stays inside that map directory

`tree` and `file` accept `project_key + workspace_id` so the UI can open maps
from the all-generated-maps list without changing the selected workspace.

The browser raw-file view is capped so huge JSONL shards do not freeze the UI.
This is still a faithful filesystem view; it does not flatten records back into
one large file.

### Generation Mode

Generate the index as an explicit, side-effect-contained service operation:

```python
build_project_context_index(root, workspace, *, project_key=None, refresh=False)
```

The generator should:

1. resolve `aha_home_path(root)` and `workspace.resolve()`
2. derive `project_key` using existing KB project-key logic
3. derive `workspace_id`
4. discover the repo graph: root git repo, submodules, nested repos, and
   non-git fallback
5. read current git heads and dirty flags per repo
6. collect files with `git ls-files` per repo when available
7. fall back to bounded filesystem walk only for non-git workspaces
8. apply ignore rules, file-size limits, and per-repo/per-topdir budgets
9. detect project flavors from marker files and paths
10. record optional tool availability without requiring those tools
11. write `index.json` manifest and record shards atomically through temp files
12. generate `summary.md` from the manifest and shard counts

Normal task prompt generation must not build the index automatically. The
current integration only loads an existing map for the task workspace when task
token saving is enabled; refresh remains an explicit map action.

## Implementation Phases

### P0. Documentation And Configuration

- Land this plan.
- Add config shape for project context index, but keep behavior off until tests
  exist.
- Decide cache path and invalidation policy.

### P1. Deterministic File/Build Index

- Implement project flavor detection.
- Implement file collection with ignore rules and size caps.
- Implement Kconfig, Makefile/Kbuild, defconfig, DTS/DTSI, and Buildroot
  package scanning without external dependencies.

### P2. Optional C Symbol Index

- Add `universal-ctags` integration.
- Add ctags-unavailable fallback parser.
- Record tool availability in the index.
- Keep tests independent of external ctags by mocking subprocess output.

### P3. Query And Prompt Guidance

- Implement term extraction and match ranking.
- Add bounded reference formatting.
- Inject the compact map capability block through the prompt pipeline only when
  task token saving is enabled and an existing map is available.
- Keep query result references manual or future policy-driven; do not apply
  references when a query has no useful result.
- Add sticky fingerprint handling so repeated turns do not receive duplicate
  map guidance.

### P4. Navigation Enrichment

- Use index summaries when building navigation candidates.
- Keep generated details out of permanent nav unless they are useful routes,
  key files, or durable module/build relationships.

### P5. UI And Diagnostics

- Surface index status in the knowledge console: generated time, git head,
  flavors, tools available, file/symbol/config counts.
- Add manual refresh and query diagnostics if useful.

### P6. Cloud And Additional Language Extractors

- Add Go, TypeScript/JavaScript, Rust, Docker/Kubernetes/Terraform/CI support
  after the embedded path proves useful.

## Tests

Add focused coverage:

- `tests/test_project_context_index.py`
- `tests/test_project_context_embedded.py`
- `tests/test_knowledge_retrieval_code_index.py`

Fixtures should include:

- mini C project with functions, macros, structs, and Makefile
- mini kernel-like tree with `drivers/foo/foo.c`, `Kconfig`, `Makefile`,
  `defconfig`, and DTS/DTSI
- mini Buildroot package with `.mk`, `Config.in`, board config, and defconfig
- ctags available/unavailable paths via mocks
- prompt injection budget and reference-only behavior
- sticky fingerprint behavior

## Open Decisions

- Exact config keys and defaults.
- Whether index generation should run automatically at task start or only when
  stale and cheap.
- Whether generated cache should live under runtime forever or move to a
  knowledge-root cache ignored by git.
- Final CLI command names.
- Minimum useful cloud-stack extractor set for the first non-embedded release.

## Discussion Maintenance Rule

This document is part of the design process. During this task, whenever the
discussion reaches a concrete decision or changes implementation direction,
update this file before moving on. Treat the document as the shared source of
truth for the implementation plan until code work starts.
