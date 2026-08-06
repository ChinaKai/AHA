"""Bounded backend stdout consumption after a native turn completion event."""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
import queue
import subprocess
import threading
import time

BACKEND_COMPLETION_GRACE_SECONDS = 3.0
BACKEND_TERMINATION_WAIT_SECONDS = 1.0
_POLL_SECONDS = 0.05


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


def _finish_completed_backend_process(process: subprocess.Popen[str]) -> int | None:
    """Stop only a lingering backend parent after its native completion event.

    Do not tree-kill here: a deliberately detached server may be the requested
    result of the turn. If that descendant merely inherited stdout, the backend
    parent has normally exited already and its code can be returned immediately.
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
