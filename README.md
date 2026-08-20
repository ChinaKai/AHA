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

新版 bridge 会自动使用无需板端预装工具或目标架构二进制的 raw Shell receiver；旧 bridge 自动回退到可靠的 octal + SHA-256 传输。

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

以下命令在 PowerShell 中运行。下载官方安装脚本后直接执行即可；默认 `Full`
模式会自动创建独立 Python 环境、校验并安装 onebin、补齐 Git、串口和飞书 Python
模块，并在本机没有 Codex/Claude 时安装 Codex CLI 及其 Node.js 运行时。体积较大的
Playwright/Chromium 默认不下载。第三方 CLI
登录和凭据配置始终由用户自行完成，安装器不会代为登录：

```powershell
$Installer = Join-Path $env:TEMP "install_aha.ps1"
Invoke-WebRequest "https://github.com/ChinaKai/AHA/releases/latest/download/install_windows.ps1" -OutFile $Installer
powershell.exe -ExecutionPolicy Bypass -File $Installer
```

安装模式：

| 模式 | 行为 |
| --- | --- |
| `Full`（默认） | 自动安装 Python、Git、pyserial、飞书 SDK，并确保至少一个 Agent CLI；Codex 缺失时补 Node.js，不默认下载 Chromium |
| `Minimal` | 只创建运行环境并安装 AHA onebin，保持原有轻量安装行为 |
| `Offline` | 禁止联网，只从本地离线目录读取 onebin、wheel、浏览器和可选 Python 安装器 |

常用示例：

```powershell
# 轻量安装
& $Installer -Mode Minimal

# Full 模式改装 Claude，或同时安装两个 Agent CLI
& $Installer -Mode Full -AgentBackend Claude
& $Installer -Mode Full -AgentBackend Both

# 需要共享浏览器时显式安装 Playwright/Chromium
& $Installer -Mode Full -WithBrowser

# 只安装指定模块；可选值为 Browser、Hardware、Feishu
& $Installer -Mode Minimal -Modules Browser,Hardware

# 幂等修复；重新校验 onebin，并补齐缺失依赖
& $Installer -Mode Full -Repair
```

安装结果写入 `%LOCALAPPDATA%\AHA\install-report.json`，包含核心版本、SHA-256、
Python 路径、各模块状态和需要用户执行的登录/配置步骤。默认采用尽力安装：核心安装
成功后，单个可选模块失败会在报告中标出；传入 `-StrictModules` 可让任意请求模块失败时
返回错误。共享浏览器通过 `-WithBrowser` 或 `-Modules Browser` 按需安装，首次安装通常
会下载数百 MB Chromium 资源；`-SkipBrowserDownload` 可只安装 Playwright Python 模块。

离线目录结构如下；`SHA256SUMS` 中的 `aha` 校验值会在替换安装文件前验证：

```text
D:\AHA-offline\
  aha
  SHA256SUMS
  python-installer.exe      # 仅目标机没有 Python 时需要
  wheels\                  # playwright / pyserial / lark-channel-sdk 及其依赖 wheel
  ms-playwright\           # 仅 -WithBrowser / -Modules Browser 时需要
```

```powershell
& $Installer -Mode Offline -OfflineDir D:\AHA-offline -AgentBackend None
```

离线模式不会下载安装 Agent CLI；目标机应预装 Codex/Claude，或显式使用
`-AgentBackend None` 跳过 Agent CLI 检查。无论哪种模式，安装器都不会写入模型凭据。

托盘使用 AHA Logo。双击图标可打开 AHA；右键菜单可打开面板、重启服务，并通过“无需解锁开机启动”直接创建或删除 `AtStartup` 计划任务，也可在“设置…”中修改 `AHA_HOME`、Bind 地址、Port 和 Web Token。首次勾选时会依次显示 UAC 管理员授权和当前 Windows 账户凭据窗口；成功后 AHA Web 在下次开机时无需登录或解锁即可启动，HKCU Run 仍负责登录后显示托盘。保存设置后 Web 服务会自动重启；未启用计划任务时，选择“退出 AHA”会结束完整 Web 进程树并释放监听端口。也可在安装时直接启用：

