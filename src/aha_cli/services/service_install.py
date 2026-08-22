from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import secrets
import subprocess
from typing import Callable

from aha_cli import platform


DEFAULT_SERVICE_NAME = "aha.service"
DEFAULT_BIND = "127.0.0.1"
DEFAULT_PORT = 8788
DEFAULT_SYSTEMD_PATH = "%h/.local/bin:/usr/local/bin:/usr/bin:/bin"


class ServiceInstallError(RuntimeError):
    pass


@dataclass(frozen=True)
class UserServiceSpec:
    bin_path: Path
    aha_home: Path
    service_name: str = DEFAULT_SERVICE_NAME
    bind: str = DEFAULT_BIND
    port: int = DEFAULT_PORT
    run_id: str = ""
    auth_required: bool = True
    auth_token_file: Path | None = None
    allow_unsafe_bind: bool = False
    package_manager: str = ""

    def normalized(self) -> "UserServiceSpec":
        bin_path = self.bin_path.expanduser().resolve(strict=False)
        aha_home = self.aha_home.expanduser().resolve(strict=False)
        service_name = normalize_service_name(self.service_name)
        bind = str(self.bind or DEFAULT_BIND).strip() or DEFAULT_BIND
        try:
            port = int(self.port)
        except (TypeError, ValueError) as exc:
            raise ServiceInstallError("port must be an integer") from exc
        if not 1 <= port <= 65535:
            raise ServiceInstallError("port must be between 1 and 65535")
        if not self.auth_required and bind_host_exposes_network(bind) and not self.allow_unsafe_bind:
            raise ServiceInstallError(
                f"--no-auth with {bind}:{port} requires --allow-unsafe-bind or --host 127.0.0.1"
            )
        token_file = (
            self.auth_token_file.expanduser().resolve(strict=False)
            if self.auth_token_file is not None
            else aha_home / "web-token"
        )
        return UserServiceSpec(
            bin_path=bin_path,
            aha_home=aha_home,
            service_name=service_name,
            bind=bind,
            port=port,
            run_id=str(self.run_id or "").strip(),
            auth_required=bool(self.auth_required),
            auth_token_file=token_file,
            allow_unsafe_bind=bool(self.allow_unsafe_bind),
            package_manager=str(self.package_manager or "").strip(),
        )


CommandRunner = Callable[[list[str]], subprocess.CompletedProcess[str]]


def normalize_service_name(value: str | None) -> str:
    name = str(value or DEFAULT_SERVICE_NAME).strip() or DEFAULT_SERVICE_NAME
    return name if name.endswith(".service") else f"{name}.service"


def bind_host_exposes_network(bind: str) -> bool:
    value = str(bind or "").strip().lower()
    if value in {"", "0.0.0.0", "::", "[::]"}:
        return True
    if value in {"localhost", "127.0.0.1", "::1", "[::1]"} or value.startswith("127."):
        return False
    return True


def systemd_user_dir() -> Path:
    configured = str(os.environ.get("XDG_CONFIG_HOME") or "").strip()
    base = Path(configured).expanduser() if configured else Path.home() / ".config"
    return base / "systemd" / "user"


def systemd_user_unit_path(service_name: str = DEFAULT_SERVICE_NAME) -> Path:
    return systemd_user_dir() / normalize_service_name(service_name)


def _systemd_quote(value: object) -> str:
    text = str(value)
    text = text.replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%")
    return f'"{text}"'


def _systemd_env(key: str, value: object) -> str:
    text = str(value).replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%")
    return f'Environment="{key}={text}"'


def prepare_user_service(
    aha_home: Path,
    *,
    auth_required: bool = True,
    auth_token_file: Path | None = None,
) -> dict:
    home = aha_home.expanduser().resolve(strict=False)
    home.mkdir(parents=True, exist_ok=True)
    token_path = (
        auth_token_file.expanduser().resolve(strict=False)
        if auth_token_file is not None
        else home / "web-token"
    )
    created = False
    if auth_required:
        token_path.parent.mkdir(parents=True, exist_ok=True)
        if not token_path.is_file() or not token_path.read_text(encoding="utf-8").strip():
            token_path.write_text(secrets.token_urlsafe(32) + "\n", encoding="utf-8")
            created = True
        try:
            token_path.chmod(0o600)
        except OSError:
            pass
    return {
        "aha_home": str(home),
        "auth_required": bool(auth_required),
        "auth_token_file": str(token_path) if auth_required else "",
        "token_created": created,
    }


def render_systemd_user_unit(spec: UserServiceSpec) -> str:
    normalized = spec.normalized()
    command = [
        _systemd_quote(normalized.bin_path),
        "--home",
        _systemd_quote(normalized.aha_home),
        "ui",
    ]
    if normalized.run_id:
        command.append(_systemd_quote(normalized.run_id))
    command.extend(["--host", _systemd_quote(normalized.bind), "--port", str(normalized.port)])
    if normalized.auth_required:
        command.extend(["--auth-token-file", _systemd_quote(normalized.auth_token_file)])
    if normalized.allow_unsafe_bind:
        command.append("--allow-unsafe-bind")
    lines = [
        "[Unit]",
        "Description=AHA Web UI",
        "After=network-online.target",
        "Wants=network-online.target",
        "",
        "[Service]",
        "Type=simple",
        "WorkingDirectory=%h",
        _systemd_env("PATH", DEFAULT_SYSTEMD_PATH),
        _systemd_env("AHA_HOME", normalized.aha_home),
        _systemd_env("AHA_INSTALL_BIN", normalized.bin_path),
        _systemd_env("AHA_SERVICE_NAME", normalized.service_name),
    ]
    if normalized.package_manager:
        lines.append(_systemd_env("AHA_PACKAGE_MANAGER", normalized.package_manager))
    lines.extend(
        [
            _systemd_env("PYTHONUNBUFFERED", "1"),
            "ExecStart=" + " ".join(command),
            "Restart=on-failure",
            "RestartSec=3",
            "",
            "[Install]",
            "WantedBy=default.target",
            "",
        ]
    )
    return "\n".join(lines)


