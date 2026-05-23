from __future__ import annotations

from pathlib import Path
import tempfile
import unittest
from unittest import mock

from aha_cli.services import weixin


class WeixinServiceTests(unittest.TestCase):
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

    def test_send_test_notification_posts_to_logged_in_user(self) -> None:
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
                payload = weixin.send_test_notification(root, "run-001", "hello")

        self.assertTrue(payload["sent"])
        self.assertEqual(payload["target"], "user-1@im.wechat")
        args = post_json.call_args.args
        self.assertEqual(args[:3], ("https://example.test", "ilink/bot/sendmessage", "mock-token"))
        self.assertEqual(args[3]["msg"]["to_user_id"], "user-1@im.wechat")
        self.assertEqual(args[3]["msg"]["item_list"][0]["text_item"]["text"], "hello")


if __name__ == "__main__":
    unittest.main()
