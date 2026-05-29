# AHA

[简体中文](README.md) | [English](README.en.md)

AHA 是 `agent help agent`。

AHA 是一个本地 CLI 和 Web UI，用来协调按 task 隔离的 AI agent。它把
状态保存在 AHA home 中，用 run 和 task 组织工作，并可以从浏览器面板启动
Codex 或 Claude 后端 agent。

默认数据目录是 `~/.aha`。可以用 `--home <path>` 指定其他 AHA home。

## 从源码启动

直接从源码目录启动 Web UI：

```bash
PYTHONPATH=src python3 -m aha_cli ui --host 127.0.0.1 --port 8788
```

打开：

```text
http://127.0.0.1:8788
```

首次打开时，UI 会显示初始化表单。保存后才会在选定的 AHA home 中写入
`.aha/config.json`。之后先创建 run，再在 run 里创建 task。

## 打包 Onebin

从源码目录打包单文件 zipapp：

```bash
python3 scripts/build_onebin.py --output dist/aha
```

## 使用 Onebin 启动

在有 Python 3.10+ 的机器上直接运行：

```bash
./dist/aha --help
./dist/aha --home ~/.aha ui --host 0.0.0.0 --port 8788
```

onebin 包含 AHA Python 模块和浏览器静态文件。外部 agent CLI，例如
`codex` 和 `claude`，仍需要在目标机器上安装并完成认证。

onebin 面板启动托管 backend 时，会通过同一个 onebin artifact 启动子 AHA
backend 命令，不要求目标机器额外安装可 import 的 `aha_cli` Python 模块。

## 从源码安装为 User Systemd 服务

在源码目录里构建 onebin 到 `~/.local/bin/aha`，并安装、启动 user systemd 服务：

```bash
scripts/install_user_service.sh
```

默认服务命令是：

```text
aha --home ~/.aha ui --host 0.0.0.0 --port 8788
```

常用参数：

```bash
scripts/install_user_service.sh --port 8788 --aha-home ~/.aha
scripts/install_user_service.sh --port 8788 --run-id <run-id>
```

查看服务状态：

```bash
systemctl --user status aha.service
journalctl --user -u aha.service -f
```

如果希望服务在用户登录前也能启动，开启 lingering：

```bash
sudo loginctl enable-linger "$USER"
```

更详细的设计说明在 `docs/` 目录。
