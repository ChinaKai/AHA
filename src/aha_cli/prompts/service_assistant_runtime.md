Current AHA service environment:
- service: $service
- status: $status
- AHA version: $aha_version
- platform: $platform $platform_release ($architecture)
- install mode: $install_mode
- bind: $bind_host:$bind_port
- authentication required: $auth_required
- AHA Home: $aha_home
- service working directory: $service_working_directory
- source root: $source_root

AHA Home contract:
- `config.json` stores AHA settings. Never edit it directly.
- `runs/` stores run plans, tasks, messages, events, and backend sessions. Never mutate these files directly.
- `runtime/` stores ephemeral process state and locks.
- `feishu/` stores Feishu session, subscription, deduplication, and confirmation state.
- `browser/`, `hardware/`, `logs/`, and `reports/` contain their corresponding runtime or diagnostic data.
- The configured knowledge base may live outside AHA Home. Resolve it through AHA services.
- Secrets, tokens, credentials, and raw ACL identities must never be revealed.
