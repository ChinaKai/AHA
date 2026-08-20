Repository coding principles:
- Match the repository's existing code style, naming, module boundaries, abstractions, and test patterns.
- Respect the current architecture and public contracts. Cross-layer or architectural changes require concrete root-cause evidence and compatibility consideration.
- Make the smallest sufficient change: touch only what the task requires while still fixing the root cause.
- Preserve backward compatibility unless the user explicitly requests a breaking change.
- Reuse existing utilities and patterns before introducing new abstractions, dependencies, configuration, or duplicate implementations.
- Do not perform unrelated refactors, cleanup, formatting, or overwrite existing workspace changes.
- Verify changes proportionally to risk with focused regression coverage.
