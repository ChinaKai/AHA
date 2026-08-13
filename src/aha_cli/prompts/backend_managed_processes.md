AHA managed background processes:
- If a server, watcher, tunnel, monitor, or other command must survive the current model turn, do not launch it with the backend tool's ordinary background-task mode. That process belongs to the Codex/Claude turn and may be reclaimed when the turn closes.
- Start it through the long-lived AHA Web runtime: `aha managed-process start <name> --cwd <workspace-relative-dir> -- <executable> <arg>...`. `AHA_RUN_ID`, `AHA_TASK_ID`, and `AHA_AGENT_ID` are inherited in an AHA backend session, so those scope flags are normally unnecessary.
- Inspect it with `aha managed-process status <name>` or `aha managed-process list`, and stop its whole process tree with `aha managed-process stop <name>` when it is no longer needed.
- Commands are executed directly without a shell. Invoke an explicit shell executable only when shell syntax is required. Do not place credentials in command arguments because runtime state records the argv for diagnostics.
- Managed processes survive model turns but remain owned by the AHA Web service; an AHA service restart intentionally stops them.
