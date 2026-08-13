from __future__ import annotations

import unittest

from aha_cli.services.chat_coalescing import MAX_MERGED_GROUP_MESSAGES, _is_backend_switch_handoff, next_task_message_batch


def _backend_switch_handoff(index: int = 1) -> tuple[dict, int]:
    return (
        {
            "ts": f"2026-08-13T04:00:{index:02d}+00:00",
            "task_id": "task-001",
            "target": "main",
            "sender": "aha",
            "from_agent": "aha",
            "to_agent": "main",
            "coordination": "backend_switch",
            "message": "AHA backend handoff.\n- agent: main\n- previous backend: codex\n- new backend: claude",
        },
        index * 100,
    )


def _browser_message(index: int = 2) -> tuple[dict, int]:
    return (
        {
            "ts": f"2026-08-13T04:00:{index:02d}+00:00",
            "task_id": "task-001",
            "target": "main",
            "sender": "browser",
            "message": f"continue {index}",
        },
        index * 100,
    )


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

    def test_backend_switch_handoff_merges_with_next_browser_message(self) -> None:
        records = [_backend_switch_handoff(1), _browser_message(2)]

        item, item_offset, stats = next_task_message_batch(records, "task-001") or ({}, 0, {})

        self.assertEqual(stats["merged_count"], 2)
        self.assertTrue(stats["handoff_merged"])
        self.assertEqual(item_offset, 200)
        self.assertEqual(item["message"].index("AHA backend handoff."), 0)
        self.assertLess(item["message"].index("AHA backend handoff."), item["message"].index("continue 2"))
        self.assertEqual(item["feishu_merged_count"], 2)

    def test_backend_switch_handoff_alone_is_not_consumed(self) -> None:
        records = [_backend_switch_handoff(1)]

        item, _item_offset, stats = next_task_message_batch(records, "task-001") or ({}, 0, {})

        # Lone handoff: no real message follows, so it is returned as-is (the
        # worker may process it or wait; the merge only happens with a follow-up).
        self.assertEqual(stats["merged_count"], 1)
        self.assertEqual(item["message"], _backend_switch_handoff(1)[0]["message"])

    def test_backend_switch_handoff_merges_through_multiple_handoffs(self) -> None:
        records = [_backend_switch_handoff(1), _backend_switch_handoff(2), _browser_message(3)]

        item, item_offset, stats = next_task_message_batch(records, "task-001") or ({}, 0, {})

        self.assertEqual(stats["merged_count"], 2)
        self.assertEqual(item_offset, 300)
        self.assertEqual(item["message"].count("AHA backend handoff."), 1)
        self.assertIn("continue 3", item["message"])

    def test_backend_switch_handoff_does_not_merge_with_non_task_message(self) -> None:
        records = [_backend_switch_handoff(1), _group_message(2)]

        item, item_offset, stats = next_task_message_batch(records, "task-001") or ({}, 0, {})

        self.assertEqual(stats["merged_count"], 1)
        self.assertEqual(item_offset, 100)

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
