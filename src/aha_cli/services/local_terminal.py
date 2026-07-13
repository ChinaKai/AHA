from __future__ import annotations

import asyncio
import errno
import fcntl
import os
from pathlib import Path
import pty
import signal
import struct
import subprocess
import termios


DEFAULT_TERMINAL_COLS = 100
DEFAULT_TERMINAL_ROWS = 28


def default_shell() -> str:
    shell = str(os.environ.get("SHELL") or "").strip()
    if shell and Path(shell).exists():
        return shell
    return "/bin/sh"


def normalize_terminal_size(cols: object, rows: object) -> tuple[int, int]:
    try:
        normalized_cols = int(cols)
    except (TypeError, ValueError):
        normalized_cols = DEFAULT_TERMINAL_COLS
    try:
        normalized_rows = int(rows)
    except (TypeError, ValueError):
        normalized_rows = DEFAULT_TERMINAL_ROWS
    return max(20, min(normalized_cols, 240)), max(8, min(normalized_rows, 80))


class LocalTerminalSession:
    def __init__(self, *, cwd: Path | None = None, shell: str | None = None) -> None:
        self.cwd = (cwd or Path.cwd()).expanduser().resolve(strict=False)
        self.shell = shell or default_shell()
        self.master_fd: int | None = None
        self._slave_fd: int | None = None
        self.process: subprocess.Popen[bytes] | None = None
        self._output_queue: asyncio.Queue[bytes | None] = asyncio.Queue()
        self._reader_attached = False

    def start(self, *, cols: int = DEFAULT_TERMINAL_COLS, rows: int = DEFAULT_TERMINAL_ROWS) -> None:
        if self.process is not None:
            return
        master_fd, slave_fd = pty.openpty()
        self.master_fd = master_fd
        self._slave_fd = slave_fd
        self.resize(cols=cols, rows=rows)
        flags = fcntl.fcntl(master_fd, fcntl.F_GETFL)
        fcntl.fcntl(master_fd, fcntl.F_SETFL, flags | os.O_NONBLOCK)
        env = os.environ.copy()
        env.update({
            "AHA_LOCAL_TERMINAL": "1",
            "TERM": "xterm-256color",
        })
        self.process = subprocess.Popen(
            [self.shell, "-i"],
            cwd=str(self.cwd),
            env=env,
            stdin=slave_fd,
            stdout=slave_fd,
            stderr=slave_fd,
            close_fds=True,
            start_new_session=True,
        )
        os.close(slave_fd)
        self._slave_fd = None

    def attach_reader(self, loop: asyncio.AbstractEventLoop) -> None:
        if self.master_fd is None or self._reader_attached:
            return
        loop.add_reader(self.master_fd, self._read_ready)
        self._reader_attached = True

    def detach_reader(self, loop: asyncio.AbstractEventLoop) -> None:
        if self.master_fd is None or not self._reader_attached:
            return
        loop.remove_reader(self.master_fd)
        self._reader_attached = False

    def _read_ready(self) -> None:
        if self.master_fd is None:
            return
        while True:
            try:
                chunk = os.read(self.master_fd, 4096)
            except BlockingIOError:
                break
            except OSError as exc:
                if exc.errno in {errno.EIO, errno.EBADF}:
                    self._output_queue.put_nowait(None)
                    break
                self._output_queue.put_nowait(f"\r\n[AHA terminal read error: {exc}]\r\n".encode("utf-8", errors="replace"))
                self._output_queue.put_nowait(None)
                break
            if not chunk:
                self._output_queue.put_nowait(None)
                break
            self._output_queue.put_nowait(chunk)

    async def read(self) -> bytes | None:
        return await self._output_queue.get()

    def write(self, data: str) -> None:
        if self.master_fd is None or not data:
            return
        os.write(self.master_fd, data.encode("utf-8", errors="surrogatepass"))

    def resize(self, *, cols: object, rows: object) -> None:
        if self.master_fd is None:
            return
        normalized_cols, normalized_rows = normalize_terminal_size(cols, rows)
        size = struct.pack("HHHH", normalized_rows, normalized_cols, 0, 0)
        fcntl.ioctl(self.master_fd, termios.TIOCSWINSZ, size)

    async def wait(self) -> int:
        if self.process is None:
            return 0
        return int(await asyncio.to_thread(self.process.wait))

    async def terminate(self) -> None:
        process = self.process
        if process is not None and process.poll() is None:
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except OSError:
                process.terminate()
            try:
                await asyncio.to_thread(process.wait, timeout=1.0)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except OSError:
                    process.kill()
                await asyncio.to_thread(process.wait)
        if self._slave_fd is not None:
            try:
                os.close(self._slave_fd)
            except OSError:
                pass
            self._slave_fd = None
        if self.master_fd is not None:
            try:
                os.close(self.master_fd)
            except OSError:
                pass
            self.master_fd = None
