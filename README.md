# AHA

[简体中文](README.md) | [English](README.en.md)

AHA（Agent Help Agent）是一个本地优先的 AI Agent 工作台。它使用
**Run → Task → Agent** 组织 Codex、Claude、OpenCode 等后端，并通过 Web UI
管理对话、上下文、共享浏览器、本机终端和硬件调试。

AHA 不提供模型或账号。请安装至少一个 Agent CLI，并通过 CLI 登录或在
AHA Settings 中配置 API Provider。数据默认保存在 `~/.aha`。

## 依赖

| 程序 | 要求 | 用途 |
| --- | --- | --- |
| Codex CLI、Claude Code 或 OpenCode | 至少一个 | 执行 AI Task |
| Node.js + npm | 使用 Codex 时需要 | 安装 Codex CLI |
| Git | 推荐 | 仓库操作与 Knowledge 同步 |
| Chrome、Edge 或 Playwright Chromium | 浏览器功能至少一个 | 共享浏览器 |
| pyserial | 按需 | 串口调试 |
| lark-channel-sdk | 按需 | 飞书助手 |

## Windows 安装

下载并双击最新 Release 中的 `AHA-Setup-x64.exe`。中英文 GUI 向导默认以
Full 模式配置
专用 Python 环境、AHA、Playwright、pyserial、飞书 SDK、Git 和至少一个
Agent CLI，但不会自动登录第三方账号，也不会默认下载 Chromium。
每个 Windows 用户只登记一个程序安装；向导会根据版本显示安装、升级、修复
或需确认的降级，卸载只作用于该登记实例并保留 AHA 数据。

命令行安装示例：

```powershell
.\AHA-Setup-x64.exe

# 只安装核心
.\AHA-Setup-x64.exe --mode Minimal

# 安装 Playwright Chromium
.\AHA-Setup-x64.exe --with-browser

# 启用开机后台服务
.\AHA-Setup-x64.exe --enable-startup
```

默认位置：

```text
程序：%LOCALAPPDATA%\AHA
数据：%USERPROFILE%\.aha
页面：http://127.0.0.1:8788
```

PowerShell 安装脚本 `install_windows.ps1` 继续作为离线、修复和自动化入口。

## Linux 安装

Ubuntu / Debian：

```bash
arch=$(dpkg --print-architecture)
curl -fL "https://github.com/ChinaKai/AHA/releases/latest/download/aha_${arch}.deb" \
  -o "aha_${arch}.deb"
sudo apt install "./aha_${arch}.deb"

systemctl --user enable --now aha.service
```

安装后打开 <http://127.0.0.1:8788>。首次启动会生成 `~/.aha/web-token`。

Linux DEB 需要 Python 3.10+。便携安装仍可下载 Release 中的 `aha` onebin，
也可使用 `install_user_service.sh` 安装自定义路径或服务参数。
