from __future__ import annotations

import json
from pathlib import Path
import stat
import tempfile
import unittest

from aha_cli.services.feishu_audit import audit_feishu_channel, feishu_audit_path


class FeishuAuditTests(unittest.TestCase):
    def test_audit_log_is_private_hashed_and_redacted(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            written = audit_feishu_channel(
                root,
                direction="inbound",
                kind="message",
                status="accepted",
                transport="channel_ws",
                message_id="om-123",
                chat_id="oc-secret-chat",
                open_id="ou-secret-user",
                session_key="tenant:p2p:ou-secret-user",
                run_id="run-001",
                task_id="task-006",
                content="请处理 Authorization: Bearer top-secret access_token=hidden",
            )
            path = feishu_audit_path(root)
            record = json.loads(path.read_text(encoding="utf-8").splitlines()[0])
            mode = stat.S_IMODE(path.stat().st_mode)

        self.assertTrue(written)
        self.assertEqual(mode, 0o600)
        self.assertEqual(record["message_id"], "om-123")
        self.assertEqual(record["run_id"], "run-001")
        self.assertEqual(len(record["chat_hash"]), 16)
        self.assertEqual(len(record["open_id_hash"]), 16)
        serialized = json.dumps(record, ensure_ascii=False)
        self.assertNotIn("oc-secret-chat", serialized)
        self.assertNotIn("ou-secret-user", serialized)
        self.assertNotIn("top-secret", serialized)
        self.assertNotIn("hidden", serialized)
        self.assertIn("[REDACTED]", serialized)

    def test_card_audit_keeps_title_summary_without_raw_action_body(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            card = {
                "schema": "2.0",
                "header": {"title": {"tag": "plain_text", "content": "请确认 AHA 操作"}},
                "body": {"elements": [{"token": "must-not-be-written", "action": {"danger": True}}]},
            }
            audit_feishu_channel(
                root,
                direction="outbound",
                kind="card",
                status="sent",
                transport="channel_ws",
                content={"card": card},
            )
            record = json.loads(feishu_audit_path(root).read_text(encoding="utf-8").splitlines()[0])

        self.assertEqual(record["content_summary"], "card: 请确认 AHA 操作")
        self.assertNotIn("must-not-be-written", json.dumps(record, ensure_ascii=False))


if __name__ == "__main__":
    unittest.main()
