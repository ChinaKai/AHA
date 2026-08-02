from __future__ import annotations

import argparse
import math
from pathlib import Path
import struct
import zlib

ICON_SIZES = (16, 20, 24, 32, 40, 48, 64, 128, 256)
CANVAS_SIZE = 64.0
BACKGROUND = (15, 23, 42, 255)
FOREGROUND = (248, 250, 252, 255)
ACCENT = (56, 189, 248, 255)


def _inside_rounded_rect(x: float, y: float, radius: float = 14.0) -> bool:
    cx = min(max(x, radius), CANVAS_SIZE - radius)
    cy = min(max(y, radius), CANVAS_SIZE - radius)
    return (x - cx) ** 2 + (y - cy) ** 2 <= radius**2


def _distance_to_segment(x: float, y: float, start: tuple[float, float], end: tuple[float, float]) -> float:
    ax, ay = start
    bx, by = end
    dx, dy = bx - ax, by - ay
    length_squared = dx * dx + dy * dy
    if not length_squared:
        return math.hypot(x - ax, y - ay)
    ratio = min(1.0, max(0.0, ((x - ax) * dx + (y - ay) * dy) / length_squared))
    return math.hypot(x - (ax + ratio * dx), y - (ay + ratio * dy))


def _sample_color(x: float, y: float) -> tuple[int, int, int, int] | None:
    color = BACKGROUND if _inside_rounded_rect(x, y) else None
    logo = ((12.0, 48.0), (22.0, 17.0), (32.0, 48.0), (42.0, 17.0), (52.0, 48.0))
    if any(_distance_to_segment(x, y, start, end) <= 2.6 for start, end in zip(logo, logo[1:])):
        color = FOREGROUND
    if _distance_to_segment(x, y, (16.0, 35.5), (48.0, 35.5)) <= 2.4:
        color = ACCENT
    return color


def _rgba(size: int, supersample: int = 4) -> bytes:
    pixels = bytearray()
    scale = CANVAS_SIZE / size
    samples = supersample * supersample
    for py in range(size):
        for px in range(size):
            channels = [0, 0, 0]
            covered = 0
            for sy in range(supersample):
                for sx in range(supersample):
                    x = (px + (sx + 0.5) / supersample) * scale
                    y = (py + (sy + 0.5) / supersample) * scale
                    color = _sample_color(x, y)
                    if color is None:
                        continue
                    covered += 1
                    for index in range(3):
                        channels[index] += color[index]
            if not covered:
                pixels.extend((0, 0, 0, 0))
                continue
            pixels.extend((*[round(value / covered) for value in channels], round(255 * covered / samples)))
    return bytes(pixels)


def _png_chunk(kind: bytes, payload: bytes) -> bytes:
    return struct.pack(">I", len(payload)) + kind + payload + struct.pack(">I", zlib.crc32(kind + payload) & 0xFFFFFFFF)


def png_image(size: int) -> bytes:
    rgba = _rgba(size)
    rows = b"".join(b"\x00" + rgba[offset : offset + size * 4] for offset in range(0, len(rgba), size * 4))
    header = struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + _png_chunk(b"IHDR", header) + _png_chunk(b"IDAT", zlib.compress(rows, 9)) + _png_chunk(b"IEND", b"")


def build_ico(output: Path) -> Path:
    images = [(size, png_image(size)) for size in ICON_SIZES]
    directory_size = 6 + 16 * len(images)
    offset = directory_size
    entries = []
    payloads = []
    for size, payload in images:
        dimension = 0 if size == 256 else size
        entries.append(struct.pack("<BBBBHHII", dimension, dimension, 0, 0, 1, 32, len(payload), offset))
        payloads.append(payload)
        offset += len(payload)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_bytes(struct.pack("<HHH", 0, 1, len(images)) + b"".join(entries) + b"".join(payloads))
    return output


def main() -> int:
    parser = argparse.ArgumentParser(description="Build the Windows ICO matching web/static/aha-logo.svg")
    parser.add_argument("--output", default="src/aha_cli/assets/aha.ico")
    args = parser.parse_args()
    print(build_ico(Path(args.output)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
