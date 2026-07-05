Project context map:
- token_saving: enabled
- project_key: $project_key_value
- workspace_id: $workspace_id
- status: $map_status
- map_index: $map_index
- generated_at: $generated_at
- counts: $map_counts
- flavors: $map_flavors
- profiles: $map_profiles

Use this map to locate relevant files, symbols, configs, DTS, package, or build records before broad repository search. Use focused terms from the user's request with `/aha map query <terms>` when available, or inspect the map index and shards directly. Treat map results as hints only; read exact source files before analysis or edits. If the map has no relevant result, continue normally without adding reference text.

Do not hand-edit generated Project Map cache files. If map output is stale, refresh it; if map generation or query is wrong, repair the extractor, schema, resolver, query expansion, ranking, or refresh logic when that is in scope.
