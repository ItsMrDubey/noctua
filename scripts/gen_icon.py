"""Generate the desktop app icons (NOCTUA owl-eyes mark) without an imaging library.

Writes desktop/icons/{icon.png (512), 128x128.png, 32x32.png, icon.ico (16..256 PNG frames)}.
"""

from __future__ import annotations

import math
import struct
import zlib
from pathlib import Path

OUT = Path(__file__).resolve().parents[1] / "desktop" / "icons"
BG = (6, 14, 22)
RING = (94, 227, 234)
DIM = (31, 58, 82)


def _png(size: int, rgba: bytes) -> bytes:
    raw = b"".join(b"\x00" + rgba[y * size * 4:(y + 1) * size * 4] for y in range(size))
    def chunk(t: bytes, d: bytes) -> bytes:
        return struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d) & 0xFFFFFFFF)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw, 9)) + chunk(b"IEND", b""))


def _shade(x: float, y: float) -> tuple[tuple[int, int, int], float]:
    """Colour and coverage of the mark at unit coords (0..1)."""
    cx, cy = x - 0.5, y - 0.5
    r = math.hypot(cx, cy)
    if r > 0.48:
        return BG, 0.0
    col = BG
    # outer ring
    if 0.43 <= r <= 0.47:
        col = RING
    # two "eyes"
    for ex in (-0.15, 0.15):
        er = math.hypot(cx - ex, cy + 0.02)
        if 0.085 <= er <= 0.125:
            col = RING
        elif er < 0.045:
            col = RING
    # beak / crosshair tick
    if abs(cx) < 0.018 and 0.06 < cy < 0.2:
        col = DIM if col == BG else col
    return col, 1.0


def render(size: int, ss: int = 4) -> bytes:
    out = bytearray()
    for py in range(size):
        for px in range(size):
            acc = [0.0, 0.0, 0.0]
            cov = 0.0
            for sy in range(ss):
                for sx in range(ss):
                    c, a = _shade((px + (sx + 0.5) / ss) / size, (py + (sy + 0.5) / ss) / size)
                    cov += a
                    for i in range(3):
                        acc[i] += c[i] * a
            n = ss * ss
            if cov:
                out += bytes(int(acc[i] / cov) for i in range(3)) + bytes([int(255 * cov / n)])
            else:
                out += b"\x00\x00\x00\x00"
    return bytes(out)


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    pngs = {s: _png(s, render(s)) for s in (16, 24, 32, 48, 64, 128, 256)}
    (OUT / "icon.png").write_bytes(_png(512, render(512, 2)))
    (OUT / "128x128.png").write_bytes(pngs[128])
    (OUT / "32x32.png").write_bytes(pngs[32])
    sizes = sorted(pngs)
    header = struct.pack("<HHH", 0, 1, len(sizes))
    entries, data = b"", b""
    offset = 6 + 16 * len(sizes)
    for s in sizes:
        blob = pngs[s]
        entries += struct.pack("<BBBBHHII", s % 256, s % 256, 0, 0, 1, 32, len(blob), offset)
        data += blob
        offset += len(blob)
    (OUT / "icon.ico").write_bytes(header + entries + data)
    print("icons written to", OUT)


if __name__ == "__main__":
    main()
