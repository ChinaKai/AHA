from __future__ import annotations

import unittest

from aha_cli.store.ws_target import (
    is_wsl_workspace,
    wsl_distro_and_path,
    windows_path_to_wsl,
    wsl_workspace_native_path,
    wsl_native_home,
    wsl_unc_from_native,
)


class WsTargetTests(unittest.TestCase):
    def test_is_wsl_workspace(self) -> None:
        self.assertTrue(is_wsl_workspace(r"\\wsl.localhost\Ubuntu-24.04\home\kaikai\proj"))
        self.assertTrue(is_wsl_workspace(r"\\wsl$\Ubuntu-24.04\home\kaikai\proj"))
        self.assertFalse(is_wsl_workspace(r"C:\Users\toope\proj"))
        self.assertFalse(is_wsl_workspace(""))

    def test_wsl_distro_and_path(self) -> None:
        distro, native = wsl_distro_and_path(r"\\wsl.localhost\Ubuntu-24.04\home\kaikai\proj")
        self.assertEqual(distro, "Ubuntu-24.04")
        self.assertEqual(native, "/home/kaikai/proj")

        distro, native = wsl_distro_and_path(r"\\wsl$\Ubuntu-24.04\home\kaikai")
        self.assertEqual(distro, "Ubuntu-24.04")
        self.assertEqual(native, "/home/kaikai")

    def test_wsl_distro_and_path_rejects_non_wsl(self) -> None:
        self.assertEqual(wsl_distro_and_path(r"C:\x"), (None, None))

    def test_windows_path_to_wsl(self) -> None:
        self.assertEqual(windows_path_to_wsl(r"C:\Users\toope\.aha"), "/mnt/c/Users/toope/.aha")
        self.assertEqual(windows_path_to_wsl(r"D:\tmp"), "/mnt/d/tmp")

    def test_windows_path_to_wsl_rejects_wsl_native(self) -> None:
        self.assertIsNone(windows_path_to_wsl("/home/kaikai/proj"))

    def test_wsl_workspace_native_path(self) -> None:
        self.assertEqual(
            wsl_workspace_native_path(r"\\wsl.localhost\Ubuntu-24.04\home\kaikai\proj"),
            "/home/kaikai/proj",
        )
        self.assertEqual(wsl_workspace_native_path(r"C:\Users\toope\.aha"), "/mnt/c/Users/toope/.aha")
        self.assertEqual(wsl_workspace_native_path("/home/kaikai/proj"), "/home/kaikai/proj")

    def test_wsl_native_home(self) -> None:
        self.assertEqual(wsl_native_home(r"C:\Users\toope\.aha"), "/mnt/c/Users/toope/.aha")

    def test_wsl_unc_from_native(self) -> None:
        self.assertEqual(
            wsl_unc_from_native("Ubuntu-24.04", "/home/kaikai/proj"),
            r"\\wsl.localhost\Ubuntu-24.04\home\kaikai\proj",
        )


if __name__ == "__main__":
    unittest.main()
