# AHA

[简体中文](README.md) | [English](README.en.md)

AHA（Agent Help Agent）是一个本地优先的 AI Agent 工作台。它使用
**Run → Task → Agent** 组织 Codex、Claude、OpenCode 等后端，并通过 Web UI
管理对话、上下文、共享浏览器、本机终端和硬件调试。

AHA 不提供模型或账号。你需要安装至少一个 Agent CLI，并通过 CLI 登录或在
AHA Settings 中配置 API Provider。数据默认保存在 `~/.aha`。

## 依赖

| 程序 | 要求 | 用途 |
| --- | --- | --- |
| Python 3.10+ | 必需 | 运行 AHA onebin |
| Codex CLI、Claude Code 或 OpenCode | 至少一个 | 执行 AI Task |
| Node.js + npm | 使用 Codex 时需要 | 安装 Codex CLI |
| Git | 推荐 | 仓库操作与 Knowledge 同步 |
| Playwright Python | Windows Full 默认安装 | 共享浏览器控制 |
| Chrome、Edge 或 Playwright Chromium | 浏览器功能至少一个 | 浏览器运行时 |
| pyserial | 按需 | 串口 / Windows COM 调试 |
| lark-channel-sdk | 按需 | 飞书助手 |

## Windows 安装

在 PowerShell 中运行：

```powershell
$Installer = Join-Path $env:TEMP "install_aha.ps1"
Invoke-WebRequest `
  "https://github.com/ChinaKai/AHA/releases/latest/download/install_windows.ps1" `
  -OutFile $Installer
powershell.exe -ExecutionPolicy Bypass -File $Installer
```

默认 `Full` 模式会安装 AHA、Python、Git、Playwright、pyserial、飞书 SDK，
并确保至少存在一个 Agent CLI。它优先使用系统 Chrome/Edge，默认不下载
Playwright Chromium。

```powershell
# 只安装 AHA 核心
& $Installer -Mode Minimal

# 显式下载 Playwright Chromium
& $Installer -Mode Full -WithBrowser

# 只安装指定模块；Browser 会请求 Chromium
& $Installer -Mode Minimal -Modules Browser,Hardware

# 安装 Playwright 模块但禁止下载浏览器
& $Installer -Mode Full -SkipBrowserDownload

# 离线安装
& $Installer -Mode Offline -OfflineDir D:\AHA-offline
```

默认安装位置：

```text
程序：%LOCALAPPDATA%\AHA\aha
数据：%USERPROFILE%\.aha
页面：http://127.0.0.1:8788
```

## Linux 安装

以下示例适用于 Ubuntu / Debian：

```bash
sudo apt update
sudo apt install -y python3 python3-venv python3-pip curl git

python3 -m venv ~/.venvs/aha
~/.venvs/aha/bin/python -m pip install --upgrade pip

# 至少安装一个 Agent CLI
npm install --global @openai/codex

# 可选：共享浏览器；系统已有 Chrome/Edge 时无需下载 Chromium
~/.venvs/aha/bin/python -m pip install 'playwright>=1.45,<2'

mkdir -p ~/.local/bin
curl -fL https://github.com/ChinaKai/AHA/releases/latest/download/aha \
  -o ~/.local/bin/aha
chmod +x ~/.local/bin/aha

~/.venvs/aha/bin/python ~/.local/bin/aha \
  --home ~/.aha ui --host 127.0.0.1 --port 8788
```

打开 <http://127.0.0.1:8788>。
