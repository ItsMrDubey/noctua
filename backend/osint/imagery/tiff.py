"""Minimal tiled GeoTIFF / COG reader over HTTP range requests (stdlib only).

Supports classic (non-Big) TIFF, chunky tiles, uncompressed or deflate compression, horizontal
predictor, 8/16-bit samples. Anything else raises :class:`Unsupported` so callers can fall back.
"""

import struct
import sys
import zlib
from array import array
from dataclasses import dataclass, field
from itertools import accumulate
from typing import Protocol

import httpx

BLOCK = 65536


class Unsupported(Exception):
    """The file uses a TIFF feature this reader does not implement."""


class Reader(Protocol):
    def read(self, offset: int, length: int) -> bytes: ...

    def get(self, offset: int, length: int) -> bytes: ...


class RangeFile:
    """HTTP range reader: ``read`` uses a block cache (headers), ``get`` is one exact range."""

    def __init__(self, client: httpx.Client, url: str, block: int = BLOCK) -> None:
        self.client, self.url, self.block = client, url, block
        self._blocks: dict[int, bytes] = {}
        self.requests = 0

    def get(self, offset: int, length: int) -> bytes:
        self.requests += 1
        r = self.client.get(self.url, headers={"Range": f"bytes={offset}-{offset + length - 1}"})
        if r.status_code == 206:
            return r.content
        if r.status_code == 200:  # server ignored Range
            return r.content[offset:offset + length]
        r.raise_for_status()
        raise httpx.HTTPError(f"unexpected status {r.status_code}")

    def read(self, offset: int, length: int) -> bytes:
        out = bytearray()
        pos, end = offset, offset + length
        while pos < end:
            b = pos // self.block
            if b not in self._blocks:
                self._blocks[b] = self.get(b * self.block, self.block)
            data = self._blocks[b]
            start = pos - b * self.block
            chunk = data[start:start + (end - pos)]
            if not chunk:
                break
            out += chunk
            pos += len(chunk)
            if len(data) < self.block and pos < end:
                break
        return bytes(out)


_TYPE = {1: ("B", 1), 2: ("B", 1), 3: ("H", 2), 4: ("I", 4), 6: ("b", 1), 7: ("B", 1),
         8: ("h", 2), 9: ("i", 4), 11: ("f", 4), 12: ("d", 8), 16: ("Q", 8)}

T_WIDTH, T_HEIGHT, T_BPS, T_COMPRESSION = 256, 257, 258, 259
T_SPP, T_PLANAR, T_PREDICTOR = 277, 284, 317
T_TILE_W, T_TILE_H, T_TILE_OFFSETS, T_TILE_COUNTS = 322, 323, 324, 325
T_SAMPLE_FORMAT, T_PIXEL_SCALE, T_TIEPOINT, T_GEOKEYS = 339, 33550, 33922, 34735


