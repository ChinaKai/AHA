# AHA

[简体中文](README.md) | [English](README.en.md)

AHA (Agent Help Agent) is a local-first AI agent workbench. It organizes work
as **Run → Task → Agent**, runs Codex, Claude, OpenCode, and other backends, and
provides one Web UI for conversations, context, a shared browser, local
terminals, and hardware debugging.

AHA does not provide models or accounts. Install at least one Agent CLI and
either sign in through that CLI or configure an API Provider in AHA Settings.
Data is stored in `~/.aha` by default.

## Requirements

| Program | Requirement | Purpose |
| --- | --- | --- |
| Codex CLI, Claude Code, or OpenCode | At least one | Execute AI tasks |
| Node.js + npm | Required for Codex | Install Codex CLI |
| Git | Recommended | Repository operations and Knowledge sync |
| Chrome, Edge, or Playwright Chromium | At least one for browser tasks | Shared browser |
| pyserial | Optional | Serial debugging |
| lark-channel-sdk | Optional | Feishu assistant |

## Windows Installation

Download and double-click `AHA-Setup-x64.exe` from the latest Release. Its
bilingual GUI defaults to Full mode and configures a dedicated Python
environment, AHA, Playwright,
pyserial, the Feishu SDK, Git, and at least one Agent CLI. It does not sign in
to third-party services or download Chromium by default.
Each Windows user has one registered program installation. The wizard detects
install, upgrade, repair, or confirmed downgrade actions; uninstall removes
only that registered program while retaining AHA data.

Command-line examples:

```powershell
.\AHA-Setup-x64.exe

# Core only
.\AHA-Setup-x64.exe --mode Minimal

# Install Playwright Chromium
.\AHA-Setup-x64.exe --with-browser

# Enable the background startup service
.\AHA-Setup-x64.exe --enable-startup
```

Default locations:

```text
Program: %LOCALAPPDATA%\AHA
Data:    %USERPROFILE%\.aha
Web UI:  http://127.0.0.1:8788
```

`install_windows.ps1` remains available for offline, repair, and automation
workflows.

## Linux Installation

Ubuntu / Debian:

```bash
arch=$(dpkg --print-architecture)
curl -fL "https://github.com/ChinaKai/AHA/releases/latest/download/aha_${arch}.deb" \
  -o "aha_${arch}.deb"
sudo apt install "./aha_${arch}.deb"

systemctl --user enable --now aha.service
```

Open <http://127.0.0.1:8788>. The first start creates `~/.aha/web-token`.

The Linux package requires Python 3.10+. The portable `aha` onebin remains
available in Releases, and `install_user_service.sh` supports custom paths and
service settings.
