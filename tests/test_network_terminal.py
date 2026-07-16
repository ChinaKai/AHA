from __future__ import annotations

import json
from pathlib import Path
import socket
import tempfile
import threading
import time
import unittest

from aha_cli.services.network_terminal import (
    NetworkTerminalDaemon,
    TelnetCodec,
    append_network_control,
    network_credentials_path,
    network_stream_page,
    network_terminal_socket_path,
    task_network_target,
)


def wait_until(predicate, timeout: float = 3.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return False


class NetworkTerminalTests(unittest.TestCase):
    def test_task_network_target_uses_v2_network_and_credentials(self) -> None:
        target = task_network_target(
            {
                "hardware_debug": {
                    "mode": "network",
                    "network": {"device_ip": "192.168.1.20"},
                    "credentials": {"username": "root", "password": "secret"},
                }
            }
        )
        self.assertEqual(target, ("192.168.1.20", 23, "root", "secret"))
        self.assertIsNone(task_network_target({"hardware_debug": {"mode": "off"}}))

    def test_telnet_codec_removes_negotiation_and_replies(self) -> None:
        codec = TelnetCodec()
        payload, reply = codec.feed(bytes((255, 251, 1)) + b"login: ")
        self.assertEqual(payload, b"login: ")
        self.assertEqual(reply, bytes((255, 253, 1)))

    def test_telnet_codec_negotiates_xterm_type_and_window_size(self) -> None:
        codec = TelnetCodec(cols=120, rows=40)
        payload, reply = codec.feed(
            bytes((255, 253, TelnetCodec.TTYPE, 255, 253, TelnetCodec.NAWS))
            + bytes((255, 250, TelnetCodec.TTYPE, TelnetCodec.SEND, 255, 240))
        )
        self.assertEqual(payload, b"")
        self.assertIn(bytes((255, 251, TelnetCodec.TTYPE)), reply)
        self.assertIn(bytes((255, 251, TelnetCodec.NAWS)), reply)
        self.assertIn(b"xterm-256color", reply)
        self.assertIn(bytes((0, 120, 0, 40)), reply)
        self.assertIn(bytes((0, 80, 0, 24)), codec.resize(80, 24))

    def test_daemon_auto_logs_in_and_supports_manual_terminal_input(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            listener = socket.socket()
            listener.bind(("127.0.0.1", 0))
            listener.listen(1)
            host, port = listener.getsockname()
            credentials = network_credentials_path(root, host, port)
            credentials.parent.mkdir(parents=True, exist_ok=True)
            credentials.write_text(json.dumps({"username": "root", "password": "secret"}), encoding="utf-8")
            credentials.chmod(0o600)
            received: list[str] = []

            def read_line(conn: socket.socket) -> str:
                data = bytearray()
                while not data.endswith(b"\r"):
                    chunk = conn.recv(128)
                    if not chunk:
                        break
                    data.extend(chunk)
                return data.decode("utf-8", "replace")

            def serve() -> None:
                conn, _address = listener.accept()
                with conn:
                    conn.sendall(b"board login: ")
                    received.append(read_line(conn))
                    conn.sendall(b"Password: ")
                    received.append(read_line(conn))
                    conn.sendall(b"\r\n# ")
                    command = read_line(conn)
                    received.append(command)
                    conn.sendall(b"echo ok\r\nok\r\n# ")

            server_thread = threading.Thread(target=serve, daemon=True)
            server_thread.start()
            daemon = NetworkTerminalDaemon(root, host, port, self_reap=False, poll_interval=0.01)
            daemon_thread = threading.Thread(target=daemon.run, daemon=True)
            daemon_thread.start()
            ipc_client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            try:
                self.assertTrue(wait_until(lambda: len(received) >= 2))
                ipc_path = network_terminal_socket_path(root, host, port)
                self.assertTrue(wait_until(ipc_path.exists))
                ipc_client.connect(str(ipc_path))
                ipc_client.sendall(b'{"type":"input","data":"echo ok\\r"}\n')
                self.assertTrue(wait_until(lambda: len(received) >= 3))
                self.assertTrue(
                    wait_until(
                        lambda: any("ok" in str(item.get("data") or "") for item in network_stream_page(root, host, port)["events"])
                    )
                )
            finally:
                ipc_client.close()
                append_network_control(root, host, port, {"cmd": "stop"})
                daemon_thread.join(timeout=2)
                listener.close()

            self.assertEqual(received[0], "root\r")
            self.assertEqual(received[1], "secret\r")
            self.assertEqual(received[2], "echo ok\r")
            stream_text = "\n".join(str(item.get("data") or "") for item in network_stream_page(root, host, port)["events"])
            self.assertNotIn("secret", stream_text)
            self.assertIn("password submitted", stream_text)


if __name__ == "__main__":
    unittest.main()
