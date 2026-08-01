from __future__ import annotations

import ctypes
import sys
import unittest

from aha_cli.windows_conpty import (
    ConPtyUnavailable,
    WindowsConPtyProcess,
    _STARTF_USESTDHANDLES,
    _conpty_startup_info,
    _environment_block,
)


class WindowsConPtyTests(unittest.TestCase):
    def test_environment_block_is_sorted_and_double_null_terminated(self) -> None:
        block = _environment_block({"z_key": "last", "A_KEY": "first"})
        text = ctypes.wstring_at(ctypes.addressof(block), len(block))

        self.assertEqual(text, "A_KEY=first\0z_key=last\0\0")

    def test_startup_info_forces_console_client_to_open_conpty_stdio(self) -> None:
        pointer = ctypes.c_void_p(1234)
        startup = _conpty_startup_info(pointer)

        self.assertEqual(startup.StartupInfo.dwFlags & _STARTF_USESTDHANDLES, _STARTF_USESTDHANDLES)
        self.assertFalse(startup.StartupInfo.hStdInput)
        self.assertFalse(startup.StartupInfo.hStdOutput)
        self.assertFalse(startup.StartupInfo.hStdError)
        self.assertEqual(startup.lpAttributeList, pointer.value)

    @unittest.skipIf(sys.platform == "win32", "non-Windows import-safety check")
    def test_process_reports_conpty_unavailable_off_windows(self) -> None:
        with self.assertRaises(ConPtyUnavailable):
            WindowsConPtyProcess([], cwd=None, env={}, cols=80, rows=24)  # type: ignore[arg-type]

