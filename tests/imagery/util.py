"""Synthetic tiled GeoTIFF builder + in-memory range reader for imagery tests."""

import struct
import zlib

from osint.imagery.geo import utm_forward


def build_tiff(width, height, tile, pixels, *, spp=3, origin=(0.0, 0.0), pixel=(10.0, 10.0),
               epsg=32635, compression=8, predictor=2, bits=8):
    """pixels(x, y) -> tuple of spp ints. Little-endian classic TIFF, chunky tiles."""
    across, down = -(-width // tile), -(-height // tile)
    tiles = []
    for ty in range(down):
        for tx in range(across):
            rows = []
            for y in range(tile):
                row = []
                for x in range(tile):
                    gx, gy = tx * tile + x, ty * tile + y
                    row += list(pixels(gx, gy)) if gx < width and gy < height else [0] * spp
                if predictor == 2:
                    mod = 1 << bits
                    row = [(row[i] - (row[i - spp] if i >= spp else 0)) % mod
                           for i in range(len(row))]
                rows.append(row)
            flat = [v for r in rows for v in r]
            raw = bytes(flat) if bits == 8 else struct.pack(f"<{len(flat)}H", *flat)
            tiles.append(zlib.compress(raw) if compression == 8 else raw)
    # layout: header(8) | tile data | arrays | IFD
    offsets, counts, body = [], [], b""
    for t in tiles:
        offsets.append(8 + len(body))
        counts.append(len(t))
        body += t
    extra_pos = 8 + len(body)
    extra = b""
    entries = []  # (tag, type, count, 4-byte value-or-offset)

    def add(tag, typ, vals, fmt):
        nonlocal extra
        data = struct.pack("<" + fmt * len(vals), *vals)
        if len(data) <= 4:
            entries.append((tag, typ, len(vals), data.ljust(4, b"\0")))
        else:
            entries.append((tag, typ, len(vals), struct.pack("<I", extra_pos + len(extra))))
            extra += data

    add(256, 4, [width], "I")
    add(257, 4, [height], "I")
    add(258, 3, [bits] * spp, "H")
    add(259, 3, [compression], "H")
    add(262, 3, [2 if spp == 3 else 1], "H")
    add(277, 3, [spp], "H")
    add(284, 3, [1], "H")
    add(317, 3, [predictor], "H")
    add(322, 3, [tile], "H")
    add(323, 3, [tile], "H")
    add(324, 4, offsets, "I")
    add(325, 4, counts, "I")
    add(339, 3, [1] * spp, "H")
    add(33550, 12, [pixel[0], pixel[1], 0.0], "d")
    add(33922, 12, [0, 0, 0, origin[0], origin[1], 0.0], "d")
    add(34735, 3, [1, 1, 0, 3, 1024, 0, 1, 1, 1025, 0, 1, 1, 3072, 0, 1, epsg], "H")
    entries.sort(key=lambda e: e[0])
    ifd_off = extra_pos + len(extra)
    ifd = struct.pack("<H", len(entries))
    for tag, typ, cnt, val in entries:
        ifd += struct.pack("<HHI", tag, typ, cnt) + val
    ifd += struct.pack("<I", 0)
    return b"II*\x00" + struct.pack("<I", ifd_off) + body + extra + ifd


class MemFile:
    """Reader over bytes that records tile fetches."""

    def __init__(self, data: bytes):
        self.data, self.gets = data, []

    def read(self, off, n):
        return self.data[off:off + n]

    def get(self, off, n):
        self.gets.append((off, n))
        return self.data[off:off + n]


def scene_tiff(lat, lon, *, epsg=32635, size=80, tile=32, pixels=None, half_px=40):
    """A small UTM raster (10 m pixels) with (lat, lon) in its centre."""
    e, n = utm_forward(lat, lon, epsg)
    origin = (round(e) - half_px * 10, round(n) + half_px * 10)
    pixels = pixels or (lambda x, y: (10 + x % 200, 20 + y % 200, 30))
    return build_tiff(size, size, tile, pixels, origin=origin, epsg=epsg)
