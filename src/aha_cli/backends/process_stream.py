"""Bounded backend stdout consumption after a native turn completion event."""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
import os
import queue
import signal
import subprocess
import threading
import time

BACKEND_COMPLETION_GRACE_SECONDS = 3.0
BACKEND_TERMINATION_WAIT_SECONDS = 1.0
_POLL_SECONDS = 0.05

_WIN = os.name == "nt"


@dataclass(frozen=True)
class ProcessStreamResult:
    exit_code: int
    process_exit_code: int | None
    completion_seen: bool
    completion_grace_exceeded: bool


def _poll(process: subprocess.Popen[str]) -> int | None:
    poll = getattr(process, "poll", None)
    return poll() if callable(poll) else None


def _wait_with_timeout(process: subprocess.Popen[str], timeout: float) -> int | None:
    try:
        return process.wait(timeout=max(0.0, timeout))
    except subprocess.TimeoutExpired:
        return None


def terminate_process_tree(pid: int) -> None:
    """Kill ``pid`` and its whole descendant tree.

    This is used only after a backend's native completion record was seen but
    stdout did not EOF within the grace window. A descendant that merely
    inherited the stdout pipe would otherwise keep the backend turn alive
    indefinitely; the native result is already captured, so killing the tree is
    safe and does not discard model output.
    """
    if not pid or pid <= 0:
        return
    if _WIN:
        try:
            subprocess.run(
                ["taskkill", "/PID", str(int(pid)), "/T", "/F"],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            )
        except OSError:
            return
        return
    try:
        os.killpg(int(pid), signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        pass


def _finish_completed_backend_process(process: subprocess.Popen[str]) -> int | None:
    """Stop a lingering backend parent after its native completion event.

    Prefer not to tree-kill: a deliberately detached server may be the requested
    result of the turn, and if the parent has already exited its code can be
    returned immediately. Only when the parent itself stays alive past the grace
    window do we escalate to a tree kill — the native result is already in hand,
    and the lingering descendant is holding the stdout pipe open.
    """
    exit_code = _poll(process)
    if exit_code is not None:
        return exit_code
    try:
        process.terminate()
    except (AttributeError, OSError):
        pass

    exit_code = _wait_with_timeout(process, BACKEND_TERMINATION_WAIT_SECONDS)
    if exit_code is not None:
        return exit_code
    # Escalate: the parent refused to die within the grace window. A descendant
    # is keeping it (or the pipe) alive, so take down the whole tree.
    terminate_process_tree(getattr(process, "pid", None))
    try:
        process.kill()
    except (AttributeError, OSError):
        pass
    return _wait_with_timeout(process, BACKEND_TERMINATION_WAIT_SECONDS)


def consume_process_output(
    process: subprocess.Popen[str],
    *,
    handle_line: Callable[[str], None],
    is_completion_line: Callable[[str], bool],
    completion_grace_seconds: float = BACKEND_COMPLETION_GRACE_SECONDS,
) -> ProcessStreamResult:
    """Consume stdout until EOF, with a bounded wait after native completion.

    Backend CLIs normally close stdout immediately after their native completion
    record. On Windows, an agent-started long-running descendant can inherit the
    pipe or job and keep the backend turn alive indefinitely. Reading on a daemon
    thread lets the caller stop waiting without changing active-turn behavior.
    """
    assert process.stdout is not None
    items: queue.Queue[tuple[str, object]] = queue.Queue()

    def read_stdout() -> None:
        try:
            for raw_line in process.stdout:
                items.put(("line", raw_line))
        except BaseException as exc:  # Preserve the old iterator error behavior.
            items.put(("error", exc))
        finally:
            items.put(("eof", None))

    reader = threading.Thread(target=read_stdout, name="aha-backend-stdout", daemon=True)
    reader.start()

    completion_seen = False
    deadline: float | None = None
    reader_error: BaseException | None = None

    while True:
        timeout = _POLL_SECONDS
        if deadline is not None:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                process_exit_code = _finish_completed_backend_process(process)
                return ProcessStreamResult(
                    exit_code=0,
                    process_exit_code=process_exit_code,
                    completion_seen=True,
                    completion_grace_exceeded=True,
                )
            timeout = min(timeout, remaining)
        try:
            kind, payload = items.get(timeout=timeout)
        except queue.Empty:
            continue

        if kind == "line":
            raw_line = str(payload)
            handle_line(raw_line)
            if not completion_seen and is_completion_line(raw_line.strip()):
                completion_seen = True
                deadline = time.monotonic() + max(0.0, float(completion_grace_seconds))
            continue
        if kind == "error":
            reader_error = payload if isinstance(payload, BaseException) else RuntimeError(str(payload))
            continue
        if kind == "eof":
            break

    if reader_error is not None:
        raise reader_error
    exit_code = process.wait()
    return ProcessStreamResult(
        exit_code=exit_code,
        process_exit_code=exit_code,
        completion_seen=completion_seen,
        completion_grace_exceeded=False,
    )
