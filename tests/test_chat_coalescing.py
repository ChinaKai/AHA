from __future__ import annotations

import unittest

from aha_cli.services.chat_coalescing import MAX_MERGED_GROUP_MESSAGES, next_task_message_batch


def _group_message(index: int, *, chat_id: str = "oc-group", user: str = "user-a") -> tuple[dict, int]:
    return (
        {
            "ts": f"2026-08-06T03:00:{index:02d}+00:00",
            "task_id": "task-001",
            "target": "main",
            "sender": "feishu",
            "message": f"wrapped {index}",
            "reply_target": "feishu",
            "feishu_channel": "group_digital_human",
            "feishu_chat_id": chat_id,
            "feishu_session_key": f"tenant:feishu-group-user:{user}",
            "feishu_mention_open_id": user,
            "feishu_reply_to": f"om-{index}",
            "feishu_message_id": f"om-{index}",
            "feishu_original_text": f"message {index}",
        },
        index * 100,
    )


class ChatCoalescingTests(unittest.TestCase):
    def test_same_user_group_messages_merge_and_reply_to_latest(self) -> None:
        batch = next_task_message_batch([_group_message(1), _group_message(2), _group_message(3)], "task-001")

        self.assertIsNotNone(batch)
        item, item_offset, stats = batch or ({}, 0, {})
        self.assertEqual(item_offset, 300)
        self.assertEqual(stats["merged_count"], 3)
        self.assertEqual(item["feishu_reply_to"], "om-3")
        self.assertEqual(item["feishu_merged_count"], 3)
        self.assertLess(item["message"].index("message 1"), item["message"].index("message 3"))
        self.assertIn("只回复一次", item["message"])

    def test_different_group_starts_a_new_batch(self) -> None:
        records = [
            _group_message(1),
            _group_message(2, chat_id="oc-other"),
            _group_message(3),
        ]

        item, item_offset, stats = next_task_message_batch(records, "task-001") or ({}, 0, {})

        self.assertEqual(stats["merged_count"], 1)
        self.assertEqual(item_offset, 100)
        self.assertEqual(item["feishu_reply_to"], "om-1")

    def test_different_user_starts_a_new_batch(self) -> None:
        records = [
            _group_message(1),
            _group_message(2, user="user-b"),
            _group_message(3),
        ]

        item, item_offset, stats = next_task_message_batch(records, "task-001") or ({}, 0, {})

        self.assertEqual(stats["merged_count"], 1)
        self.assertEqual(item_offset, 100)
        self.assertEqual(item["feishu_reply_to"], "om-1")

    def test_non_group_message_is_never_merged(self) -> None:
        browser = ({"task_id": "task-001", "sender": "browser", "message": "manual"}, 50)

        item, item_offset, stats = next_task_message_batch([browser, _group_message(1)], "task-001") or ({}, 0, {})

        self.assertEqual(item["message"], "manual")
        self.assertEqual(item_offset, 50)
        self.assertEqual(stats["merged_count"], 1)

    def test_flood_protection_keeps_latest_messages(self) -> None:
        total = MAX_MERGED_GROUP_MESSAGES + 5
        records = [_group_message(index) for index in range(1, total + 1)]

        item, _item_offset, stats = next_task_message_batch(records, "task-001") or ({}, 0, {})

        self.assertEqual(stats["merged_count"], total)
        self.assertEqual(stats["omitted_count"], 5)
        self.assertNotIn("message 5\n", item["feishu_original_text"])
        self.assertIn("message 6", item["feishu_original_text"])
        self.assertIn(f"message {total}", item["feishu_original_text"])


if __name__ == "__main__":
    unittest.main()
