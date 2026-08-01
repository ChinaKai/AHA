"""Cross-platform process control: liveness, signaling, and process groups.

POSIX uses signals and process groups (``os.kill``, ``os.killpg``, ``os.getpgid``).
Windows has neither POSIX signals nor process groups:

* Termination uses ``TerminateProcess`` via ``ctypes`` (the ``signal`` module's
  Windows support is limited to console control events, which do not apply to
  spawned agent/bridge processes).
* Liveness is probed via ``OpenProcess``/``GetExitCodeProcess``. This must NEVER
  use ``os.kill(pid, 0)`` on Windows — CPython's ``os.kill`` on Windows treats a
  non-console signal as a terminate request, which would kill the target instead
  of checking it.
* Process groups do not exist; a pid is treated as its own group, and a group
  signal is implemented as a tree-kill (``taskkill /T``).

Callers use the same functions on both platforms. POSIX behavior is byte-for-byte
identical to the previous inline ``os.kill``/``os.killpg``/``os.getpgid`` calls,
so Linux behavior (and tests) are unchanged; the Windows paths are additive.
"""
from __future__ import annotations

import errno
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

_WIN = sys.platform == "win32"

# Win32 access rights and exit codes (used only on Windows).
_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_PROCESS_TERMINATE = 0x0001
_STILL_ACTIVE = 259
_ERROR_ACCESS_DENIED = 5


def current_uid() -> int | None:
    """Effective UID on POSIX; ``None`` on Windows (no UID concept)."""
    if _WIN:
        return None
    return os.geteuid()


