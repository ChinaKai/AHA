# AHA

[简体中文](README.md) | [English](README.en.md)

AHA means `agent help agent`.

AHA is a local CLI and web UI for coordinating task-scoped AI agents. It stores
state in an AHA home, uses runs and tasks to keep work organized, and can launch
Codex or Claude-backed agents from the browser dashboard.

Data is stored under `~/.aha` by default. Use `--home <path>` to choose another
AHA home.

## Start From Source

Start the web UI directly from the source checkout:

```bash
PYTHONPATH=src python3 -m aha_cli ui --host 127.0.0.1 --port 8788
```

Open:

```text
http://127.0.0.1:8788
```

On first open, the UI shows an initialization form. Saving that form writes
`.aha/config.json` in the selected AHA home. After that, create a run, then
create tasks inside the run.

## Build Onebin

Build a single-file executable zipapp from the source checkout:

```bash
python3 scripts/build_onebin.py --output dist/aha
```

## Run Onebin

Run the packaged artifact directly on a machine with Python 3.10+:

```bash
./dist/aha --help
./dist/aha --home ~/.aha ui --host 0.0.0.0 --port 8788
```

The onebin contains the AHA Python modules and browser static files. External
agent CLIs such as `codex` and `claude` still need to be installed and
authenticated on the target machine.

When the onebin dashboard starts managed backends, it launches child AHA backend
commands through the same onebin artifact instead of requiring an installed
`aha_cli` Python module.

## Install / Upgrade As A User Systemd Service

Without cloning the source checkout, download the installer from the latest
GitHub Release and install the released onebin:

```bash
curl -fsSL -o /tmp/install_aha.sh \
  https://github.com/ChinaKai/AHA/releases/latest/download/install_user_service.sh
bash /tmp/install_aha.sh
```

By default this downloads the `aha` artifact from the latest `ChinaKai/AHA`
release, installs it to `~/.local/bin/aha`, and starts a user systemd service.
You can install a specific tag or a local release artifact:

```bash
bash /tmp/install_aha.sh --version v0.1.0
bash /tmp/install_aha.sh --artifact ./dist/aha
```

For local development, build from the current source checkout explicitly:

```bash
scripts/install_user_service.sh --build-from-source
```

By default the service runs:

```text
aha --home ~/.aha ui --host 127.0.0.1 --port 8788 --auth-token-file ~/.aha/web-token
```

The install script enables Web UI token login by default and creates or reuses
`web-token` under the AHA home. With the default home, read the login token from
`~/.aha/web-token`; if you pass `--aha-home`, use that directory instead.

Common overrides:

```bash
scripts/install_user_service.sh --port 8788 --aha-home ~/.aha
scripts/install_user_service.sh --port 8788 --run-id <run-id>
```

The Web UI upgrade button calls the installed onebin's
`aha service upgrade-user` command. It downloads the next release artifact using
the release metadata recorded in the systemd unit, replaces the installed
executable, and restarts `aha.service` without requiring a source checkout.

Check the service:

```bash
systemctl --user status aha.service
journalctl --user -u aha.service -f
```

If the service should start before login, enable lingering for the user:

```bash
sudo loginctl enable-linger "$USER"
```

## Automated Releases

Pushing a `v*` tag triggers `.github/workflows/release.yml`. The workflow builds
the onebin, runs release smokes, writes `SHA256SUMS`, and publishes both `aha`
and `install_user_service.sh` to the GitHub Release.

Detailed design notes live in `docs/`.
