from __future__ import annotations

import base64
import json
import os
from pathlib import Path
import socket
import tempfile
import threading
import time
import unittest
from unittest import mock

from aha_cli.services.network_terminal import (
    NetworkTerminalDaemon,
    TelnetCodec,
    append_network_control,
    network_credentials_path,
    network_status,
    network_state_path,
    network_stream_page,
    network_terminal_socket_path,
    stop_all_network_terminals,
    task_network_target,
)
from aha_cli.store.io import iter_jsonl_from


def wait_until(predicate, timeout: float = 3.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return False


class NetworkTerminalTests(unittest.TestCase):
    def test_stop_all_network_terminals_forces_stale_process_and_normalizes_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            host, port = "192.0.2.10", 23
            state_path = network_state_path(root, host, port)
            state_path.parent.mkdir(parents=True)
            state_path.write_text(
                json.dumps(
                    {
                        "host": host,
                        "port": port,
                        "pid": 4343,
                        "pid_platform": "posix",
                        "status": "running",
                        "owner_instance": "web-instance",
                        "transfer": {"id": "transfer-2"},
                    }
                ),
                encoding="utf-8",
            )
            socket_path = network_terminal_socket_path(root, host, port)
            socket_path.touch()
            alive = {"value": True}

            def terminate(_pid: int, *, timeout: float) -> bool:
                alive["value"] = False
                return True

            with mock.patch.dict(os.environ, {"AHA_WEB_INSTANCE_ID": "web-instance"}), mock.patch(
                "aha_cli.services.network_terminal.pid_alive", side_effect=lambda _pid: alive["value"]
            ), mock.patch(
                "aha_cli.services.network_terminal.process_control.terminate_process", side_effect=terminate
            ) as terminate_process:
                result = stop_all_network_terminals(root, timeout=0.0)

            state = json.loads(state_path.read_text(encoding="utf-8"))
            controls, _ = iter_jsonl_from(state_path.parent / "control.jsonl", 0)

        self.assertEqual(result, {"found": 1, "stopped": 1, "forced": 1, "remaining": []})
        self.assertEqual(controls[-1]["cmd"], "stop")
        self.assertEqual(state["status"], "stopped")
        self.assertEqual(state["stop_reason"], "web-shutdown")
        self.assertNotIn("transfer", state)
        self.assertFalse(socket_path.exists())
        terminate_process.assert_called_once_with(4343, timeout=1.0)

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
        self.assertEqual(
            task_network_target(
                {
                    "hardware_debug": {
                        "mode": "both",
                        "network": {"device_ip": "192.168.1.21"},
                    }
                }
            ),
            ("192.168.1.21", 23, "", ""),
        )
        self.assertIsNone(task_network_target({"hardware_debug": {"mode": "off"}}))

    def test_telnet_codec_removes_negotiation_and_replies(self) -> None:
        codec = TelnetCodec()
        payload, reply = codec.feed(bytes((255, 251, 1)) + b"login: ")
        self.assertEqual(payload, b"login: ")
        self.assertEqual(reply, bytes((255, 253, 1)))

    def test_telnet_codec_negotiates_binary_both_directions(self) -> None:
        codec = TelnetCodec()
        self.assertEqual(
            codec.initial_negotiation(),
            bytes((255, 251, TelnetCodec.BINARY, 255, 253, TelnetCodec.BINARY)),
        )
        payload, reply = codec.feed(
            bytes((255, 253, TelnetCodec.BINARY, 255, 251, TelnetCodec.BINARY))
        )
        self.assertEqual(payload, b"")
        self.assertTrue(codec.binary_ready)
        self.assertIn(bytes((255, 251, TelnetCodec.BINARY)), reply)
        self.assertIn(bytes((255, 253, TelnetCodec.BINARY)), reply)

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
                    initial = bytearray()
                    while len(initial) < 6:
                        initial.extend(conn.recv(6 - len(initial)))
                    conn.sendall(bytes((255, 253, TelnetCodec.BINARY, 255, 251, TelnetCodec.BINARY)))
                    reply = bytearray()
                    while len(reply) < 6:
                        reply.extend(conn.recv(6 - len(reply)))
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

    def test_file_transfer_lease_blocks_interactive_network_tx(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            listener = socket.socket()
            listener.bind(("127.0.0.1", 0))
            listener.listen(1)
            host, port = listener.getsockname()
            received = bytearray()
            server_done = threading.Event()

            def serve() -> None:
                conn, _address = listener.accept()
                with conn:
                    initial = bytearray()
                    while len(initial) < 6:
                        initial.extend(conn.recv(6 - len(initial)))
                    conn.sendall(bytes((255, 253, TelnetCodec.BINARY, 255, 251, TelnetCodec.BINARY)))
                    reply = bytearray()
                    while len(reply) < 6:
                        reply.extend(conn.recv(6 - len(reply)))
                    conn.settimeout(0.05)
                    while not server_done.is_set():
                        try:
                            chunk = conn.recv(4096)
                        except socket.timeout:
                            continue
                        if not chunk:
                            break
                        received.extend(chunk)

            server_thread = threading.Thread(target=serve, daemon=True)
            server_thread.start()
            daemon = NetworkTerminalDaemon(root, host, port, self_reap=False, poll_interval=0.01)
            daemon_thread = threading.Thread(target=daemon.run, daemon=True)
            daemon_thread.start()
            ipc_client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            try:
                self.assertTrue(wait_until(lambda: network_status(root, host, port).get("status") == "running"))
                ipc_path = network_terminal_socket_path(root, host, port)
                self.assertTrue(wait_until(ipc_path.exists))
                ipc_client.connect(str(ipc_path))

                append_network_control(root, host, port, {"cmd": "transfer_begin", "transfer_id": "transfer-1"})
                self.assertTrue(
                    wait_until(lambda: (network_status(root, host, port).get("transfer") or {}).get("id") == "transfer-1")
                )
                self.assertTrue(wait_until(lambda: network_status(root, host, port).get("telnet_binary") is True))
                self.assertIn("network-transfer-v2", network_status(root, host, port).get("capabilities") or [])
                append_network_control(root, host, port, {"cmd": "send_raw", "data": "interactive-control"})
                append_network_control(
                    root,
                    host,
                    port,
                    {"cmd": "transfer_send", "transfer_id": "transfer-1", "data": "file-data"},
                )
                binary_payload = b"\x00\xffaha-binary\n"
                append_network_control(
                    root,
                    host,
                    port,
                    {
                        "cmd": "transfer_send_bytes",
                        "transfer_id": "transfer-1",
                        "data": base64.b64encode(binary_payload).decode("ascii"),
                    },
                )
                ipc_client.sendall(b'{"type":"input","data":"interactive-web"}\n')
                self.assertTrue(wait_until(lambda: TelnetCodec.encode(binary_payload) in received))

                append_network_control(root, host, port, {"cmd": "transfer_end", "transfer_id": "transfer-1"})
                self.assertTrue(wait_until(lambda: not network_status(root, host, port).get("transfer")))
                append_network_control(root, host, port, {"cmd": "send_raw", "data": "after-transfer"})
                self.assertTrue(wait_until(lambda: b"after-transfer" in received))
            finally:
                ipc_client.close()
                append_network_control(root, host, port, {"cmd": "stop"})
                daemon_thread.join(timeout=2)
                server_done.set()
                listener.close()

            self.assertNotIn(b"interactive-control", received)
            self.assertNotIn(b"interactive-web", received)
            self.assertIn(b"file-data", received)
            self.assertIn(TelnetCodec.encode(binary_payload), received)


if __name__ == "__main__":
    unittest.main()