def process_exists(pid: object) -> bool:
    """True if ``pid`` references a live, non-zombie process."""
    try:
        value = int(pid)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return False
    if value <= 0:
        return False
    if _WIN:
        return _windows_exists(value)
    try:
        os.kill(value, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    # A zombie still answers signal 0; treat it as gone so callers stop waiting.
    return not _is_zombie(value)


def _is_zombie(pid: int) -> bool:
    try:
        stat_text = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
    except OSError:
        return False
    closing = stat_text.rfind(")")
    return closing >= 0 and stat_text[closing + 2 : closing + 3] == "Z"


def send_signal(pid: int, sig: int) -> None:
    """Deliver ``sig`` to one process.

    Mirrors ``os.kill``: raises :class:`ProcessLookupError` if the pid is gone and
    :class:`PermissionError` if the caller lacks rights. On Windows any signal
    terminates the process via ``TerminateProcess`` — there is no graceful/force
    distinction at the signal level.
    """
    if _WIN:
        _windows_terminate(int(pid))
        return
    os.kill(int(pid), sig)


def signal_process_group(pgid_or_pid: int, sig: int) -> None:
    """Signal a POSIX process group (by pgid) or kill a process tree (Windows).

    On POSIX this is ``os.killpg``. On Windows there are no process groups, so the
    argument is treated as a pid and its whole tree is terminated (``taskkill /T``).
    """
    if _WIN:
        _windows_tree_terminate(int(pgid_or_pid), force=True)
        return
    os.killpg(int(pgid_or_pid), sig)


def process_group_id(pid: int) -> int:
    """Return the process-group id of ``pid`` (POSIX), or ``pid`` itself (Windows)."""
    if _WIN:
        return int(pid)
    return os.getpgid(int(pid))


def terminate_process(pid: int, *, timeout: float | None = None) -> bool:
    """Send SIGTERM (POSIX) / TerminateProcess (Windows) and optionally wait.

    With ``timeout`` set, poll until the process exits or the deadline elapses;
    return ``True`` if it exited. Without ``timeout``, return immediately.
    """
    if not pid or pid <= 0:
        return False
    send_signal(int(pid), signal.SIGTERM)
    if timeout is None:
        return True
    deadline = time.monotonic() + max(0.0, float(timeout))
    while time.monotonic() < deadline:
        if not process_exists(pid):
            return True
        time.sleep(0.02)
    return not process_exists(pid)


# --- Parent-death binding --------------------------------------------------

_PR_SET_PDEATHSIG = 1


def _posix_parent_death_signal() -> None:
    # Runs in the child between fork and exec (POSIX only): ask the kernel to
    # deliver SIGTERM when the spawning process dies, so a bridge never outlives
    # the runtime that owns it.
    try:
        import ctypes

        libc = ctypes.CDLL("libc.so.6", use_errno=True)
        libc.prctl(_PR_SET_PDEATHSIG, signal.SIGTERM)
    except (OSError, AttributeError):
        pass


def parent_death_preexec():
    """Return a ``preexec_fn`` callable that binds the child to die with this
    process, or ``None`` on Windows.

    On POSIX the returned callable arms ``PR_SET_PDEATHSIG`` in the child. On
    Windows ``preexec_fn`` is unsupported by :mod:`subprocess` (it raises
    ``ValueError``), so this returns ``None`` and parent-death is instead handled
    post-spawn by :func:`assign_parent_death` (a kill-on-close Job Object).
    """
    if _WIN:
        return None
    return _posix_parent_death_signal


def assign_parent_death(proc) -> None:
    """Bind a spawned subprocess to die when this process exits.

    POSIX: no-op (``preexec_fn`` already armed PDEATHSIG in the child). Windows:
    add the process to a single Job Object created with
    ``JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE`` so the kernel reaps it (and any
    grandchildren) when this process exits and the job handle closes.
    """
    if not _WIN:
        return
    _windows_assign_kill_job(proc.pid)


# --- Windows helpers -------------------------------------------------------

def _windows_kernel32():
    import ctypes

    return ctypes.WinDLL("kernel32", use_last_error=True)


def _windows_exists(pid: int) -> bool:
    import ctypes

    kernel32 = _windows_kernel32()
    handle = kernel32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return False
    try:
        code = ctypes.c_ulong()
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
            return False
        return code.value == _STILL_ACTIVE
    finally:
        kernel32.CloseHandle(handle)


def _windows_terminate(pid: int) -> None:
    import ctypes

    kernel32 = _windows_kernel32()
    handle = kernel32.OpenProcess(_PROCESS_TERMINATE, False, pid)
    if not handle:
        err = ctypes.get_last_error()
        if err == _ERROR_ACCESS_DENIED:
            raise PermissionError(errno.EACCES, f"access denied to process {pid}")
        raise ProcessLookupError(errno.ESRCH, f"process {pid} not found")
    try:
        kernel32.TerminateProcess(handle, 1)
    finally:
        kernel32.CloseHandle(handle)


def _windows_tree_terminate(pid: int, *, force: bool) -> None:
    cmd = ["taskkill", "/PID", str(pid), "/T"]
    if force:
        cmd.append("/F")
    try:
        subprocess.run(
            cmd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
    except OSError:
        return


# Job Object parent-death binding (Windows only).
_JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000
_JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9
_PROCESS_SET_QUOTA = 0x0100
_kill_job_handle = None


def _windows_kill_job():
    global _kill_job_handle
    if _kill_job_handle is not None:
        return _kill_job_handle
    import ctypes
    from ctypes import wintypes

    kernel32 = _windows_kernel32()
    kernel32.CreateJobObjectW.restype = ctypes.c_void_p
    job = kernel32.CreateJobObjectW(None, None)
    if not job:
        return None

    class _IO_COUNTERS(ctypes.Structure):
        _fields_ = [
            ("ReadOperationCount", ctypes.c_uint64),
            ("WriteOperationCount", ctypes.c_uint64),
            ("OtherOperationCount", ctypes.c_uint64),
            ("ReadTransferCount", ctypes.c_uint64),
            ("WriteTransferCount", ctypes.c_uint64),
            ("OtherTransferCount", ctypes.c_uint64),
        ]

    class _BASIC_LIMITS(ctypes.Structure):
        _fields_ = [
            ("PerProcessUserTimeLimit", ctypes.c_int64),
            ("PerJobUserTimeLimit", ctypes.c_int64),
            ("LimitFlags", wintypes.DWORD),
            ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t),
            ("ActiveProcessLimit", wintypes.DWORD),
            ("Affinity", ctypes.c_void_p),
            ("PriorityClass", wintypes.DWORD),
            ("SchedulingClass", wintypes.DWORD),
        ]

    class _EXTENDED_LIMITS(ctypes.Structure):
        _fields_ = [
            ("BasicLimitInformation", _BASIC_LIMITS),
            ("IoInfo", _IO_COUNTERS),
            ("ProcessMemoryLimit", ctypes.c_size_t),
            ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryUsed", ctypes.c_size_t),
            ("PeakJobMemoryUsed", ctypes.c_size_t),
        ]

    info = _EXTENDED_LIMITS()
    info.BasicLimitInformation.LimitFlags = _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    kernel32.SetInformationJobObject(
        job,
        _JOB_OBJECT_EXTENDED_LIMIT_INFORMATION,
        ctypes.byref(info),
        ctypes.sizeof(info),
    )
    _kill_job_handle = job
    return job


def _windows_assign_kill_job(pid: int) -> None:
    import ctypes

    job = _windows_kill_job()
    if job is None:
        return
    kernel32 = _windows_kernel32()
    kernel32.OpenProcess.restype = ctypes.c_void_p
    handle = kernel32.OpenProcess(_PROCESS_SET_QUOTA | _PROCESS_TERMINATE, False, int(pid))
    if not handle:
        return
    try:
        kernel32.AssignProcessToJobObject(job, handle)
    finally:
        kernel32.CloseHandle(handle)