@dataclass
class Tiff:
    width: int
    height: int
    tile_w: int
    tile_h: int
    bits: int
    samples: int
    compression: int
    predictor: int
    offsets: list[int]
    counts: list[int]
    byteorder: str  # "<" or ">"
    origin: tuple[float, float] | None = None  # top-left corner (x, y) of pixel (0, 0)
    pixel_size: tuple[float, float] | None = None
    epsg: int | None = None
    tags: dict = field(default_factory=dict, repr=False)

    @property
    def tiles_across(self) -> int:
        return -(-self.width // self.tile_w)


def _read_values(rf: Reader, bo: str, ent: bytes, ftype: int, count: int):
    if ftype not in _TYPE:
        raise Unsupported(f"TIFF field type {ftype}")
    code, size = _TYPE[ftype]
    total = size * count
    raw = ent[8:8 + total] if total <= 4 else rf.read(struct.unpack(bo + "I", ent[8:12])[0], total)
    if len(raw) < total:
        raise Unsupported("truncated TIFF metadata")
    vals = struct.unpack(f"{bo}{count}{code}", raw)
    if ftype == 2:
        return bytes(vals).rstrip(b"\0").decode("latin-1")
    return vals


def parse_tiff(rf: Reader) -> Tiff:
    head = rf.read(0, 8)
    if head[:2] == b"II":
        bo = "<"
    elif head[:2] == b"MM":
        bo = ">"
    else:
        raise Unsupported("not a TIFF")
    magic = struct.unpack(bo + "H", head[2:4])[0]
    if magic == 43:
        raise Unsupported("BigTIFF")
    if magic != 42:
        raise Unsupported("bad TIFF magic")
    ifd = struct.unpack(bo + "I", head[4:8])[0]
    n = struct.unpack(bo + "H", rf.read(ifd, 2))[0]
    body = rf.read(ifd + 2, n * 12)
    if len(body) < n * 12:
        raise Unsupported("truncated IFD")
    tags: dict = {}
    for i in range(n):
        ent = body[i * 12:(i + 1) * 12]
        tag, ftype, count = struct.unpack(bo + "HHI", ent[:8])
        try:
            tags[tag] = _read_values(rf, bo, ent, ftype, count)
        except Unsupported:
            continue

    def one(tag: int, default=None):
        v = tags.get(tag)
        return default if v is None else v[0]

    if T_TILE_OFFSETS not in tags:
        raise Unsupported("striped TIFF (not tiled)")
    if one(T_PLANAR, 1) != 1:
        raise Unsupported("planar configuration 2")
    bps = tags.get(T_BPS, (8,))
    if len(set(bps)) != 1 or bps[0] not in (8, 16):
        raise Unsupported(f"bits per sample {bps}")
    if one(T_SAMPLE_FORMAT, 1) != 1:
        raise Unsupported("non-unsigned-integer samples")
    compression = one(T_COMPRESSION, 1)
    if compression not in (1, 8, 32946):
        raise Unsupported(f"compression {compression}")
    predictor = one(T_PREDICTOR, 1)
    if predictor not in (1, 2):
        raise Unsupported(f"predictor {predictor}")
    t = Tiff(width=one(T_WIDTH), height=one(T_HEIGHT), tile_w=one(T_TILE_W),
             tile_h=one(T_TILE_H), bits=bps[0], samples=one(T_SPP, 1), compression=compression,
             predictor=predictor, offsets=list(tags[T_TILE_OFFSETS]),
             counts=list(tags[T_TILE_COUNTS]), byteorder=bo, tags=tags)
    scale, tie = tags.get(T_PIXEL_SCALE), tags.get(T_TIEPOINT)
    if scale and tie and len(tie) >= 6:
        t.pixel_size = (scale[0], scale[1])
        t.origin = (tie[3] - tie[0] * scale[0], tie[4] + tie[1] * scale[1])
    gk = tags.get(T_GEOKEYS)
    if gk and len(gk) >= 4:
        for i in range(gk[3]):
            key, loc, _cnt, val = gk[4 + i * 4:8 + i * 4]
            if key == 3072 and loc == 0:
                t.epsg = val
    return t


def _native_little() -> bool:
    return sys.byteorder == "little"


def decode_tile(raw: bytes, t: Tiff) -> bytes:
    """Decompress + un-predict one tile into native-endian interleaved samples."""
    data = zlib.decompress(raw) if t.compression in (8, 32946) else raw
    row_samples = t.tile_w * t.samples
    nbytes = t.bits // 8
    expected = row_samples * t.tile_h * nbytes
    if len(data) < expected:
        raise Unsupported("short tile data")
    data = data[:expected]
    if nbytes == 1:
        if t.predictor == 2:
            buf = bytearray(data)
            for r in range(t.tile_h):
                base = r * row_samples
                for c in range(t.samples):
                    sl = slice(base + c, base + row_samples, t.samples)
                    buf[sl] = bytes(x & 255 for x in accumulate(buf[sl]))
            return bytes(buf)
        return data
    arr = array("H")
    arr.frombytes(data)
    if (t.byteorder == "<") != _native_little():
        arr.byteswap()
    if t.predictor == 2:
        for r in range(t.tile_h):
            base = r * row_samples
            for c in range(t.samples):
                sl = slice(base + c, base + row_samples, t.samples)
                arr[sl] = array("H", (x & 0xFFFF for x in accumulate(arr[sl])))
    return arr.tobytes()


@dataclass
class Window:
    width: int
    height: int
    samples: int
    bits: int
    data: bytes  # native-endian, interleaved, zero where outside the raster / no tile


def read_window(rf: Reader, t: Tiff, col0: int, row0: int, w: int, h: int) -> Window:
    """Read a pixel window, fetching only intersecting tiles. Out-of-raster pixels are 0."""
    nb = (t.bits // 8) * t.samples
    out = bytearray(w * h * nb)
    c1, r1 = col0 + w, row0 + h
    if c1 <= 0 or r1 <= 0 or col0 >= t.width or row0 >= t.height:
        return Window(w, h, t.samples, t.bits, bytes(out))
    tc0, tc1 = max(0, col0) // t.tile_w, (min(t.width, c1) - 1) // t.tile_w
    tr0, tr1 = max(0, row0) // t.tile_h, (min(t.height, r1) - 1) // t.tile_h
    for tr in range(tr0, tr1 + 1):
        for tc in range(tc0, tc1 + 1):
            idx = tr * t.tiles_across + tc
            if idx >= len(t.offsets) or t.counts[idx] == 0:
                continue
            tile = decode_tile(rf.get(t.offsets[idx], t.counts[idx]), t)
            tx0, ty0 = tc * t.tile_w, tr * t.tile_h
            x_a, x_b = max(col0, tx0), min(c1, tx0 + t.tile_w, t.width)
            y_a, y_b = max(row0, ty0), min(r1, ty0 + t.tile_h, t.height)
            for y in range(y_a, y_b):
                s = ((y - ty0) * t.tile_w + (x_a - tx0)) * nb
                d = ((y - row0) * w + (x_a - col0)) * nb
                out[d:d + (x_b - x_a) * nb] = tile[s:s + (x_b - x_a) * nb]
    return Window(w, h, t.samples, t.bits, bytes(out))
