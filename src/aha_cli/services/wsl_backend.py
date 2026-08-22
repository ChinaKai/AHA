from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time

from aha_cli.backends.plugin import process_backend_plugins
from aha_cli.store.paths import aha_home_path

WSL_BACKENDS_CACHE_FILE = "wsl-backends.json"
WSL_PROBE_TIMEOUT = 15.0
WSL_CACHE_TTL_SECONDS = 6 * 3600  # 6h; environment changes are rare


def _wsl_executable() -> str:
    if sys.platform == "win32":
        resolved = shutil.which("wsl.exe")
        if resolved:
            return resolved
        system_root = str(os.environ.get("SystemRoot") or r"C:\Windows")
        return str(Path(system_root) / "System32" / "wsl.exe")
    return "wsl"


def _run_wsl_script(distro: str, script: str) -> tuple[int, str]:
    """Run a bash script inside the given WSL distro, returning (exit, stdout)."""
    command = [_wsl_executable(), "-d", distro, "--", "bash", "-s"]
    try:
        result = subprocess.run(
            command,
            input=script.encode("utf-8"),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=WSL_PROBE_TIMEOUT,
            check=False,
            creationflags=int(getattr(subprocess, "CREATE_NO_WINDOW", 0)),
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return 1, str(exc)
    # wsl.exe may emit UTF-16 when stdout is redirected; strip NUL bytes.
    raw = (result.stdout or b"").decode("utf-8", errors="replace")
    text = raw.replace("\0", "")
    return result.returncode, text


_WSL_PROBE_SCRIPT_PREFIX = r"""
detect() {
  local bin="$1"
  local found=""
  local p
  for p in $(command -v "$bin" 2>/dev/null); do
    case "$p" in /mnt/*) continue ;; esac
    found="$p"; break
  done
  if [ -z "$found" ]; then
    local c
    for c in /usr/local/bin/$bin /usr/bin/$bin "$HOME/.local/bin/$bin" "$HOME"/.nvm/versions/node/*/bin/$bin \
      "$HOME/.npm-global/bin/$bin" "$HOME/.local/share/fnm/node-versions/"*/installation/bin/$bin \
      "$HOME/.volta/bin/$bin" "$HOME/.pyenv/shims/$bin"; do
      if [ -x "$c" ] || [ -L "$c" ]; then found="$c"; break; fi
    done
  fi
  echo "$bin=$found"
}
"""

_WSL_PROBE_SCRIPT_SUFFIX = r"""
# The WSL onebin is launched with an explicit native python so it never
# resolves through the Windows shim (e.g. the CRLF python3 in the AHA bin dir).
detect python3
# WSL native home of the backend user (e.g. /home/kaikai). Needed so the
# Windows-side Web service can resolve claude/codex session files written
# inside the distro (via \\wsl.localhost\..). The `detect` helper only probes
# executables, so emit the home directly.
echo "home=$HOME"
# nvm node path (needed by the codex js launcher)
export NVM_DIR="$HOME/.nvm"
if [ -s "$NVM_DIR/nvm.sh" ]; then
  . "$NVM_DIR/nvm.sh" >/dev/null 2>&1
  command -v node >/dev/null 2>&1 && echo "node=$(command -v node)"
fi
"""


def _wsl_probe_script() -> str:
    backend_names = _expected_probe_backend_names()
    detects = "\n".join(f"detect {name}" for name in backend_names if name)
    return f"{_WSL_PROBE_SCRIPT_PREFIX}\n{detects}\n{_WSL_PROBE_SCRIPT_SUFFIX}"


def _expected_probe_backend_names() -> list[str]:
    return [
        str(plugin.descriptor.default_binary or plugin.descriptor.name).strip()
        for plugin in process_backend_plugins()
    ]


def _parse_probe_output(output: str) -> dict[str, str]:
    result: dict[str, str] = {}
    for line in output.splitlines():
        line = line.strip()
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if key and value and value != "NOT_FOUND":
            result[key] = value
    return result


def probe_wsl_backends(distro: str) -> dict[str, str]:
    """Probe a WSL distro for native process-agent backends and helper paths.

    Returns a dict like ``{"codex": "...", "claude": "...", "node": "..."}``.
    Values are WSL-native absolute paths; keys are absent when not found. The
    WSL session's own ``$HOME`` is used, so no user path is hardcoded.
    """
    _exit_code, output = _run_wsl_script(distro, _wsl_probe_script())
    parsed = _parse_probe_output(output)
    # Drop paths that still point into the Windows mount (shims).
    return {key: value for key, value in parsed.items() if not value.startswith("/mnt/")}


def wsl_backends_cache_path(root: Path) -> Path:
    return aha_home_path(root) / WSL_BACKENDS_CACHE_FILE


def cached_wsl_backends(root: Path, distro: str) -> dict[str, str] | None:
    """Return cached WSL backends for a distro if fresh, else None."""
    path = wsl_backends_cache_path(root)
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(payload, dict):
        return None
    now = time.time()
    entry = payload.get(distro)
    if not isinstance(entry, dict):
        return None
    detected_at = entry.get("detected_at") or 0
    if now - float(detected_at) > WSL_CACHE_TTL_SECONDS:
        return None
    probed_names = entry.get("probed_names")
    if (
        not isinstance(probed_names, list)
        or set(_expected_probe_backend_names()) - {str(name) for name in probed_names}
    ):
        return None
    backends = entry.get("backends")
    return backends if isinstance(backends, dict) else None


def cache_wsl_backends(root: Path, distro: str, backends: dict[str, str]) -> None:
    """Persist probed WSL backends for a distro to the on-disk cache."""
    path = wsl_backends_cache_path(root)
    try:
        payload = {}
        if path.exists():
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                payload = {}
            if not isinstance(payload, dict):
                payload = {}
        payload[distro] = {
            "detected_at": time.time(),
            "probed_names": _expected_probe_backend_names(),
            "backends": backends,
        }
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    except OSError:
        # Cache is best-effort; a failed write only means we re-probe next time.
        pass


def wsl_backends_for_workspace(
    root: Path,
    distro: str,
) -> dict[str, str]:
    """Return WSL backends for a distro, probing once and caching the result.

    On probe failure (or empty result) returns ``{}`` so callers fall back to the
    Windows backend.
    """
    cached = cached_wsl_backends(root, distro)
    if cached:
        return cached
    probed = probe_wsl_backends(distro)
    if probed:
        cache_wsl_backends(root, distro, probed)
    return probed
