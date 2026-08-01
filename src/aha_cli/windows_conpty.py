"""Minimal stdlib-only Windows ConPTY process wrapper.

The module is import-safe on non-Windows hosts. Win32 APIs are resolved only
when :class:`WindowsConPtyProcess` starts, keeping Linux packaging and tests
independent from ``msvcrt`` and ``kernel32``.
"""
from __future__ import annotations

import ctypes
from ctypes import wintypes
import os
from pathlib import Path
import subprocess
import sys
from typing import BinaryIO


_EXTENDED_STARTUPINFO_PRESENT = 0x00080000
_CREATE_UNICODE_ENVIRONMENT = 0x00000400
_PROC_THREAD_ATTRIBUTE_PSEUDOCONSOLE = 0x00020016
_STARTF_USESTDHANDLES = 0x00000100
_STILL_ACTIVE = 259
_WAIT_OBJECT_0 = 0
_WAIT_TIMEOUT = 258
_WAIT_FAILED = 0xFFFFFFFF
_INFINITE = 0xFFFFFFFF


class ConPtyUnavailable(RuntimeError):
    """Raised only when the Windows host does not expose the ConPTY API."""


class _COORD(ctypes.Structure):
    _fields_ = [("X", ctypes.c_short), ("Y", ctypes.c_short)]


class _STARTUPINFOW(ctypes.Structure):
    _fields_ = [
        ("cb", wintypes.DWORD),
        ("lpReserved", wintypes.LPWSTR),
        ("lpDesktop", wintypes.LPWSTR),
        ("lpTitle", wintypes.LPWSTR),
        ("dwX", wintypes.DWORD),
        ("dwY", wintypes.DWORD),
        ("dwXSize", wintypes.DWORD),
        ("dwYSize", wintypes.DWORD),
        ("dwXCountChars", wintypes.DWORD),
        ("dwYCountChars", wintypes.DWORD),
        ("dwFillAttribute", wintypes.DWORD),
        ("dwFlags", wintypes.DWORD),
        ("wShowWindow", wintypes.WORD),
        ("cbReserved2", wintypes.WORD),
        ("lpReserved2", ctypes.POINTER(wintypes.BYTE)),
        ("hStdInput", wintypes.HANDLE),
        ("hStdOutput", wintypes.HANDLE),
        ("hStdError", wintypes.HANDLE),
    ]


class _STARTUPINFOEXW(ctypes.Structure):
    _fields_ = [("StartupInfo", _STARTUPINFOW), ("lpAttributeList", ctypes.c_void_p)]


class _PROCESS_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("hProcess", wintypes.HANDLE),
        ("hThread", wintypes.HANDLE),
        ("dwProcessId", wintypes.DWORD),
        ("dwThreadId", wintypes.DWORD),
    ]


def _environment_block(env: dict[str, str]) -> ctypes.Array:
    entries = [f"{key}={value}" for key, value in env.items() if "\0" not in key and "\0" not in value]
    entries.sort(key=str.upper)
    # create_unicode_buffer appends its own terminator, yielding the required
    # double-NUL environment block from one explicit trailing NUL here.
    return ctypes.create_unicode_buffer("\0".join(entries) + "\0")


def _win_error(action: str) -> OSError:
    error = ctypes.get_last_error()
    detail = ctypes.FormatError(error).strip() if error else "unknown Win32 error"
    return OSError(error, f"{action}: {detail}")


def _check_hresult(result: int, action: str) -> None:
    if int(result) < 0:
        code = int(result) & 0xFFFFFFFF
        raise OSError(code, f"{action} failed with HRESULT 0x{code:08x}")


