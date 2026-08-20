AHA cross-platform runtime context:
- The AHA Web/service control plane and authoritative installed onebin run on Windows; this `$backend` backend executes inside WSL distro `$distro`.
- The authoritative AHA home as seen from WSL is `$aha_home`. The authoritative Windows-installed onebin as seen from WSL is `$aha_bin`.
- Use Linux paths for the backend shell and workspace. Keep valid `/home/...` paths unchanged when AHA or its backend reads them; convert paths only when invoking a Windows process that requires a Windows or UNC path.
- Do not treat a separate WSL AHA installation or `python -m aha_cli` as the authoritative installed AHA for service, upgrade, hardware bridge, or other control-plane operations.
- AHA local upgrades target the Windows-installed onebin. When the `aha-local-upgrade` skill is enabled and relevant, read and follow it; do not restart or stop the Windows Web/tray service unless the user explicitly requests that action.
