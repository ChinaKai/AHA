Knowledge/nav feedback context:
- knowledge_enabled: true
- project_nav_enabled: true
- project_nav_index_exists: true
- project_key: $project_key_value
- workspace_path: $workspace_path

You may append one hidden knowledge sidecar after the visible Final to feed back project nav updates from this task. The sidecar will be stripped before the user-visible Final is saved; it is not an AHA JSON action.

This is a byproduct of finalizing the current task, not a new task:
- Do not inspect files, run commands, or broaden analysis only to maintain nav.
- Use only facts already learned while solving this task: touched/read files, final changes, validation commands, discovered module ownership, stale nav facts, or changed workflows.
- Emit nav feedback only when there is a concrete, evidence-backed delta. If there is no clear nav delta, omit the sidecar.
- Keep each nav candidate a minimal patch candidate for review; do not rewrite the whole project nav.
- Prefer `kind:"navigation"` candidates with `slug` such as `index`, `modules/<module>`, or `flows/<flow>`, plus `title`, `responsibility` or `summary`, `related_files`, optional `entry_points`, optional `diagnostic_paths`, and a short `navigation_reason`.

Hidden sidecar format, only when useful:
`<aha_knowledge_candidates>[...]</aha_knowledge_candidates>`
