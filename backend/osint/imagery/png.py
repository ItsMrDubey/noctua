"""Minimal 8-bit RGB PNG encoder/decoder (zlib + struct) and an image-difference score."""

import struct
import zlib

SIGNATURE = b"\x89PNG\r\n\x1a\n"


def _chunk(kind: bytes, body: bytes) -> bytes:
    return (struct.pack(">I", len(body)) + kind + body
            + struct.pack(">I", zlib.crc32(kind + body) & 0xFFFFFFFF))


def encode_rgb(width: int, height: int, rgb: bytes) -> bytes:
    if len(rgb) != width * height * 3:
        raise ValueError("pixel buffer does not match dimensions")
    stride = width * 3
    raw = b"".join(b"\x00" + rgb[y * stride:(y + 1) * stride] for y in range(height))
    return (SIGNATURE
            + _chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
            + _chunk(b"IDAT", zlib.compress(raw, 6))
            + _chunk(b"IEND", b""))


def decode_rgb(png: bytes) -> tuple[int, int, bytes]:
    """Decode an 8-bit RGB non-interlaced PNG (any filter type)."""
    if png[:8] != SIGNATURE:
        raise ValueError("not a PNG")
    pos, idat, hdr = 8, bytearray(), None
    while pos < len(png):
        n, kind = struct.unpack(">I4s", png[pos:pos + 8])
        body = png[pos + 8:pos + 8 + n]
        pos += 12 + n
        if kind == b"IHDR":
            hdr = struct.unpack(">IIBBBBB", body)
        elif kind == b"IDAT":
            idat += body
        elif kind == b"IEND":
            break
    if hdr is None or hdr[2:] != (8, 2, 0, 0, 0):
        raise ValueError("unsupported PNG (need 8-bit RGB, non-interlaced)")
    w, h = hdr[0], hdr[1]
    raw, stride = zlib.decompress(bytes(idat)), w * 3
    out = bytearray(h * stride)
    for y in range(h):
        ft = raw[y * (stride + 1)]
        line = bytearray(raw[y * (stride + 1) + 1:(y + 1) * (stride + 1)])
        prev = out[(y - 1) * stride:y * stride] if y else bytes(stride)
        for i in range(stride):
            a = line[i - 3] if i >= 3 else 0
            b = prev[i]
            c = prev[i - 3] if i >= 3 else 0
            if ft == 1:
                line[i] = (line[i] + a) & 255
            elif ft == 2:
                line[i] = (line[i] + b) & 255
            elif ft == 3:
                line[i] = (line[i] + ((a + b) >> 1)) & 255
            elif ft == 4:
                p = a + b - c
                pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
                pr = a if pa <= pb and pa <= pc else (b if pb <= pc else c)
                line[i] = (line[i] + pr) & 255
        out[y * stride:(y + 1) * stride] = line
    return w, h, bytes(out)


def change_score(a: bytes, b: bytes) -> float | None:
    """Mean absolute RGB difference of two same-size crops, normalised to 0-1.

    Pixels that are nodata (all-zero) in either image are ignored; None if nothing overlaps.
    """
    if len(a) != len(b) or len(a) % 3:
        raise ValueError("crops differ in size")
    total = count = 0
    for i in range(0, len(a), 3):
        ar, ag, ab = a[i], a[i + 1], a[i + 2]
        br, bg, bb = b[i], b[i + 1], b[i + 2]
        if not (ar or ag or ab) or not (br or bg or bb):
            continue
        total += abs(ar - br) + abs(ag - bg) + abs(ab - bb)
        count += 1
    return None if count == 0 else total / (count * 3 * 255)
