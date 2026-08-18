You are generating the initial AHA project navigation for the current code workspace.

Project navigation is a first-read router for future agents. Its purpose is to reduce broad repository scans: an agent should read `navigation/index.md`, choose the smallest relevant module/flow docs, then inspect the listed key files before falling back to wider search.

Inspect the workspace in read-only mode. Use only facts you can verify from files in the workspace. Do not invent commands, modules, conventions, or caveats.

Return ONLY valid JSON. The top-level value must be an array of candidates. Each candidate must use this shape:
{"kind":"navigation","scope":"project","project_key":"...","slug":"index|modules/<slug>|flows/<slug>|knowledge/<category>/<slug>","title":"...","body":"markdown","tags":["navigation"],"related_files":[],"confidence":0.6}

Rules:
- Use the provided project_key exactly.
- Write candidate titles, body text, diagnostics, and navigation reasons in Chinese by default; keep code identifiers, paths, commands, and schema fields literal.
- Include exactly one `index` candidate.
- The `index` body is mandatory and MUST contain these sections in this order:
  - `## 项目介绍`
  - `## 如何编译 / 使用`
  - `## 注意事项`
  - `## 编码规范`
  - `## 项目结构 / 核心 Nav`
- `index` is a compact router, not a full manual. Keep it concise.
- Under `## 项目结构 / 核心 Nav`, list first-level modules/flows with direct links only to `modules/*.md` or `flows/*.md` candidates that are also in this JSON batch.
- When the workspace contains obvious long-lived project knowledge (architecture decisions, pitfalls, components, topic notes), promote it into `knowledge/<category>/<slug>` candidates and link them from the index's `### 项目知识` section. Categories are `decisions`, `pitfalls`, `components`, `topic`. Keep these entries concise and link only direct children from each `knowledge/<category>` doc.
- Navigation-internal links must use full navigation slugs: `modules/<name>.md`, `flows/<name>.md`, `knowledge/<category>/<name>.md`, or `index.md`. Do not use `../index.md` or a bare `<name>.md`; the Knowledge Web UI resolves links by persisted `slug`, not filesystem-relative paths.
- Do not overload `index` with every detailed child doc. Use parent module/flow/knowledge docs as grouping nodes; every non-index doc must be reachable from `index` through direct parent links, and parent docs should link only direct children.
- Each module/flow/knowledge doc stays lightweight: responsibility, key files, entry points, common task routing hints, caveats, and relevant tests only.
- Slugs must already be normalized: `index`, `modules/<name>`, `flows/<name>`, or `knowledge/<category>/<name>` (category must be `decisions`, `pitfalls`, `components`, or `topic`).
- Persisted `navigation_role` follows the slug: `index` → `index`, `modules/*` → `module`, `flows/*` → `flow`, and `knowledge/<category>/*` → `knowledge_<category>`.
- `slug` is required on every candidate and is persisted into Markdown frontmatter; never omit it or rely on the destination filename. The index candidate must use `"slug":"index"`.
- A custom `id` is optional; `slug` is the Web/API lookup and internal-link key.
- If there is not enough evidence for a module/flow doc, omit it instead of creating empty template noise.

workspace_path: $workspace_path
project_key: $project_key_value
