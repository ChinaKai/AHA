from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from aha_cli.services.browser_bookmarks import (
    browser_bookmarks_snapshot,
    update_browser_bookmarks,
)


class BrowserBookmarksTests(unittest.TestCase):
    def test_named_profile_bookmarks_are_shared_across_tasks(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = {"profile": "named", "profile_name": "Work"}
            added = update_browser_bookmarks(
                root,
                "run-a",
                "task-a",
                config,
                action="add",
                url="https://example.com/docs",
                title="Example Docs",
            )
            shared = browser_bookmarks_snapshot(root, "run-b", "task-b", config)

        self.assertTrue(added["added"])
        self.assertEqual(added["scope"]["kind"], "named")
        self.assertEqual(shared["scope"]["name"], "Work")
        self.assertEqual(
            shared["items"],
            added["items"],
        )

    def test_task_bookmarks_are_isolated_and_toggle_removes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = {"profile": "task"}
            added = update_browser_bookmarks(
                root,
                "run-a",
                "task-a",
                config,
                action="toggle",
                url="https://example.com/",
                title="  Example   Home ",
            )
            other = browser_bookmarks_snapshot(root, "run-a", "task-b", config)
            removed = update_browser_bookmarks(
                root,
                "run-a",
                "task-a",
                config,
                action="toggle",
                url="https://example.com/",
            )

        self.assertEqual(added["items"][0]["title"], "Example Home")
        self.assertEqual(other["items"], [])
        self.assertTrue(removed["removed"])
        self.assertEqual(removed["items"], [])

    def test_bookmarks_reject_non_http_urls_and_credentials(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = {"profile": "ephemeral"}
            for url in ("javascript:alert(1)", "https://user:secret@example.com/"):
                with self.subTest(url=url):
                    with self.assertRaises(ValueError):
                        update_browser_bookmarks(
                            root,
                            "run-a",
                            "task-a",
                            config,
                            action="add",
                            url=url,
                        )


if __name__ == "__main__":
    unittest.main()
