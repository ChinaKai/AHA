# AHA

[简体中文](README.md) | [English](README.en.md)

AHA（Agent Help Agent）是一个本地优先的 AI agent 工作台。它用
**Run → Task → Agent** 组织工作，让 Codex、Claude 等 agent 在相互隔离的任务中
执行和协作，并通过 Web UI 统一管理对话、上下文、共享浏览器、本机终端与硬件调试。

AHA 不提供模型或模型账号，而是调用本机已安装并登录的 agent CLI。数据默认保存
在 `~/.aha`，可用 `--home <path>` 修改。

## 依赖

| 环境 / 程序 | 要求 | 用途 |
| --- | --- | --- |
| [Python](https://www.python.org/downloads/) 3.10+ | 必需 | 运行 AHA onebin |
| [Codex CLI](https://help.openai.com/en/articles/11096431) 或 [Claude Code](https://code.claude.com/docs/en/installation) | 至少一个 | 执行 AI task，安装后需登录 |
| [Node.js](https://nodejs.org/en/download) + npm/npx | 按需 | 安装 Codex；启用 Daily Usage |
| [Playwright Python](https://playwright.dev/python/docs/intro) + Chromium | 按需 | 启用共享浏览器 |
| [lark-channel-sdk](https://pypi.org/project/lark-channel-sdk/) | 按需 | 启用飞书助手 |
| [pyserial](https://pyserial.readthedocs.io/) | 按需 | 启用串口 / Windows COM 调试 |

PowerShell 7、WSL、Chrome 和 Edge 均为可选项；安装后 AHA 会自动检测。

## Linux 安装与启动

以下以 Ubuntu / Debian 为例。

安装 Python 并创建独立运行环境：

```bash
sudo apt update
sudo apt install -y python3 python3-venv python3-pip curl
python3 --version  # 需要 3.10+
python3 -m venv ~/.venvs/aha
~/.venvs/aha/bin/python -m pip install --upgrade pip
```

Codex 和 Claude 至少安装一个。Codex 需要 Node.js LTS；可按
[Node.js 官方页面](https://nodejs.org/en/download)安装后执行：

```bash
npm install --global @openai/codex
codex
```

Claude Code：

```bash
curl -fsSL https://claude.ai/install.sh | bash
claude
```

可选能力：

```bash
# 共享浏览器
~/.venvs/aha/bin/python -m pip install 'playwright>=1.45,<2'
~/.venvs/aha/bin/python -m playwright install --with-deps chromium

# Hardware Debug
~/.venvs/aha/bin/python -m pip install pyserial

# Send a file through an interactive Serial or Telnet Network shell
aha hardware-file-send <run-id> <task-id> ./local.bin /tmp/remote.bin --channel serial
aha hardware-file-send <run-id> <task-id> ./local.bin /tmp/remote.bin --channel network

# 飞书助手
~/.venvs/aha/bin/python -m pip install 'lark-channel-sdk>=1.2,<2'
```

启用飞书助手后，按[飞书助手接入说明](docs/feishu-assistant.md)配置企业自建应用、
`Allowed open IDs`、长连接事件和 task 状态推送。

飞书开放平台的机器人菜单为事件型菜单，事件订阅选择
`application.bot.menu_v6`。owner 私聊机器人菜单可配置以下菜单项，
创建/查询会复用与 Web 一致的表单卡与确认卡：

| 菜单项 | 动作 |
| --- | --- |
| `aha_create_memo` | 创建 Memo（附标题与正文输入） |
| `aha_create_task` | 创建 Task（附标题与正文输入） |
| `aha_list_memos` | 查询 Memo |
| `aha_list_tasks` | 查询 Task |

菜单只对 owner 私聊开放；群聊仍只响应 `@机器人`，不开放菜单管理入口。

下载同一份跨平台 Release onebin 并启动：

```bash
mkdir -p ~/.local/bin
curl -fL https://github.com/ChinaKai/AHA/releases/latest/download/aha \
  -o ~/.local/bin/aha
chmod +x ~/.local/bin/aha

~/.venvs/aha/bin/python ~/.local/bin/aha \
  --home ~/.aha ui --host 127.0.0.1 --port 8788
```

打开 <http://127.0.0.1:8788>。首次进入时按页面提示初始化，按 `Ctrl+C` 停止。
Linux 后台运行可使用 [`scripts/install_user_service.sh`](scripts/install_user_service.sh)
安装 user systemd 服务。

## Windows 安装与启动

以下命令在 PowerShell 中运行。先安装 Python（已安装 3.10+ 可跳过）：

```powershell
winget install --id Python.Python.3.12 -e
# 安装完成后重新打开 PowerShell
```

下载官方安装脚本。脚本会创建独立 Python 环境、安装 onebin，并立即以无控制台窗口的方式启动 AHA 托盘：

```powershell
$Installer = Join-Path $env:TEMP "install_aha.ps1"
Invoke-WebRequest "https://github.com/ChinaKai/AHA/releases/latest/download/install_windows.ps1" -OutFile $Installer
powershell.exe -ExecutionPolicy Bypass -File $Installer
```

托盘使用 AHA Logo。双击图标可打开 AHA；右键菜单可打开面板、重启服务、切换“开机自启动”，也可在“设置…”中修改 `AHA_HOME`、Bind 地址、Port 和 Web Token。保存设置后 Web 服务会自动重启，已启用的开机启动命令也会同步更新；选择“退出 AHA”会结束完整 Web 进程树并释放监听端口，包括从 Web 页面重启过的进程。若希望安装时直接启用开机自启动：

```powershell
powershell.exe -ExecutionPolicy Bypass -File $Installer -EnableStartup
```

默认安装位置是 `%LOCALAPPDATA%\AHA\aha`，数据目录是 `%USERPROFILE%\.aha`，Web UI 是 <http://127.0.0.1:8788>。安装脚本会在当前用户的开始菜单创建带 AHA Logo 的 `AHA` 快捷方式；退出托盘后可按 Win 键搜索 `AHA` 重新启动，也可将其固定到任务栏。若不需要快捷方式，安装时传入 `-NoShortcut`。托盘设置保存在 `%LOCALAPPDATA%\AHA\tray.json`；Web Token 明文只写入所选 `AHA_HOME\web-token`。开机启动项写入当前用户的 `HKCU\Software\Microsoft\Windows\CurrentVersion\Run`，不需要管理员权限。托盘模式下，Web 请求触发的 Git、后端探测和其他辅助进程会以无控制台窗口方式运行。

Codex 和 Claude 至少安装一个：

```powershell
# Codex：安装 Node.js 后重新打开 PowerShell
winget install --id OpenJS.NodeJS.LTS -e
npm install --global @openai/codex
codex

# Claude Code
winget install --id Anthropic.ClaudeCode -e
claude
```

Claude Code 在 Windows 可直接使用 PowerShell；如需 Bash，可额外执行
`winget install --id Git.Git -e`。

Agent 需要启动跨对话回合存活的预览服务、watcher 或 tunnel 时，应使用 AHA Web 托管进程，而不是 Codex/Claude 工具自身的后台任务：

```powershell
aha managed-process start preview --cwd . -- python -m http.server 8790
aha managed-process status preview
aha managed-process stop preview
```

在 AHA backend session 内，run/task/agent 范围由环境变量自动继承。托管进程随模型回合保持运行，但所属任务进入终态或 AHA Web 服务重启/退出时会受控停止。

可选能力：

```powershell
$AhaPython = "$env:USERPROFILE\.venvs\aha\Scripts\python.exe"

# 共享浏览器
& $AhaPython -m pip install "playwright>=1.45,<2"
& $AhaPython -m playwright install chromium

# Hardware Debug
& $AhaPython -m pip install pyserial

# Send a file through an interactive Serial or Telnet Network shell
aha hardware-file-send <run-id> <task-id> .\local.bin /tmp/remote.bin --channel serial
aha hardware-file-send <run-id> <task-id> .\local.bin /tmp/remote.bin --channel network

# 飞书助手
& $AhaPython -m pip install "lark-channel-sdk>=1.2,<2"
```

启用飞书助手后，按[飞书助手接入说明](docs/feishu-assistant.md)配置企业自建应用、
`Allowed open IDs`、长连接事件和 task 状态推送。

飞书开放平台的机器人菜单为事件型菜单，事件订阅选择
`application.bot.menu_v6`。owner 私聊机器人菜单可配置以下菜单项，
创建/查询会复用与 Web 一致的表单卡与确认卡：

| 菜单项 | 动作 |
| --- | --- |
| `aha_create_memo` | 创建 Memo（附标题与正文输入） |
| `aha_create_task` | 创建 Task（附标题与正文输入） |
| `aha_list_memos` | 查询 Memo |
| `aha_list_tasks` | 查询 Task |

菜单只对 owner 私聊开放；群聊仍只响应 `@机器人`，不开放菜单管理入口。

也可以跳过安装脚本，手动下载 onebin 并运行托盘。onebin 是 Python zipapp，不是 Windows 原生 `.exe`：

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

Node.js 安装后 Daily Usage 可直接使用 `npx`；Local Terminal 会自动列出系统中
可用的 Windows PowerShell、CMD、PowerShell 7 和 WSL。

## 源码开发

```bash
git clone https://github.com/ChinaKai/AHA.git
cd AHA
python3 -m pip install -e .
# 启用飞书助手时，将上一条改为：python3 -m pip install -e ".[feishu]"
PYTHONPATH=src python3 -m aha_cli ui --host 127.0.0.1 --port 8788
```

运行测试：`python3 -m pytest`；构建 onebin：
`python3 scripts/build_onebin.py --output dist/aha`。更多说明见 [`docs/`](docs/)。

> AHA 默认只监听 `127.0.0.1`。跨设备访问时请启用 Web auth token，并优先使用
> SSH、VPN 或受保护的反向代理，不要把无认证端口直接暴露到公网。
