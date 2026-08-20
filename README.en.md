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
| [lark-channel-sdk](https://pypi.org/project/lark-channel-sdk/) | Optional | Enable the Feishu assistant |
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

# Feishu assistant
~/.venvs/aha/bin/python -m pip install 'lark-channel-sdk>=1.2,<2'
```

After installing the Feishu SDK, follow the
[Feishu assistant setup guide](docs/feishu-assistant.md) to configure an
enterprise custom app, `Allowed open IDs`, long-connection events, and task
status notifications.

The Feishu Open Platform bot menu is an event-type menu; subscribe to the
`application.bot.menu_v6` menu event. The owner's DM bot menu can define the
following items, which reuse the same form and confirmation cards as the Web UI:

| Menu item | Action |
| --- | --- |
| `aha_create_memo` | Create a memo (with title and body input) |
| `aha_create_task` | Create a task (with title and body input) |
| `aha_list_memos` | List memos |
| `aha_list_tasks` | List tasks |

The menu is owner-DM only; group chats still respond only to `@机器人` (bot
mentions) and have no menu management entry.

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

Run the following commands in PowerShell. The default `Full` mode creates an
isolated Python environment, verifies and installs the onebin, installs Git plus
serial and Feishu modules, and installs Codex and its Node.js runtime when no
Codex or Claude CLI is available. The larger Playwright/Chromium payload is not
downloaded by default. Agent login and credentials always remain user-managed:

```powershell
$Installer = Join-Path $env:TEMP "install_aha.ps1"
Invoke-WebRequest "https://github.com/ChinaKai/AHA/releases/latest/download/install_windows.ps1" -OutFile $Installer
powershell.exe -ExecutionPolicy Bypass -File $Installer
```

Installation modes:

| Mode | Behavior |
| --- | --- |
| `Full` (default) | Installs Python, Git, pyserial, Feishu SDK, and at least one Agent CLI; adds Node.js when Codex needs it, without downloading Chromium |
| `Minimal` | Creates the runtime and installs only the AHA onebin |
| `Offline` | Disables downloads and reads the onebin, wheels, browser, and optional Python installer from a local directory |

Common examples:

```powershell
& $Installer -Mode Minimal
& $Installer -Mode Full -AgentBackend Claude
& $Installer -Mode Full -AgentBackend Both
& $Installer -Mode Full -WithBrowser
& $Installer -Mode Minimal -Modules Browser,Hardware
& $Installer -Mode Full -Repair
```

The installer writes `%LOCALAPPDATA%\AHA\install-report.json` with the core
version, SHA-256 result, Python path, module status, and remaining login or
configuration actions. Optional module failures are reported without rolling
back the installed core; pass `-StrictModules` to fail when any requested
module is unavailable. Install the shared browser explicitly with `-WithBrowser`
or `-Modules Browser`; the first Chromium install commonly downloads several
hundred MB. `-SkipBrowserDownload` installs only the Playwright Python module.

Offline bundle layout:

```text
D:\AHA-offline\
  aha
  SHA256SUMS
  python-installer.exe      # only needed when Python is absent
  wheels\                  # Playwright, pyserial, lark-channel-sdk and dependencies
  ms-playwright\           # only for -WithBrowser / -Modules Browser
```

```powershell
& $Installer -Mode Offline -OfflineDir D:\AHA-offline -AgentBackend None
```

Offline mode never downloads an Agent CLI. Preinstall Codex/Claude, or use
`-AgentBackend None` to skip the Agent CLI requirement. No mode writes model
credentials or performs third-party login.

The tray uses the AHA logo. Double-click it to open AHA. Its context menu can
open the dashboard, restart the service, toggle "Start at login", or exit. The
"Settings..." dialog lets you change `AHA_HOME`, the bind address, port, and
Web token. Saving restarts the Web service and updates an enabled startup
command. "Exit AHA" terminates the complete supervised Web process tree and
releases its listening port, including after a Web-triggered restart. To enable
startup during installation:

```powershell
powershell.exe -ExecutionPolicy Bypass -File $Installer -EnableStartup
```

The default installation path is `%LOCALAPPDATA%\AHA\aha`, the data directory
is `%USERPROFILE%\.aha`, and the Web UI is <http://127.0.0.1:8788>. The installer
creates an `AHA` shortcut with the AHA logo in the current user's Start Menu;
after exiting the tray, search for `AHA` from Start to launch it again or pin it
to the taskbar. Pass `-NoShortcut` during installation to omit it. Tray settings
are stored in `%LOCALAPPDATA%\AHA\tray.json`; the plaintext Web token is stored
only in the selected `AHA_HOME\web-token`. Startup uses the current user's
`HKCU\Software\Microsoft\Windows\CurrentVersion\Run` key and does not require
administrator privileges. In tray mode, Git checks, backend discovery, and
other helper processes started by Web requests run without flashing console
windows.

Full mode detects existing Codex/Claude installations. When neither is found,
`-AgentBackend Auto` installs Codex. Run the selected CLI once to complete
login:

```powershell
codex
claude
```

Claude Code works directly in PowerShell. Full mode also installs Git for
Knowledge synchronization and Bash-oriented workflows.

When an agent needs a preview server, watcher, or tunnel to survive across chat
turns, use an AHA Web-managed process instead of the Codex/Claude tool's own
background-task mode:

```powershell
aha managed-process start preview --cwd . -- python -m http.server 8790
aha managed-process status preview
aha managed-process stop preview
```

Inside an AHA backend session, run/task/agent scope is inherited from the
environment. A managed process survives model turns but is stopped cleanly when
its task becomes terminal or the AHA Web service restarts/exits.

Advanced users can still maintain modules directly in the AHA venv; this is
normally unnecessary:

```powershell
$AhaPython = "$env:USERPROFILE\.venvs\aha\Scripts\python.exe"

# Shared browser
& $AhaPython -m pip install "playwright>=1.45,<2"
& $AhaPython -m playwright install chromium

# Hardware Debug
& $AhaPython -m pip install pyserial

# Feishu assistant
& $AhaPython -m pip install "lark-channel-sdk>=1.2,<2"
```

After installing the Feishu SDK, follow the
[Feishu assistant setup guide](docs/feishu-assistant.md) to configure an
enterprise custom app, `Allowed open IDs`, long-connection events, and task
status notifications.

The Feishu Open Platform bot menu is an event-type menu; subscribe to the
`application.bot.menu_v6` menu event. The owner's DM bot menu can define the
following items, which reuse the same form and confirmation cards as the Web UI:

| Menu item | Action |
| --- | --- |
| `aha_create_memo` | Create a memo (with title and body input) |
| `aha_create_task` | Create a task (with title and body input) |
| `aha_list_memos` | List memos |
| `aha_list_tasks` | List tasks |

The menu is owner-DM only; group chats still respond only to `@机器人` (bot
mentions) and have no menu management entry.

You can also skip the installer, download the onebin manually, and start the
tray. The onebin is a Python zipapp, not a native Windows `.exe`:

```powershell
$AhaDir = "$env:LOCALAPPDATA\AHA"
py -3.12 -m venv "$env:USERPROFILE\.venvs\aha"
$AhaPython = "$env:USERPROFILE\.venvs\aha\Scripts\python.exe"
$AhaPythonw = "$env:USERPROFILE\.venvs\aha\Scripts\pythonw.exe"
New-Item -ItemType Directory -Force $AhaDir | Out-Null
Invoke-WebRequest "https://github.com/ChinaKai/AHA/releases/latest/download/aha" -OutFile "$AhaDir\aha"
& $AhaPython "$AhaDir\aha" --version
& $AhaPythonw "$AhaDir\aha" --home "$env:USERPROFILE\.aha" tray --host 127.0.0.1 --port 8788 --open-browser
```

With Node.js installed, Daily Usage can use `npx` directly. Local Terminal
automatically lists available Windows PowerShell, CMD, PowerShell 7, and WSL
environments.

## Development From Source

```bash
git clone https://github.com/ChinaKai/AHA.git
cd AHA
python3 -m pip install -e .
# For Feishu, replace the previous command with: python3 -m pip install -e ".[feishu]"
PYTHONPATH=src python3 -m aha_cli ui --host 127.0.0.1 --port 8788
```

Run tests with `python3 -m pytest`. Build the onebin with
`python3 scripts/build_onebin.py --output dist/aha`. See [`docs/`](docs/) for
additional details.

> AHA listens on `127.0.0.1` by default. For access from another device, enable
> a Web auth token and prefer SSH, a VPN, or a protected reverse proxy. Never
> expose an unauthenticated port directly to the public Internet.
