# AHA

[简体中文](README.md) | [English](README.en.md)

AHA (Agent Help Agent) is a local-first AI agent workbench. It organizes work as
**Run → Task → Agent**, lets Codex, Claude, and other agents work independently
or collaborate, and provides one Web UI for conversations, context, a shared
browser, local terminals, and hardware debugging.

AHA does not provide models or model accounts. It uses agent CLIs installed and
authenticated on your machine. Data is stored in `~/.aha` by default; use
`--home <path>` to choose another location.

## Requirements

| Environment / program | Requirement | Purpose |
| --- | --- | --- |
| [Python](https://www.python.org/downloads/) 3.10+ | Required | Run the AHA onebin |
| [Codex CLI](https://help.openai.com/en/articles/11096431) or [Claude Code](https://code.claude.com/docs/en/installation) | At least one | Run AI tasks; sign in after installation |
| [Node.js](https://nodejs.org/en/download) + npm/npx | Optional | Install Codex; enable Daily Usage |
| [Playwright Python](https://playwright.dev/python/docs/intro) + Chromium | Optional | Enable the shared browser |
| [pyserial](https://pyserial.readthedocs.io/) | Optional | Enable serial / Windows COM debugging |

PowerShell 7, WSL, Chrome, and Edge are optional. AHA detects them automatically
when installed.

## Linux Installation and Startup

The following commands target Ubuntu / Debian.

Install Python and create an isolated runtime environment:

```bash
sudo apt update
sudo apt install -y python3 python3-venv python3-pip curl
python3 --version  # 3.10 or newer
python3 -m venv ~/.venvs/aha
~/.venvs/aha/bin/python -m pip install --upgrade pip
```

Install at least one of Codex and Claude. Codex requires Node.js LTS; follow the
[official Node.js instructions](https://nodejs.org/en/download), then run:

```bash
npm install --global @openai/codex
codex
```

Claude Code:

```bash
curl -fsSL https://claude.ai/install.sh | bash
claude
```

Optional features:

```bash
# Shared browser
~/.venvs/aha/bin/python -m pip install 'playwright>=1.45,<2'
~/.venvs/aha/bin/python -m playwright install --with-deps chromium

# Hardware Debug
~/.venvs/aha/bin/python -m pip install pyserial
```

Download the single cross-platform Release onebin and start AHA:

```bash
mkdir -p ~/.local/bin
curl -fL https://github.com/ChinaKai/AHA/releases/latest/download/aha \
  -o ~/.local/bin/aha
chmod +x ~/.local/bin/aha

~/.venvs/aha/bin/python ~/.local/bin/aha \
  --home ~/.aha ui --host 127.0.0.1 --port 8788
```

Open <http://127.0.0.1:8788>. Complete initialization when prompted and press
`Ctrl+C` to stop. To run AHA in the background on Linux, use
[`scripts/install_user_service.sh`](scripts/install_user_service.sh) to install
a user systemd service.

## Windows Installation and Startup

Run the following commands in PowerShell.

Install Python and create an isolated runtime environment:

```powershell
winget install --id Python.Python.3.12 -e
# Reopen PowerShell
py -3.12 -m venv "$env:USERPROFILE\.venvs\aha"
$AhaPython = "$env:USERPROFILE\.venvs\aha\Scripts\python.exe"
& $AhaPython -m pip install --upgrade pip
```

Install at least one of Codex and Claude:

```powershell
# Codex: reopen PowerShell after installing Node.js
winget install --id OpenJS.NodeJS.LTS -e
npm install --global @openai/codex
codex

# Claude Code
winget install --id Anthropic.ClaudeCode -e
claude
```

Claude Code works directly in PowerShell on Windows. For Bash support, also run
`winget install --id Git.Git -e`.

Optional features:

```powershell
$AhaPython = "$env:USERPROFILE\.venvs\aha\Scripts\python.exe"

# Shared browser
& $AhaPython -m pip install "playwright>=1.45,<2"
& $AhaPython -m playwright install chromium

# Hardware Debug
& $AhaPython -m pip install pyserial
```

Download and start AHA. The onebin is a Python zipapp, not a native Windows
`.exe`:

```powershell
$AhaDir = "$env:LOCALAPPDATA\AHA"
$AhaPython = "$env:USERPROFILE\.venvs\aha\Scripts\python.exe"
New-Item -ItemType Directory -Force $AhaDir | Out-Null
Invoke-WebRequest "https://github.com/ChinaKai/AHA/releases/latest/download/aha" -OutFile "$AhaDir\aha"
& $AhaPython "$AhaDir\aha" --home "$env:USERPROFILE\.aha" ui --host 127.0.0.1 --port 8788
```

Open <http://127.0.0.1:8788>. Complete initialization when prompted and press
`Ctrl+C` to stop. With Node.js installed, Daily Usage can use `npx` directly.
Local Terminal automatically lists available Windows PowerShell, CMD,
PowerShell 7, and WSL environments.

## Development From Source

```bash
git clone https://github.com/ChinaKai/AHA.git
cd AHA
python3 -m pip install -e .
PYTHONPATH=src python3 -m aha_cli ui --host 127.0.0.1 --port 8788
```

Run tests with `python3 -m pytest`. Build the onebin with
`python3 scripts/build_onebin.py --output dist/aha`. See [`docs/`](docs/) for
additional details.

> AHA listens on `127.0.0.1` by default. For access from another device, enable
> a Web auth token and prefer SSH, a VPN, or a protected reverse proxy. Never
> expose an unauthenticated port directly to the public Internet.
