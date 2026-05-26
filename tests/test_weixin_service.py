from __future__ import annotations

from pathlib import Path
import re
import tempfile
import unittest
from unittest import mock

from aha_cli.services import weixin


class WeixinServiceTests(unittest.TestCase):
    def qr_matrix(self, payload: str) -> list[list[bool]]:
        svg = weixin._qr_svg(payload)
        view_box = re.search(r'viewBox="0 0 (\d+) \d+"', svg)
        self.assertIsNotNone(view_box)
        size = int(view_box.group(1)) - 8
        matrix = [[False] * size for _ in range(size)]
        for match in re.finditer(r'<rect x="(\d+)" y="(\d+)" width="(\d+)" height="1"', svg):
            col = int(match.group(1)) - 4
            row = int(match.group(2)) - 4
            width = int(match.group(3))
            if row < 0 or col < 0:
                continue
            for offset in range(width):
                if 0 <= row < size and 0 <= col + offset < size:
                    matrix[row][col + offset] = True
        return matrix

    def test_qr_svg_places_format_bits_in_spec_positions(self) -> None:
        matrix = self.qr_matrix("hello")
        size = len(matrix)
        format_bits = 0b111011111000100
        positions = (
            [(i, 8, i) for i in range(6)]
            + [(7, 8, 6), (8, 8, 7), (8, 7, 8)]
            + [(8, 14 - i, i) for i in range(9, 15)]
            + [(8, size - 1 - i, i) for i in range(8)]
            + [(size - 15 + i, 8, i) for i in range(8, 15)]
        )
        for row, col, bit_index in positions:
            self.assertEqual(matrix[row][col], bool((format_bits >> bit_index) & 1), (row, col, bit_index))
        self.assertTrue(matrix[13][8])

    def test_start_pairing_stores_qr_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch(
                "aha_cli.services.weixin._api_get_json",
                return_value={"qrcode": "qr-1", "qrcode_img_content": "https://example.test/login"},
            ):
                payload = weixin.start_pairing(root, "run-001")

        self.assertFalse(payload["paired"])
        self.assertEqual(payload["pairing"]["status"], "waiting")
        self.assertIn("<svg", payload["pairing"]["qrcode_svg"])
        self.assertIn("<rect", payload["pairing"]["qrcode_svg"])
        self.assertEqual(payload["pairing"]["qrcode_payload"], "https://example.test/login")

    def test_status_poll_confirmed_saves_account_without_exposing_token(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            weixin._write_secret_json(
                weixin.pairing_path(root),
                {
                    "status": "waiting",
                    "qrcode": "qr-1",
                    "qrcode_payload": "https://example.test/login",
                    "qrcode_svg": "<svg/>",
                    "base_url": weixin.DEFAULT_BASE_URL,
                    "expires_epoch": 9999999999,
                },
            )
            with mock.patch(
                "aha_cli.services.weixin._api_get_json",
                return_value={
                    "status": "confirmed",
                    "ilink_bot_id": "bot-1",
                    "bot_token": "mock-token",
                    "ilink_user_id": "user-1@im.wechat",
                },
            ):
                payload = weixin.status_snapshot(root, "run-001")
            saved_account = weixin.load_account(root)

        self.assertTrue(payload["paired"])
        self.assertEqual(payload["account"]["account_id"], "bot-1")
        self.assertEqual(payload["account"]["user_id"], "user-1@im.wechat")
        self.assertNotIn("token", payload["account"])
        self.assertEqual(saved_account["token"], "mock-token")

    def test_status_poll_timeout_keeps_waiting_without_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            weixin._write_secret_json(
                weixin.pairing_path(root),
                {
                    "status": "waiting",
                    "qrcode": "qr-1",
                    "qrcode_payload": "https://example.test/login",
                    "qrcode_svg": "<svg/>",
                    "base_url": weixin.DEFAULT_BASE_URL,
                    "expires_epoch": 9999999999,
                },
            )
            with mock.patch("aha_cli.services.weixin._api_get_json", side_effect=weixin.WeixinError("微信接口请求失败: timed out")):
                payload = weixin.status_snapshot(root, "run-001")

        self.assertFalse(payload["paired"])
        self.assertEqual(payload["pairing"]["status"], "waiting")
        self.assertEqual(payload["error"], "")

    def test_reset_pairing_clears_saved_weixin_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            weixin._write_secret_json(
                weixin.pairing_path(root),
                {
                    "status": "waiting",
                    "qrcode": "qr-1",
                    "qrcode_payload": "https://example.test/login",
                },
            )
            weixin.save_account(
                root,
                {
                    "account_id": "bot-1",
                    "token": "mock-token",
                    "base_url": "https://example.test",
                    "user_id": "user-1@im.wechat",
                },
            )
            weixin._write_secret_json(weixin.updates_path(root), {"get_updates_buf": "buf-1"})
            weixin._write_secret_json(weixin.contexts_path(root), {"user-1@im.wechat": "ctx-1"})
            weixin._write_secret_json(weixin.context_state_path(root), {"user-1@im.wechat": {"updated_at": "2026-05-26T00:00:00+00:00"}})

            payload = weixin.reset_pairing(root, "run-001")

            self.assertFalse(weixin.pairing_path(root).exists())
            self.assertFalse(weixin.account_path(root).exists())
            self.assertFalse(weixin.updates_path(root).exists())
            self.assertFalse(weixin.contexts_path(root).exists())
            self.assertFalse(weixin.context_state_path(root).exists())
            self.assertFalse(payload["paired"])
            self.assertIsNone(payload["account"])
            self.assertIsNone(payload["pairing"])

    def test_send_test_notification_requires_context_token(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            weixin.save_account(
                root,
                {
                    "account_id": "bot-1",
                    "token": "mock-token",
                    "base_url": "https://example.test",
                    "user_id": "user-1@im.wechat",
                },
            )
            with mock.patch("aha_cli.services.weixin._api_post_json", return_value={"msgs": [], "get_updates_buf": "buf-1"}) as post_json:
                with self.assertRaisesRegex(weixin.WeixinError, "context_token"):
                    weixin.send_test_notification(root, "run-001", "hello")
            saved_updates = weixin._read_json(weixin.updates_path(root))

        self.assertEqual(post_json.call_count, 1)
        self.assertEqual(post_json.call_args.args[:3], ("https://example.test", "ilink/bot/getupdates", "mock-token"))
        self.assertEqual(saved_updates["get_updates_buf"], "buf-1")

    def test_fetch_updates_saves_context_token(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            weixin.save_account(
                root,
                {
                    "account_id": "bot-1",
                    "token": "mock-token",
                    "base_url": "https://example.test",
                    "user_id": "user-1@im.wechat",
                },
            )
            with mock.patch(
                "aha_cli.services.weixin._api_post_json",
                return_value={
                    "get_updates_buf": "buf-2",
                    "msgs": [
                        {
                            "from_user_id": "user-1@im.wechat",
                            "context_token": "ctx-1",
                            "item_list": [{"type": 1, "text_item": {"text": "测试消息"}}],
                        }
                    ],
                },
            ) as post_json:
                payload = weixin.fetch_updates(root)
            saved_updates = weixin._read_json(weixin.updates_path(root))
            saved_context_token = weixin.load_context_token(root, "user-1@im.wechat")
            send_context = weixin.send_context_snapshot(root, "user-1@im.wechat")

        self.assertEqual(payload["message_count"], 1)
        self.assertEqual(post_json.call_args.args[3], {"get_updates_buf": ""})
        self.assertEqual(saved_updates["get_updates_buf"], "buf-2")
        self.assertEqual(saved_context_token, "ctx-1")
        self.assertEqual(send_context["state"], "fresh")
        self.assertTrue(send_context["fresh"])

    def test_notify_channel_start_and_stop_call_upstream(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            weixin.save_account(
                root,
                {
                    "account_id": "bot-1",
                    "token": "mock-token",
                    "base_url": "https://example.test",
                    "user_id": "user-1@im.wechat",
                },
            )
            with mock.patch("aha_cli.services.weixin._api_post_json", return_value={}) as post_json:
                start = weixin.notify_channel_start(root)
                stop = weixin.notify_channel_stop(root)

        self.assertTrue(start["ok"])
        self.assertTrue(stop["ok"])
        self.assertEqual(post_json.call_args_list[0].args[:3], ("https://example.test", "ilink/bot/msg/notifystart", "mock-token"))
        self.assertEqual(post_json.call_args_list[1].args[:3], ("https://example.test", "ilink/bot/msg/notifystop", "mock-token"))

    def test_fetch_updates_stores_recent_received_messages(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            weixin.save_account(
                root,
                {
                    "account_id": "bot-1",
                    "token": "mock-token",
                    "base_url": "https://example.test",
                    "user_id": "user-1@im.wechat",
                },
            )
            with mock.patch(
                "aha_cli.services.weixin._api_post_json",
                return_value={
                    "get_updates_buf": "buf-4",
                    "msgs": [
                        {"msg_id": "msg-1", "from_user_id": "user-1@im.wechat", "item_list": [{"type": 1, "text_item": {"text": "one"}}]},
                        {"msg_id": "msg-2", "from_user_id": "user-1@im.wechat", "item_list": [{"type": 1, "text_item": {"text": "two"}}]},
                        {"msg_id": "msg-3", "from_user_id": "user-1@im.wechat", "item_list": [{"type": 1, "text_item": {"text": "three"}}]},
                        {"msg_id": "msg-4", "from_user_id": "user-1@im.wechat", "item_list": [{"type": 1, "text_item": {"text": "four"}}]},
                    ],
                },
            ):
                payload = weixin.fetch_updates(root)
            saved_updates = weixin._read_json(weixin.updates_path(root))
            status = weixin.status_snapshot(root, "run-001", poll=False)

        self.assertEqual(payload["message_count"], 4)
        self.assertEqual([item["text"] for item in payload["recent_messages"]], ["four", "three", "two"])
        self.assertEqual([item["text"] for item in saved_updates["recent_messages"]], ["two", "three", "four"])
        self.assertEqual([item["text"] for item in status["received_messages"]], ["four", "three", "two"])

    def test_send_test_notification_includes_saved_context_token(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            weixin.save_account(
                root,
                {
                    "account_id": "bot-1",
                    "token": "mock-token",
                    "base_url": "https://example.test",
                    "user_id": "user-1@im.wechat",
                },
            )
            with mock.patch(
                "aha_cli.services.weixin._api_post_json",
                side_effect=[
                    {
                        "get_updates_buf": "buf-3",
                        "msgs": [{"from_user_id": "user-1@im.wechat", "context_token": "ctx-2"}],
                    },
                    {},
                ],
            ) as post_json:
                weixin.send_test_notification(root, "run-001", "hello")

        send_args = post_json.call_args_list[1].args
        self.assertEqual(send_args[1], "ilink/bot/sendmessage")
        self.assertEqual(send_args[3]["msg"]["context_token"], "ctx-2")

    def test_send_test_notification_rejects_stale_context_token(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            weixin.save_account(
                root,
                {
                    "account_id": "bot-1",
                    "token": "mock-token",
                    "base_url": "https://example.test",
                    "user_id": "user-1@im.wechat",
                },
            )
            weixin._write_secret_json(weixin.contexts_path(root), {"user-1@im.wechat": "ctx-old"})
            weixin._write_secret_json(weixin.context_state_path(root), {"user-1@im.wechat": {"updated_at": "2026-05-26T00:00:00+00:00"}})
            with (
                mock.patch("aha_cli.services.weixin.utc_now", return_value="2026-05-26T00:31:00+00:00"),
                mock.patch("aha_cli.services.weixin._api_post_json", return_value={"msgs": [], "get_updates_buf": "buf-1"}) as post_json,
            ):
                with self.assertRaisesRegex(weixin.WeixinError, "超过 30 分钟"):
                    weixin.send_test_notification(root, "run-001", "hello")

        self.assertEqual(post_json.call_count, 1)

    def test_send_test_notification_reports_api_error_body(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            weixin.save_account(
                root,
                {
                    "account_id": "bot-1",
                    "token": "mock-token",
                    "base_url": "https://example.test",
                    "user_id": "user-1@im.wechat",
                },
            )
            with mock.patch(
                "aha_cli.services.weixin._api_post_json",
                side_effect=[
                    {
                        "get_updates_buf": "buf-3",
                        "msgs": [{"from_user_id": "user-1@im.wechat", "context_token": "ctx-2"}],
                    },
                    {"ret": -14, "errmsg": "session timeout"},
                ],
            ):
                with self.assertRaisesRegex(weixin.WeixinError, "ret=-14"):
                    weixin.send_test_notification(root, "run-001", "hello")


if __name__ == "__main__":
    unittest.main()
