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
| Python 3.10+ | Required | Run the AHA onebin |
| Codex CLI, Claude Code, or OpenCode | At least one | Execute AI tasks |
| Node.js + npm | Required for Codex | Install the Codex CLI |
| Git | Recommended | Repository operations and Knowledge sync |
| Playwright Python | Installed by Windows Full | Shared-browser control |
| Chrome, Edge, or Playwright Chromium | At least one for browser tasks | Browser runtime |
| pyserial | Optional | Serial / Windows COM debugging |
| lark-channel-sdk | Optional | Feishu assistant |

## Windows Installation

Run in PowerShell:

```powershell
$Installer = Join-Path $env:TEMP "install_aha.ps1"
Invoke-WebRequest `
  "https://github.com/ChinaKai/AHA/releases/latest/download/install_windows.ps1" `
  -OutFile $Installer
powershell.exe -ExecutionPolicy Bypass -File $Installer
```

Default `Full` mode installs AHA, Python, Git, Playwright, pyserial, the Feishu
SDK, and ensures that at least one Agent CLI exists. It prefers system
Chrome/Edge and does not download Playwright Chromium by default.

```powershell
# Core-only installation
& $Installer -Mode Minimal

# Explicitly download Playwright Chromium
& $Installer -Mode Full -WithBrowser

# Install selected modules; Browser requests Chromium
& $Installer -Mode Minimal -Modules Browser,Hardware

# Install Playwright but never download a browser
& $Installer -Mode Full -SkipBrowserDownload

# Offline installation
& $Installer -Mode Offline -OfflineDir D:\AHA-offline
```

Default locations:

```text
Program: %LOCALAPPDATA%\AHA\aha
Data:    %USERPROFILE%\.aha
Web UI:  http://127.0.0.1:8788
```

## Linux Installation

Ubuntu / Debian example:

```bash
sudo apt update
sudo apt install -y python3 python3-venv python3-pip curl git

python3 -m venv ~/.venvs/aha
~/.venvs/aha/bin/python -m pip install --upgrade pip

# Install at least one Agent CLI
npm install --global @openai/codex

# Optional shared browser; no Chromium download is needed with system Chrome/Edge
~/.venvs/aha/bin/python -m pip install 'playwright>=1.45,<2'

mkdir -p ~/.local/bin
curl -fL https://github.com/ChinaKai/AHA/releases/latest/download/aha \
  -o ~/.local/bin/aha
chmod +x ~/.local/bin/aha

~/.venvs/aha/bin/python ~/.local/bin/aha \
  --home ~/.aha ui --host 127.0.0.1 --port 8788
```

Open <http://127.0.0.1:8788>.
