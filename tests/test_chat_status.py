from __future__ import annotations

import unittest

from aha_cli.services.chat import status_from_agent_result


class ChatStatusTests(unittest.TestCase):
    def test_agent_result_status_detects_blocked_reply(self) -> None:
        self.assertEqual(status_from_agent_result(0, "done"), "completed")
        self.assertEqual(status_from_agent_result(1, "done"), "failed")
        self.assertEqual(status_from_agent_result(0, "文件没有落盘，因为 read-only sandbox"), "blocked")
        self.assertEqual(status_from_agent_result(0, "当前沙箱是只读，写入被拦截"), "blocked")
        self.assertEqual(status_from_agent_result(0, "NAS mp4 写入失败，导致状态抖动"), "completed")
        self.assertEqual(status_from_agent_result(0, "不是 NAS 参数写入失败，配置已经生效"), "completed")
        self.assertEqual(status_from_agent_result(0, '`write_task_result()` 写入 `task["output_file"]`'), "completed")
        self.assertEqual(
            status_from_agent_result(
                0,
                '{"actions":[{"type":"record_task_update","summary":"host uses read-only sandbox"}],"response":"done"}',
            ),
            "completed",
        )
