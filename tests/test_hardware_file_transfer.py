from __future__ import annotations

import hashlib
import re
import shlex
import subprocess
import tempfile
import unittest
from pathlib import Path

from aha_cli.domain.models import utc_now
from aha_cli.services.hardware_file_transfer import (
    HardwareFileTransferError,
    _RAW_RECEIVER_SCRIPT,
    _RECEIVER_SCRIPT,
    _crc32c,
    _raw_bootstrap_command,
    _raw_cache_probe_command,
    _raw_frame_trailer,
    encode_octal_block,
    raw_receiver_script_bytes,
    receiver_script_bytes,
    send_file_via_raw_shell,
    send_file_via_shell,
)
from aha_cli.store.io import append_jsonl


class HardwareFileTransferTests(unittest.TestCase):
    def test_crc32c_matches_castagnoli_reference_vector(self) -> None:
        self.assertEqual(_crc32c(b"123456789"), 0xE3069283)

    def test_compiled_receiver_cache_markers_preserve_shell_fallback(self) -> None:
        bootstrap = _raw_bootstrap_command("test-token")
        probe = _raw_cache_probe_command("test-token")

        self.assertIn("shell-sha256-v3", bootstrap)
        self.assertIn("IFS= read -r cap", probe)
        self.assertNotIn("cat ", probe)

    def test_raw_receiver_retries_corruption_and_round_trips_all_bytes(self) -> None:
        payload = bytes(range(256)) * 4 + b"aha\x00raw\n"
        corrupted = bytearray(payload)
        corrupted[517] ^= 0x20
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            script = root / "recv-raw.sh"
            destination = root / "result.bin"
            script.write_text(_RAW_RECEIVER_SCRIPT, encoding="utf-8")
            digest = hashlib.sha256(payload).hexdigest()
            process = subprocess.Popen(
                ["sh", str(script), str(destination), str(len(payload)), digest, "test-token"],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                bufsize=0,
            )
            self.assertIsNotNone(process.stdin)
            self.assertIsNotNone(process.stdout)
            self.assertEqual(process.stdout.readline(), b"AHA-RAW READY test-token\n")

            header = f"F 0 {len(payload)} {digest}\n".encode("ascii")
            process.stdin.write(header)
            process.stdin.flush()
            self.assertEqual(process.stdout.readline(), b"AHA-RAW DATA test-token 0\n")
            process.stdin.write(bytes(corrupted) + _raw_frame_trailer("test-token", 0))
            process.stdin.flush()
            self.assertEqual(process.stdout.readline(), b"AHA-RAW NAK test-token 0 sha256\n")

            process.stdin.write(header)
            process.stdin.flush()
            self.assertEqual(process.stdout.readline(), b"AHA-RAW DATA test-token 0\n")
            process.stdin.write(payload[:-1] + _raw_frame_trailer("test-token", 0))
            process.stdin.flush()
            self.assertEqual(process.stdout.readline(), b"AHA-RAW NAK test-token 0 framing\n")

            process.stdin.write(header)
            process.stdin.flush()
            self.assertEqual(process.stdout.readline(), b"AHA-RAW DATA test-token 0\n")
            process.stdin.write(payload + _raw_frame_trailer("test-token", 0))
            process.stdin.flush()
            self.assertEqual(process.stdout.readline(), f"AHA-RAW ACK test-token 0 {len(payload)}\n".encode("ascii"))
            process.stdin.write(b"E\n")
            process.stdin.flush()
            self.assertEqual(
                process.stdout.readline(),
                f"AHA-RAW DONE test-token {len(payload)} {digest}\n".encode("ascii"),
            )
            _stdout, stderr = process.communicate(timeout=10.0)

            self.assertEqual(process.returncode, 0, stderr.decode("utf-8", errors="replace"))
            self.assertEqual(destination.read_bytes(), payload)
            self.assertLess(raw_receiver_script_bytes(), 6000)

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
                f"D{index}:{hashlib.sha256(chunk).hexdigest()}:{encode_octal_block(chunk)}\n"
                for index, chunk in enumerate(chunks)
            ) + "E\n"
            stdout, stderr = process.communicate(wire.encode("ascii"), timeout=10.0)

            self.assertEqual(process.returncode, 0, stderr.decode("utf-8", errors="replace"))
            self.assertEqual(destination.read_bytes(), payload)
            self.assertIn(b"AHA-RECV DONE test-token", stdout)
            self.assertLess(receiver_script_bytes(), 5000)

    def test_receiver_rejects_corrupted_block_before_append(self) -> None:
        payload = bytes(range(128))
        corrupted = bytearray(payload)
        corrupted[37] ^= 0x01
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
            wire = (
                f"D0:{digest}:{encode_octal_block(bytes(corrupted))}\n"
                f"D0:{digest}:{encode_octal_block(payload)}\n"
                "E\n"
            )
            stdout, stderr = process.communicate(wire.encode("ascii"), timeout=10.0)

            self.assertEqual(process.returncode, 0, stderr.decode("utf-8", errors="replace"))
            self.assertEqual(destination.read_bytes(), payload)
            self.assertIn(b"AHA-RECV NAK test-token 0 sha256", stdout)
            self.assertIn(b"AHA-RECV ACK test-token 0 128", stdout)

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
                if text.startswith("if [ -x /tmp/.aha-recv-v2 ]"):
                    token = text.rsplit(" MISS ", 1)[1].split(";", 1)[0].strip("' ")
                    emit(f"AHA-MISS {token}\n")
                    return
                if "AHA-%s %s\\n' BOOT" in text:
                    token = text.rsplit(" BOOT ", 1)[1].splitlines()[0].strip("' ")
                    emit(f"AHA-BOOT {token}\n")
                    return
                if text.startswith("sh /tmp/.aha-recv-v2"):
                    token = shlex.split(text)[-1]
                    emit(f"AHA-RECV READY {token}\n")
                    return
                if text.startswith("D"):
                    header, block_sha256, encoded = text.rstrip("\n").split(":", 2)
                    sequence = int(header[1:])
                    sends_by_sequence[sequence] = sends_by_sequence.get(sequence, 0) + 1
                    if sequence == 0 and sends_by_sequence[sequence] == 1:
                        emit(f"AHA-RECV NAK {token} {sequence} sha256\n")
                        return
                    decoded = bytes(
                        int(encoded[index + 2:index + 5], 8)
                        for index in range(0, len(encoded), 5)
                    )
                    self.assertEqual(hashlib.sha256(decoded).hexdigest(), block_sha256)
                    received.extend(decoded)
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

    def test_send_file_via_raw_shell_retries_nak_and_verifies(self) -> None:
        payload = bytes(range(256)) * 3
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source.bin"
            source.write_bytes(payload)
            device = "/dev/ttyTEST0"
            stream_path = root / "raw-stream.jsonl"
            received = bytearray()
            pending: tuple[int, int, str] | None = None
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
                nonlocal pending, token
                if text.startswith("if [ -x /tmp/.aha-recv-v3 ]"):
                    token_match = re.search(r"[0-9a-f]{16}", text)
                    self.assertIsNotNone(token_match)
                    token = str(token_match.group(0))
                    emit(f"AHA-RAWCACHE {token} crc32c-v1\n")
                    return
                if text.startswith("/tmp/.aha-recv-v3"):
                    token = shlex.split(text)[-1]
                    emit(f"AHA-RAW READY {token}\n")
                    return
                if text.startswith("C "):
                    _kind, sequence_text, size_text, checksum = text.split()
                    pending = (int(sequence_text), int(size_text), checksum)
                    emit(f"AHA-RAW DATA {token} {sequence_text}\n")
                    return
                if text == "E\n":
                    digest = hashlib.sha256(received).hexdigest()
                    emit(f"AHA-RAW DONE {token} {len(received)} {digest}\n")

            def send_bytes(data: bytes) -> None:
                nonlocal pending
                self.assertIsNotNone(pending)
                sequence, frame_size, checksum = pending
                frame = data[:frame_size]
                self.assertEqual(data[frame_size:], _raw_frame_trailer(token, sequence))
                self.assertEqual(f"{_crc32c(frame):08x}", checksum)
                sends_by_sequence[sequence] = sends_by_sequence.get(sequence, 0) + 1
                if sequence == 0 and sends_by_sequence[sequence] == 1:
                    emit(f"AHA-RAW NAK {token} {sequence} crc32c\n")
                    return
                received.extend(frame)
                emit(f"AHA-RAW ACK {token} {sequence} {len(received)}\n")
                pending = None

            result = send_file_via_raw_shell(
                root,
                device,
                source,
                "/tmp/received.bin",
                send_text=send_text,
                send_bytes=send_bytes,
                stream_path=stream_path,
                chunk_size=256,
                timeout=0.05,
                retries=2,
            )

            self.assertEqual(bytes(received), payload)
            self.assertEqual(result.chunks, 3)
            self.assertEqual(result.retries, 1)
            self.assertEqual(result.sha256, hashlib.sha256(payload).hexdigest())

    def test_send_file_via_raw_shell_keeps_sha256_shell_receiver_compatibility(self) -> None:
        payload = b"legacy-shell-receiver"
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source.bin"
            source.write_bytes(payload)
            stream_path = root / "raw-stream.jsonl"
            token = ""
            pending: tuple[int, int, str] | None = None

            def emit(data: str) -> None:
                append_jsonl(stream_path, {"direction": "rx", "data": data})

            def send_text(text: str) -> None:
                nonlocal token, pending
                if text.startswith("if [ -x /tmp/.aha-recv-v3 ]"):
                    token_match = re.search(r"[0-9a-f]{16}", text)
                    self.assertIsNotNone(token_match)
                    token = str(token_match.group(0))
                    emit(f"AHA-RAWCACHE {token} shell-sha256-v3\n")
                elif text.startswith("/tmp/.aha-recv-v3"):
                    emit(f"AHA-RAW READY {token}\n")
                elif text.startswith("F "):
                    _kind, sequence, size, digest = text.split()
                    pending = (int(sequence), int(size), digest)
                    emit(f"AHA-RAW DATA {token} {sequence}\n")
                elif text == "E\n":
                    emit(f"AHA-RAW DONE {token} {len(payload)} {hashlib.sha256(payload).hexdigest()}\n")

            def send_bytes(data: bytes) -> None:
                self.assertIsNotNone(pending)
                sequence, size, digest = pending
                frame = data[:size]
                self.assertEqual(hashlib.sha256(frame).hexdigest(), digest)
                self.assertEqual(data[size:], _raw_frame_trailer(token, sequence))
                emit(f"AHA-RAW ACK {token} {sequence} {len(frame)}\n")

            result = send_file_via_raw_shell(
                root,
                "/dev/ttyTEST0",
                source,
                "/tmp/received.bin",
                send_text=send_text,
                send_bytes=send_bytes,
                stream_path=stream_path,
                chunk_size=256,
                timeout=0.05,
            )

        self.assertEqual(result.size, len(payload))
        self.assertEqual(result.retries, 0)

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

            with self.assertRaisesRegex(HardwareFileTransferError, "raw chunk size"):
                send_file_via_raw_shell(
                    Path(tmp),
                    "/dev/ttyTEST0",
                    source,
                    "/tmp/result.bin",
                    send_text=lambda _text: None,
                    send_bytes=lambda _data: None,
                    chunk_size=128 * 1024 + 1,
                )


if __name__ == "__main__":
    unittest.main()
