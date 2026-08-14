from __future__ import annotations

import os
from pathlib import Path
import re


WSL_UNC_PREFIXES = ("\\\\wsl.localhost\\", "\\\\wsl$\\")
WSL_UNC_RE = re.compile(r"^\\\\wsl(?:\.localhost|\\$)\\([^\\/]+)\\(.*)$", re.IGNORECASE)
WINDOWS_DRIVE_RE = re.compile(r"^([A-Za-z]):[\\/]?(.*)$")


def _strip_wsl_unc_prefix(text: str) -> str | None:
    """Strip a WSL UNC prefix and return the remainder after the distro.

    Accepts both ``\\\\wsl.localhost\\<distro>\\...`` and ``\\\\wsl$\\<distro>\\...``.
    Returns ``None`` when the text is not a WSL UNC path.
    """
    lowered = text.lower()
    for prefix in ("\\\\wsl.localhost\\", "\\\\wsl$\\"):
        if lowered.startswith(prefix):
            return text[len(prefix) :]
    return None


def is_wsl_workspace(path: str | Path | None) -> bool:
    """True when a workspace path is a WSL UNC path (\\wsl.localhost\\<distro>\\...)."""
    text = str(path or "")
    if not text:
        return False
    return text.startswith(WSL_UNC_PREFIXES)


def wsl_distro_and_path(path: str | Path | None) -> tuple[str | None, str | None]:
    """Split a WSL UNC path into (distro, native path).

    ``\\\\wsl.localhost\\Ubuntu-24.04\\home\\kaikai\\project`` ->
    ``("Ubuntu-24.04", "/home/kaikai/project")``.
    """
    text = str(path or "").strip()
    remainder = _strip_wsl_unc_prefix(text)
    if remainder is None:
        return None, None
    # First segment is the distro; the rest is the native path.
    if "\\" not in remainder and "/" not in remainder:
        return remainder, "/"
    first_sep = min(
        [index for index in (remainder.find("\\"), remainder.find("/")) if index >= 0] or [len(remainder)]
    )
    distro = remainder[:first_sep]
    native = "/" + remainder[first_sep:].lstrip("\\/").replace("\\", "/")
    return distro, native


def windows_path_to_wsl(path: str | Path | None) -> str | None:
    """Convert a Windows path to the WSL /mnt/<drive>/... form.

    ``C:\\Users\\toope\\.aha`` -> ``/mnt/c/Users/toope/.aha``. Returns None when
    the path is not a Windows drive path (e.g. already a WSL native path).
    """
    text = str(path or "").strip()
    match = WINDOWS_DRIVE_RE.match(text)
    if not match:
        return None
    drive = match.group(1).lower()
    rest = match.group(2).replace("\\", "/")
    if rest.startswith("/"):
        rest = rest.lstrip("/")
    return f"/mnt/{drive}/{rest}"


def wsl_native_home(windows_home: str | Path | None) -> str | None:
    """Return the WSL-native path for an AHA home directory.

    - Windows drive path -> ``/mnt/<drive>/...``
    - Already WSL-native (``/...``) passes through unchanged.
    """
    text = str(windows_home or "").strip()
    if not text:
        return None
    if text.startswith("/"):
        return text
    return windows_path_to_wsl(text)


def wsl_workspace_native_path(path: str | Path | None) -> str | None:
    """Resolve a workspace path to the WSL-native form.

    - ``\\\\wsl.localhost\\<distro>\\...`` -> ``/...``
    - Windows drive path -> ``/mnt/<drive>/...``
    - Already-native ``/...`` passes through.
    """
    text = str(path or "").strip()
    if not text:
        return None
    _distro, native = wsl_distro_and_path(text)
    if native:
        return native
    converted = windows_path_to_wsl(text)
    if converted:
        return converted
    if text.startswith("/"):
        return text
    return None


def wsl_unc_from_native(distro: str, native_path: str | Path | None) -> str | None:
    """Convert a WSL native path back to the \\\\wsl.localhost\\<distro>\\... form."""
    text = str(native_path or "").strip().lstrip("/")
    if not text or not distro:
        return None
    native = text.replace("/", "\\")
    return f"\\\\wsl.localhost\\{distro}\\{native}"


def host_native_path(path: str | Path | None, *, aha_home: str | Path | None = None) -> str:
    """Resolve a path stored in the AHA config to the current host's native form.

    AHA config paths are written in the platform that owns the AHA home
    (typically Windows). When consumed inside a WSL backend process, those
    Windows-view paths must be converted to the distro-native view before use:

    - ``~`` tokens expand against the AHA home's parent (the user home that
      owns the AHA home), not ``Path.home()``, which inside WSL would resolve
      to the distro native home and split the data.
    - ``C:\\...`` / ``D:\\...`` drive paths become ``/mnt/<drive>/...``.
    - ``\\\\wsl.localhost\\<distro>\\...`` becomes ``/...``.
    - Already-native ``/...`` passes through.

    On Windows (non-WSL) this is effectively ``os.path.expanduser`` with the
    ``~`` resolved against the AHA home's parent, so ``~/.aha/knowledge`` stays
    under the same home as the AHA home.
    """
    raw = str(path or "").strip()
    if not raw:
        return raw
    if aha_home:
        home = str(aha_home)
        # ~/.aha/...  -> <home_parent>/.aha/...
        if raw.startswith("~"):
            base = Path(home).expanduser().resolve()
            if base.name == ".aha":
                base = base.parent
            raw = str(base / raw.lstrip("~/"))
        # Windows drive / UNC -> native view.
        return wsl_workspace_native_path(raw) or raw
    return os.path.expanduser(raw)
