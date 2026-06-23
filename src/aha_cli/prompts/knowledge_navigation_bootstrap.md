You are generating the initial AHA project navigation for a code workspace.
Project navigation is an agent `/init` style project briefing plus a compact map: one small `index`, then lightweight modules/* or flows/* docs that tell agents where to look. The index is the project manual future agents read on demand; it must be useful, concise, and grounded in the supplied workspace evidence.

Return ONLY valid JSON. The top-level value must be an array of candidates. Each candidate must use this shape:
{"kind":"navigation","scope":"project","project_key":"...","slug":"index|modules/<slug>|flows/<slug>","title":"...","body":"markdown","tags":["navigation"],"related_files":[],"confidence":0.6}

Rules:
- Include exactly one `index` candidate unless the scan is unusable.
- The `index` body MUST contain `## Project README` followed by the generated project briefing.
- The `index` body MUST contain `## Project Map`; put first-level module/flow links under it.
- Project README should cover purpose, tech stack, run/test commands, code organization, conventions, and agent caveats when supported by evidence.
- `index` links only directly to first-level `modules/*.md` or `flows/*.md` candidates that are also in this JSON batch.
- Module/flow docs stay lightweight: responsibility, key files, entry points, and caveats only.
- Slugs must already be normalized: `index`, `modules/<name>`, or `flows/<name>`.
- Use the provided project_key exactly.
- If there is not enough evidence for a module/flow doc, omit it instead of creating empty template noise.

workspace_path: $workspace_path
project_key: $project_key_value

--- COMPRESSED WORKSPACE SCAN JSON ---
$scan_json
--- END SCAN ---
