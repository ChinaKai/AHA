You are generating the initial AHA project navigation for the current code workspace.

Project navigation is a first-read router for future agents. Its purpose is to reduce broad repository scans: an agent should read `navigation/index.md`, choose the smallest relevant module/flow docs, then inspect the listed key files before falling back to wider search.

Inspect the workspace in read-only mode. Use only facts you can verify from files in the workspace. Do not invent commands, modules, conventions, or caveats.

Return ONLY valid JSON. The top-level value must be an array of candidates. Each candidate must use this shape:
{"kind":"navigation","scope":"project","project_key":"...","slug":"index|modules/<slug>|flows/<slug>","title":"...","body":"markdown","tags":["navigation"],"related_files":[],"confidence":0.6}

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
- Each module/flow doc stays lightweight: responsibility, key files, entry points, common task routing hints, caveats, and relevant tests only.
- Slugs must already be normalized: `index`, `modules/<name>`, or `flows/<name>`.
- If there is not enough evidence for a module/flow doc, omit it instead of creating empty template noise.

workspace_path: $workspace_path
project_key: $project_key_value
