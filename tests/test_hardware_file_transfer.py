from __future__ import annotations

import hashlib
import shlex
import subprocess
import tempfile
import unittest
from pathlib import Path

from aha_cli.domain.models import utc_now
from aha_cli.services.hardware_file_transfer import (
    HardwareFileTransferError,
    _RECEIVER_SCRIPT,
    encode_octal_block,
    receiver_script_bytes,
    send_file_via_shell,
)
from aha_cli.store.io import append_jsonl


class HardwareFileTransferTests(unittest.TestCase):
    def test_receiver_script_round_trips_all_byte_values(self) -> None:
        payload = bytes(range(256)) + b"aha\x00transfer\n"
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            script = root / "recv.sh"
            destination = root / "result.bin"
            script.write_text(_RECEIVER_SCRIPT, encoding="utf-8")
            digest = hashlib.sha256(payload).hexdigest()
            process = subprocess.Popen(
                ["sh", str(script), str(destination), str(len(payload)), digest, "test-token"],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            chunks = [payload[index:index + 128] for index in range(0, len(payload), 128)]
            wire = "".join(
                f"D{index}:{encode_octal_block(chunk)}\n"
                for index, chunk in enumerate(chunks)
            ) + "E\n"
            stdout, stderr = process.communicate(wire.encode("ascii"), timeout=10.0)

            self.assertEqual(process.returncode, 0, stderr.decode("utf-8", errors="replace"))
            self.assertEqual(destination.read_bytes(), payload)
            self.assertIn(b"AHA-RECV DONE test-token", stdout)
            self.assertLess(receiver_script_bytes(), 3000)

    def test_send_file_bootstraps_retries_and_verifies(self) -> None:
        payload = bytes(range(64)) * 5
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source.bin"
            source.write_bytes(payload)
            device = "/dev/ttyTEST0"
            stream_path = root / "network-stream.jsonl"
            received = bytearray()
            token = ""
            sends_by_sequence: dict[int, int] = {}

            def emit(data: str) -> None:
                append_jsonl(
                    stream_path,
                    {
                        "ts": utc_now(),
                        "device": device,
                        "direction": "rx",
                        "encoding": "text",
                        "data": data,
                        "source": "test-board",
                    },
                )

            def send_text(text: str) -> None:
                nonlocal token
                if text.startswith("if [ -x /tmp/.aha-recv-v1 ]"):
                    token = text.rsplit(" MISS ", 1)[1].split(";", 1)[0].strip("' ")
                    emit(f"AHA-MISS {token}\n")
                    return
                if "AHA-%s %s\\n' BOOT" in text:
                    token = text.rsplit(" BOOT ", 1)[1].splitlines()[0].strip("' ")
                    emit(f"AHA-BOOT {token}\n")
                    return
                if text.startswith("sh /tmp/.aha-recv-v1"):
                    token = shlex.split(text)[-1]
                    emit(f"AHA-RECV READY {token}\n")
                    return
                if text.startswith("D"):
                    header, encoded = text.rstrip("\n").split(":", 1)
                    sequence = int(header[1:])
                    sends_by_sequence[sequence] = sends_by_sequence.get(sequence, 0) + 1
                    if sequence == 0 and sends_by_sequence[sequence] == 1:
                        return
                    if sends_by_sequence[sequence] == 1 or sequence == 0:
                        received.extend(
                            int(encoded[index + 2:index + 5], 8)
                            for index in range(0, len(encoded), 5)
                        )
                    emit(f"AHA-RECV ACK {token} {sequence} {len(received)}\n")
                    return
                if text == "E\n":
                    digest = hashlib.sha256(received).hexdigest()
                    emit(f"AHA-RECV DONE {token} {len(received)} {digest}\n")

            result = send_file_via_shell(
                root,
                device,
                source,
                "/tmp/received.bin",
                send_text=send_text,
                stream_path=stream_path,
                chunk_size=128,
                timeout=0.05,
                retries=2,
            )

            self.assertEqual(bytes(received), payload)
            self.assertEqual(result.size, len(payload))
            self.assertEqual(result.retries, 1)
            self.assertEqual(result.sha256, hashlib.sha256(payload).hexdigest())

    def test_rejects_oversized_chunk(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "source.bin"
            source.write_bytes(b"data")
            with self.assertRaisesRegex(HardwareFileTransferError, "chunk size"):
                send_file_via_shell(
                    Path(tmp),
                    "/dev/ttyTEST0",
                    source,
                    "/tmp/result.bin",
                    send_text=lambda _text: None,
                    chunk_size=513,
                )


if __name__ == "__main__":
    unittest.main()
