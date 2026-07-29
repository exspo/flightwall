#!/usr/bin/env python3
"""Generate the app icons as an LED dot-matrix aircraft.

Pure stdlib PNG writing, so the icons can be regenerated on any machine
without installing an imaging library. Run from the repo root:

    python3 scripts/make_icons.py
"""

from __future__ import annotations

import struct
import zlib
from pathlib import Path

OUT = Path(__file__).resolve().parent.parent / "web" / "icons"

GRID = 24  # dots across

BG = (0x05, 0x07, 0x0C)
DOT_OFF = (0x14, 0x18, 0x21)
CYAN = (0x35, 0xE0, 0xD0)
RED = (0xFF, 0x4D, 0x5E)
GREEN = (0x5D, 0xF0, 0x8A)

# Top-down airliner, nose up, symmetric about the 11/12 column pair.
SPANS = [
    (1, 11, 12),
    (2, 11, 12),
    (3, 10, 13),
    (4, 10, 13),
    (5, 10, 13),
    (6, 10, 13),
    (7, 1, 22),
    (8, 1, 22),
    (9, 3, 20),
    (10, 9, 14),
    (11, 9, 14),
    (12, 9, 14),
    (13, 9, 14),
    (14, 9, 14),
    (15, 9, 14),
    (16, 9, 14),
    (17, 5, 18),
    (18, 5, 18),
    (19, 8, 15),
    (20, 10, 13),
]


def build_glyph() -> dict:
    """Map each lit dot to its colour. Wingtips get navigation-light colours:
    port red, starboard green."""
    lit = {}
    for row, start, end in SPANS:
        for col in range(start, end + 1):
            lit[(col, row)] = CYAN
    for row in (7, 8):
        for col in (1, 2):
            lit[(col, row)] = RED
        for col in (21, 22):
            lit[(col, row)] = GREEN
    return lit


def write_png(path: Path, size: int, padding: float = 0.0) -> None:
    lit = build_glyph()
    pixels = bytearray()

    usable = size * (1 - 2 * padding)
    pitch = usable / GRID
    origin = size * padding
    radius = pitch * 0.40

    # Precompute each dot's centre so the inner loop stays cheap.
    centres = [origin + i * pitch + pitch / 2 for i in range(GRID)]
    r2 = radius * radius

    for y in range(size):
        pixels.append(0)  # PNG filter type 0 for this scanline
        row_index = int((y - origin) // pitch) if pitch else -1
        for x in range(size):
            col_index = int((x - origin) // pitch) if pitch else -1
            color = BG
            if 0 <= row_index < GRID and 0 <= col_index < GRID:
                dx = x - centres[col_index]
                dy = y - centres[row_index]
                if dx * dx + dy * dy <= r2:
                    color = lit.get((col_index, row_index), DOT_OFF)
            pixels.extend(color)

    def chunk(tag: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data))
            + tag
            + data
            + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
        )

    header = struct.pack(">IIBBBBB", size, size, 8, 2, 0, 0, 0)  # 8-bit RGB
    png = (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", header)
        + chunk(b"IDAT", zlib.compress(bytes(pixels), 9))
        + chunk(b"IEND", b"")
    )
    path.write_bytes(png)
    print(f"wrote {path.relative_to(path.parent.parent.parent)} ({len(png)} bytes)")


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    write_png(OUT / "icon-180.png", 180, padding=0.06)
    write_png(OUT / "icon-192.png", 192, padding=0.06)
    write_png(OUT / "icon-512.png", 512, padding=0.06)
    # Maskable icons get cropped to a circle by some launchers, so keep the
    # artwork inside the 80% safe zone.
    write_png(OUT / "icon-maskable-512.png", 512, padding=0.16)


if __name__ == "__main__":
    main()
