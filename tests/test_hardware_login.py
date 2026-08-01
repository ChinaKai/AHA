from __future__ import annotations

import unittest

from aha_cli.services.hardware_login import (
    AUTO_LOGIN_PASS_ID,
    AUTO_LOGIN_USER_ID,
    login_arm_commands,
)


class LoginArmCommandsTests(unittest.TestCase):
    def test_builds_user_and_pass_rules(self) -> None:
        commands = login_arm_commands({"username": "root", "password": "pw"})
        self.assertEqual(len(commands), 2)
        user, password = commands
        self.assertEqual(user["id"], AUTO_LOGIN_USER_ID)
        self.assertEqual(user["send"], "root\r")
        self.assertTrue(user["regex"])
        self.assertEqual(user["cmd"], "arm")
        self.assertEqual(password["id"], AUTO_LOGIN_PASS_ID)
        self.assertEqual(password["send"], "pw\r")

    def test_empty_when_no_credentials(self) -> None:
        self.assertEqual(login_arm_commands({}), [])
        self.assertEqual(login_arm_commands(None), [])

    def test_username_only(self) -> None:
        commands = login_arm_commands({"username": "admin"})
        self.assertEqual(len(commands), 1)
        self.assertEqual(commands[0]["id"], AUTO_LOGIN_USER_ID)

    def test_ids_stable_for_idempotent_rearm(self) -> None:
        # Fixed ids let re-arming replace instead of stacking duplicates.
        first = {c["id"] for c in login_arm_commands({"username": "u", "password": "p"})}
        second = {c["id"] for c in login_arm_commands({"username": "u", "password": "p"})}
        self.assertEqual(first, second)


if __name__ == "__main__":
    unittest.main()