def _conpty_startup_info(attribute_pointer: ctypes.c_void_p) -> _STARTUPINFOEXW:
    startup = _STARTUPINFOEXW()
    startup.StartupInfo.cb = ctypes.sizeof(startup)
    # When the AHA process itself has redirected stdio (onebin/UI is a
    # common case), cmd.exe otherwise keeps those parent handles instead
    # of opening its ConPTY console handles. Explicit null std handles
    # force the console client bootstrap to bind all three to ConPTY.
    startup.StartupInfo.dwFlags = _STARTF_USESTDHANDLES
    startup.lpAttributeList = attribute_pointer
    return startup


class WindowsConPtyProcess:
    """Create and own one child process attached to a Windows pseudoconsole."""

    def __init__(
        self,
        argv: list[str],
        *,
        cwd: Path,
        env: dict[str, str],
        cols: int,
        rows: int,
    ) -> None:
        if sys.platform != "win32":
            raise ConPtyUnavailable("ConPTY is available only on Windows")
        self.argv = list(argv)
        self.pid = 0
        self.stdin: BinaryIO | None = None
        self.stdout: BinaryIO | None = None
        self._kernel32 = None
        self._process_handle: int | None = None
        self._pseudo_console: int | None = None
        self._closed = False
        self._start(cwd=cwd, env=env, cols=cols, rows=rows)

    def _start(self, *, cwd: Path, env: dict[str, str], cols: int, rows: int) -> None:
        import msvcrt

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        try:
            create_pseudo_console = kernel32.CreatePseudoConsole
            resize_pseudo_console = kernel32.ResizePseudoConsole
            close_pseudo_console = kernel32.ClosePseudoConsole
        except AttributeError as exc:
            raise ConPtyUnavailable("This Windows version does not provide ConPTY") from exc

        kernel32.CreatePipe.argtypes = [
            ctypes.POINTER(wintypes.HANDLE),
            ctypes.POINTER(wintypes.HANDLE),
            ctypes.c_void_p,
            wintypes.DWORD,
        ]
        kernel32.CreatePipe.restype = wintypes.BOOL
        create_pseudo_console.argtypes = [
            _COORD,
            wintypes.HANDLE,
            wintypes.HANDLE,
            wintypes.DWORD,
            ctypes.POINTER(ctypes.c_void_p),
        ]
        create_pseudo_console.restype = ctypes.c_long
        resize_pseudo_console.argtypes = [ctypes.c_void_p, _COORD]
        resize_pseudo_console.restype = ctypes.c_long
        close_pseudo_console.argtypes = [ctypes.c_void_p]
        close_pseudo_console.restype = None
        kernel32.InitializeProcThreadAttributeList.argtypes = [
            ctypes.c_void_p,
            wintypes.DWORD,
            wintypes.DWORD,
            ctypes.POINTER(ctypes.c_size_t),
        ]
        kernel32.InitializeProcThreadAttributeList.restype = wintypes.BOOL
        kernel32.UpdateProcThreadAttribute.argtypes = [
            ctypes.c_void_p,
            ctypes.c_size_t,
            ctypes.c_size_t,
            ctypes.c_void_p,
            ctypes.c_size_t,
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_size_t),
        ]
        kernel32.UpdateProcThreadAttribute.restype = wintypes.BOOL
        kernel32.DeleteProcThreadAttributeList.argtypes = [ctypes.c_void_p]
        kernel32.DeleteProcThreadAttributeList.restype = None
        kernel32.GetProcessHeap.argtypes = []
        kernel32.GetProcessHeap.restype = wintypes.HANDLE
        kernel32.HeapAlloc.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.c_size_t]
        kernel32.HeapAlloc.restype = ctypes.c_void_p
        kernel32.HeapFree.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.c_void_p]
        kernel32.HeapFree.restype = wintypes.BOOL
        kernel32.CreateProcessW.argtypes = [
            wintypes.LPCWSTR,
            wintypes.LPWSTR,
            ctypes.c_void_p,
            ctypes.c_void_p,
            wintypes.BOOL,
            wintypes.DWORD,
            ctypes.c_void_p,
            wintypes.LPCWSTR,
            ctypes.POINTER(_STARTUPINFOW),
            ctypes.POINTER(_PROCESS_INFORMATION),
        ]
        kernel32.CreateProcessW.restype = wintypes.BOOL
        kernel32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        kernel32.GetExitCodeProcess.restype = wintypes.BOOL
        kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        kernel32.WaitForSingleObject.restype = wintypes.DWORD
        kernel32.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
        kernel32.TerminateProcess.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL

        pseudo_input = wintypes.HANDLE()
        host_input = wintypes.HANDLE()
        host_output = wintypes.HANDLE()
        pseudo_output = wintypes.HANDLE()
        pseudo_console = ctypes.c_void_p()
        attribute_pointer: ctypes.c_void_p | None = None
        attribute_initialized = False
        process_heap = kernel32.GetProcessHeap()
        process_info = _PROCESS_INFORMATION()
        process_created = False
        input_fd: int | None = None
        output_fd: int | None = None
        try:
            if not kernel32.CreatePipe(ctypes.byref(pseudo_input), ctypes.byref(host_input), None, 0):
                raise _win_error("CreatePipe(ConPTY input)")
            if not kernel32.CreatePipe(ctypes.byref(host_output), ctypes.byref(pseudo_output), None, 0):
                raise _win_error("CreatePipe(ConPTY output)")
            result = create_pseudo_console(
                _COORD(max(20, min(int(cols), 240)), max(8, min(int(rows), 80))),
                pseudo_input,
                pseudo_output,
                0,
                ctypes.byref(pseudo_console),
            )
            _check_hresult(result, "CreatePseudoConsole")

            attribute_size = ctypes.c_size_t()
            kernel32.InitializeProcThreadAttributeList(None, 1, 0, ctypes.byref(attribute_size))
            attribute_pointer = kernel32.HeapAlloc(process_heap, 0, attribute_size.value)
            if not attribute_pointer:
                raise _win_error("HeapAlloc(PROC_THREAD_ATTRIBUTE_LIST)")
            if not kernel32.InitializeProcThreadAttributeList(
                attribute_pointer,
                1,
                0,
                ctypes.byref(attribute_size),
            ):
                raise _win_error("InitializeProcThreadAttributeList")
            attribute_initialized = True
            if not kernel32.UpdateProcThreadAttribute(
                attribute_pointer,
                0,
                _PROC_THREAD_ATTRIBUTE_PSEUDOCONSOLE,
                pseudo_console,
                ctypes.sizeof(pseudo_console),
                None,
                None,
            ):
                raise _win_error("UpdateProcThreadAttribute(ConPTY)")

            startup = _conpty_startup_info(attribute_pointer)
            command_line = ctypes.create_unicode_buffer(subprocess.list2cmdline(self.argv))
            environment = _environment_block(env)
            if not kernel32.CreateProcessW(
                None,
                command_line,
                None,
                None,
                False,
                _EXTENDED_STARTUPINFO_PRESENT | _CREATE_UNICODE_ENVIRONMENT,
                environment,
                str(cwd),
                ctypes.byref(startup.StartupInfo),
                ctypes.byref(process_info),
            ):
                raise _win_error("CreateProcessW(ConPTY)")
            process_created = True
            # Microsoft requires the pipe ends supplied to CreatePseudoConsole
            # to stay open until the attached child has been created.
            kernel32.CloseHandle(pseudo_input)
            pseudo_input = wintypes.HANDLE()
            kernel32.CloseHandle(pseudo_output)
            pseudo_output = wintypes.HANDLE()
            kernel32.CloseHandle(process_info.hThread)
            process_info.hThread = wintypes.HANDLE()

            binary_flag = int(getattr(os, "O_BINARY", 0))
            input_fd = msvcrt.open_osfhandle(int(host_input.value), os.O_WRONLY | binary_flag)
            host_input = wintypes.HANDLE()
            output_fd = msvcrt.open_osfhandle(int(host_output.value), os.O_RDONLY | binary_flag)
            host_output = wintypes.HANDLE()
            self.stdin = os.fdopen(input_fd, "wb", buffering=0)
            input_fd = None
            self.stdout = os.fdopen(output_fd, "rb", buffering=0)
            output_fd = None
            self._kernel32 = kernel32
            self._process_handle = int(process_info.hProcess)
            self._pseudo_console = int(pseudo_console.value)
            self.pid = int(process_info.dwProcessId)
            process_info.hProcess = wintypes.HANDLE()
            pseudo_console = ctypes.c_void_p()
        finally:
            if attribute_pointer is not None and attribute_initialized:
                kernel32.DeleteProcThreadAttributeList(attribute_pointer)
            if attribute_pointer is not None:
                kernel32.HeapFree(process_heap, 0, attribute_pointer)
            for handle in (pseudo_input, pseudo_output, host_input, host_output):
                if handle:
                    kernel32.CloseHandle(handle)
            if input_fd is not None:
                os.close(input_fd)
            if output_fd is not None:
                os.close(output_fd)
            if process_info.hThread:
                kernel32.CloseHandle(process_info.hThread)
            if process_info.hProcess:
                if process_created:
                    kernel32.TerminateProcess(process_info.hProcess, 1)
                kernel32.CloseHandle(process_info.hProcess)
            if pseudo_console:
                close_pseudo_console(pseudo_console)

    def poll(self) -> int | None:
        if self._process_handle is None or self._kernel32 is None:
            return 0
        code = wintypes.DWORD()
        if not self._kernel32.GetExitCodeProcess(self._process_handle, ctypes.byref(code)):
            raise _win_error("GetExitCodeProcess")
        return None if int(code.value) == _STILL_ACTIVE else int(code.value)

    def wait(self, timeout: float | None = None) -> int:
        if self._process_handle is None or self._kernel32 is None:
            return 0
        milliseconds = _INFINITE if timeout is None else max(0, min(int(float(timeout) * 1000), _INFINITE - 1))
        result = int(self._kernel32.WaitForSingleObject(self._process_handle, milliseconds))
        if result == _WAIT_TIMEOUT:
            raise subprocess.TimeoutExpired(self.argv, timeout)
        if result == _WAIT_FAILED:
            raise _win_error("WaitForSingleObject")
        if result != _WAIT_OBJECT_0:
            raise OSError(result, f"unexpected WaitForSingleObject result: {result}")
        return int(self.poll() or 0)

    def resize(self, *, cols: int, rows: int) -> None:
        if self._pseudo_console is None or self._kernel32 is None:
            return
        result = self._kernel32.ResizePseudoConsole(
            self._pseudo_console,
            _COORD(max(20, min(int(cols), 240)), max(8, min(int(rows), 80))),
        )
        _check_hresult(result, "ResizePseudoConsole")

    def terminate(self) -> None:
        if self.poll() is None and self._process_handle is not None and self._kernel32 is not None:
            if not self._kernel32.TerminateProcess(self._process_handle, 1):
                raise _win_error("TerminateProcess(ConPTY)")

    def kill(self) -> None:
        self.terminate()

    def close_pseudo_console(self) -> None:
        """Close the HPCON so the output reader receives EOF.

        The child must already be stopped. Closing the output stream first can
        block while another thread is inside ``ReadFile``; closing the
        pseudoconsole first lets that read finish naturally.
        """
        if self._pseudo_console is not None and self._kernel32 is not None:
            self._kernel32.ClosePseudoConsole(self._pseudo_console)
            self._pseudo_console = None

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self.stdin is not None:
            try:
                self.stdin.close()
            except OSError:
                pass
        self.stdin = None
        self.close_pseudo_console()
        if self.stdout is not None:
            try:
                self.stdout.close()
            except OSError:
                pass
        self.stdout = None
        if self._process_handle is not None and self._kernel32 is not None:
            self._kernel32.CloseHandle(self._process_handle)
            self._process_handle = None