def render_packaged_systemd_user_unit(
    *,
    bin_path: str = "/usr/bin/aha",
    service_name: str = DEFAULT_SERVICE_NAME,
) -> str:
    normalized_name = normalize_service_name(service_name)
    return "\n".join(
        [
            "[Unit]",
            "Description=AHA Web UI",
            "After=network-online.target",
            "Wants=network-online.target",
            "",
            "[Service]",
            "Type=simple",
            "WorkingDirectory=%h",
            _systemd_env("PATH", DEFAULT_SYSTEMD_PATH),
            _systemd_env("AHA_INSTALL_BIN", "/usr/lib/aha/aha"),
            _systemd_env("AHA_SERVICE_NAME", normalized_name),
            _systemd_env("AHA_PACKAGE_MANAGER", "deb"),
            _systemd_env("PYTHONUNBUFFERED", "1"),
            (
                f"ExecStartPre={_systemd_quote(bin_path)} --home %h/.aha service prepare-user "
                "--aha-home %h/.aha --auth-token-file %h/.aha/web-token"
            ),
            (
                f"ExecStart={_systemd_quote(bin_path)} --home %h/.aha ui --host 127.0.0.1 "
                "--port 8788 --auth-token-file %h/.aha/web-token"
            ),
            "Restart=on-failure",
            "RestartSec=3",
            "",
            "[Install]",
            "WantedBy=default.target",
            "",
        ]
    )


def _default_command_runner(argv: list[str]) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            argv,
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
            **platform.hidden_subprocess_kwargs(),
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise ServiceInstallError(f"failed to run {' '.join(argv)}: {exc}") from exc


def _run_systemctl(
    args: list[str],
    *,
    command_runner: CommandRunner,
    tolerate_failure: bool = False,
) -> None:
    result = command_runner(["systemctl", "--user", *args])
    if result.returncode == 0 or tolerate_failure:
        return
    details = (result.stderr or result.stdout or "").strip()
    suffix = f": {details}" if details else ""
    raise ServiceInstallError(f"systemctl --user {' '.join(args)} failed{suffix}")


def install_user_service(
    spec: UserServiceSpec,
    *,
    enable: bool = True,
    start: bool = True,
    dry_run: bool = False,
    unit_path: Path | None = None,
    command_runner: CommandRunner | None = None,
) -> dict:
    normalized = spec.normalized()
    destination = (
        unit_path.expanduser().resolve(strict=False)
        if unit_path is not None
        else systemd_user_unit_path(normalized.service_name)
    )
    unit = render_systemd_user_unit(normalized)
    if dry_run:
        return {
            "service": normalized.service_name,
            "unit_path": str(destination),
            "unit": unit,
            "enabled": False,
            "started": False,
            "dry_run": True,
            "prepared": None,
        }
    if not normalized.bin_path.is_file():
        raise ServiceInstallError(f"AHA executable does not exist: {normalized.bin_path}")
    prepared = prepare_user_service(
        normalized.aha_home,
        auth_required=normalized.auth_required,
        auth_token_file=normalized.auth_token_file,
    )
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(unit, encoding="utf-8")
    runner = command_runner or _default_command_runner
    _run_systemctl(["daemon-reload"], command_runner=runner)
    if enable:
        _run_systemctl(["enable", normalized.service_name], command_runner=runner)
    if start:
        _run_systemctl(["restart", normalized.service_name], command_runner=runner)
    return {
        "service": normalized.service_name,
        "unit_path": str(destination),
        "unit": unit,
        "enabled": bool(enable),
        "started": bool(start),
        "dry_run": False,
        "prepared": prepared,
    }


def uninstall_user_service(
    service_name: str = DEFAULT_SERVICE_NAME,
    *,
    stop: bool = True,
    unit_path: Path | None = None,
    command_runner: CommandRunner | None = None,
) -> dict:
    normalized_name = normalize_service_name(service_name)
    destination = (
        unit_path.expanduser().resolve(strict=False)
        if unit_path is not None
        else systemd_user_unit_path(normalized_name)
    )
    runner = command_runner or _default_command_runner
    if stop:
        _run_systemctl(["disable", "--now", normalized_name], command_runner=runner, tolerate_failure=True)
    removed = False
    try:
        destination.unlink()
        removed = True
    except FileNotFoundError:
        pass
    _run_systemctl(["daemon-reload"], command_runner=runner, tolerate_failure=True)
    return {
        "service": normalized_name,
        "unit_path": str(destination),
        "stopped": bool(stop),
        "removed": removed,
    }


__all__ = [
    "DEFAULT_BIND",
    "DEFAULT_PORT",
    "DEFAULT_SERVICE_NAME",
    "ServiceInstallError",
    "UserServiceSpec",
    "bind_host_exposes_network",
    "install_user_service",
    "normalize_service_name",
    "prepare_user_service",
    "render_packaged_systemd_user_unit",
    "render_systemd_user_unit",
    "systemd_user_unit_path",
    "uninstall_user_service",
]
