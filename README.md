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
```

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

以下命令在 PowerShell 中运行。

安装 Python 并创建独立运行环境：

```powershell
winget install --id Python.Python.3.12 -e
# 重新打开 PowerShell
py -3.12 -m venv "$env:USERPROFILE\.venvs\aha"
$AhaPython = "$env:USERPROFILE\.venvs\aha\Scripts\python.exe"
& $AhaPython -m pip install --upgrade pip
```

Codex 和 Claude 至少安装一个。

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

可选能力：

```powershell
$AhaPython = "$env:USERPROFILE\.venvs\aha\Scripts\python.exe"

# 共享浏览器
& $AhaPython -m pip install "playwright>=1.45,<2"
& $AhaPython -m playwright install chromium

# Hardware Debug
& $AhaPython -m pip install pyserial
```

下载并启动 AHA。onebin 是 Python zipapp，不是 Windows 原生 `.exe`：

```powershell
$AhaDir = "$env:LOCALAPPDATA\AHA"
$AhaPython = "$env:USERPROFILE\.venvs\aha\Scripts\python.exe"
New-Item -ItemType Directory -Force $AhaDir | Out-Null
Invoke-WebRequest "https://github.com/ChinaKai/AHA/releases/latest/download/aha" -OutFile "$AhaDir\aha"
& $AhaPython "$AhaDir\aha" --home "$env:USERPROFILE\.aha" ui --host 127.0.0.1 --port 8788
```

打开 <http://127.0.0.1:8788>。首次进入时按页面提示初始化，按 `Ctrl+C` 停止。
Node.js 安装后 Daily Usage 可直接使用 `npx`；Local Terminal 会自动列出系统中
可用的 Windows PowerShell、CMD、PowerShell 7 和 WSL。

## 源码开发

```bash
git clone https://github.com/ChinaKai/AHA.git
cd AHA
python3 -m pip install -e .
PYTHONPATH=src python3 -m aha_cli ui --host 127.0.0.1 --port 8788
```

运行测试：`python3 -m pytest`；构建 onebin：
`python3 scripts/build_onebin.py --output dist/aha`。更多说明见 [`docs/`](docs/)。

通过飞书与 AHA 助手对话、查询/创建任务和接收任务推送，参见
[`docs/feishu-assistant.md`](docs/feishu-assistant.md)。

> AHA 默认只监听 `127.0.0.1`。跨设备访问时请启用 Web auth token，并优先使用
> SSH、VPN 或受保护的反向代理，不要把无认证端口直接暴露到公网。