```powershell
# 请在“以管理员身份运行”的 PowerShell 中执行；首次会提示输入当前 Windows 账户密码
powershell.exe -ExecutionPolicy Bypass -File $Installer -EnableStartup
```

默认安装位置是 `%LOCALAPPDATA%\AHA\aha`，数据目录是 `%USERPROFILE%\.aha`，Web UI 是 <http://127.0.0.1:8788>。安装脚本会在当前用户的开始菜单创建带 AHA Logo 的 `AHA` 快捷方式；退出托盘后可按 Win 键搜索 `AHA` 重新启动，也可将其固定到任务栏。若不需要快捷方式，安装时传入 `-NoShortcut`。托盘设置保存在 `%LOCALAPPDATA%\AHA\tray.json`；Web Token 明文只写入所选 `AHA_HOME\web-token`。

`-EnableStartup` 创建根目录下的 `\AHA Web` Windows Task Scheduler 任务，触发器为系统启动（`AtStartup`）。任务不使用 SYSTEM，而以当前 Windows 用户、`RunLevel Limited` 和密码登录令牌运行，以便访问该用户的 `AHA_HOME`、用户级配置、凭据与网络身份。密码由 Task Scheduler 保存为 LSA 保护的任务机密，不写入 AHA 配置、命令行或日志。若企业策略禁止保存计划任务凭据或禁止该账户进行批处理登录，安装器会失败并保留错误，不会降级为 SYSTEM。HKCU Run 只在用户登录后显示通知区托盘；托盘附着到已运行的 Web 服务，退出图标不会停止后台任务。

重复安装会更新并复用现有任务，不会再次询问密码。Windows 账户密码变更后，可显式刷新任务凭据：

```powershell
$Credential = Get-Credential -UserName ([Security.Principal.WindowsIdentity]::GetCurrent().Name)
& $Installer -EnableStartup -StartupCredential $Credential
```

从旧版 HKCU-only 自启动升级后，可在托盘中勾选“无需解锁开机启动”完成迁移，也可用管理员 PowerShell 重新执行一次 `-EnableStartup`。托盘内置的配置 helper 不会把密码放入 AHA 配置、命令行或日志。卸载前先退出托盘，然后执行以下命令；计划任务、登录启动项、快捷方式和安装文件会被幂等清理，`AHA_HOME` 数据会保留：

```powershell
powershell.exe -ExecutionPolicy Bypass -File $Installer -Uninstall
```

未启用 `-EnableStartup` 时仍保持原有托盘/快捷方式使用方式。托盘模式下，Web 请求触发的 Git、后端探测和其他辅助进程会以无控制台窗口方式运行。

Full 模式会检测现有 Codex/Claude；若两者都不存在，`-AgentBackend Auto` 默认安装
Codex。安装完成后只需手动运行对应命令完成登录：

```powershell
codex
claude
```

Claude Code 在 Windows 可直接使用 PowerShell；Full 模式同时安装 Git，可供 Knowledge
同步和 Bash 工作流使用。

Agent 需要启动跨对话回合存活的预览服务、watcher 或 tunnel 时，应使用 AHA Web 托管进程，而不是 Codex/Claude 工具自身的后台任务：

```powershell
aha managed-process start preview --cwd . -- python -m http.server 8790
aha managed-process status preview
aha managed-process stop preview
```

在 AHA backend session 内，run/task/agent 范围由环境变量自动继承。托管进程随模型回合保持运行，但所属任务进入终态或 AHA Web 服务重启/退出时会受控停止。

高级用户仍可直接在 AHA venv 中维护模块；通常无需手动执行：

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

新版 bridge 会自动使用无需板端预装工具或目标架构二进制的 raw Shell receiver；旧 bridge 自动回退到可靠的 octal + SHA-256 传输。

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
