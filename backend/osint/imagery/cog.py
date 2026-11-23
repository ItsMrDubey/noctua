"""Cloud-optimised GeoTIFF reader for the imagery viewer: every overview level, numpy decode.

The before/after job keeps the stdlib reader in :mod:`osint.imagery.tiff`; the viewer needs
more: overview levels (thumbnails read a few small tiles instead of the full-resolution
ones), 32-bit float samples with the floating-point predictor (Sentinel-1 RTC), JPEG-in-TIFF
tiles in BigTIFF files (Maxar Open Data ``visual`` assets; JPEG decoded with Pillow) and
fast numpy un-prediction.
Tiles of one window are fetched in parallel over HTTP range requests; headers are cached
per URL (without its query string, so a refreshed SAS token reuses them).
"""

import io
import struct
import threading
import zlib
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

import numpy as np

from osint.imagery.tiff import (
    _TYPE,
    T_BPS,
    T_COMPRESSION,
    T_GEOKEYS,
    T_HEIGHT,
    T_PIXEL_SCALE,
    T_PREDICTOR,
    T_SAMPLE_FORMAT,
    T_SPP,
    T_TIEPOINT,
    T_TILE_COUNTS,
    T_TILE_H,
    T_TILE_OFFSETS,
    T_TILE_W,
    T_WIDTH,
    Reader,
    Unsupported,
    _read_values,
)

T_SUBFILE, T_PHOTOMETRIC, T_PLANAR, T_JPEG_TABLES, T_NODATA = 254, 262, 284, 347, 42113
DEFLATE, JPEG, NONE = (8, 32946), 7, 1
MAX_IFDS = 16
FETCH_WORKERS = 8


@dataclass
class Level:
    """One resolution level (full resolution first, then overviews, coarsest last)."""
    width: int
    height: int
    tile_w: int
    tile_h: int
    offsets: list[int]
    counts: list[int]
    jpeg_tables: bytes | None = None

    @property
    def tiles_across(self) -> int:
        return -(-self.width // self.tile_w)


@dataclass
class Cog:
    bits: int
    samples: int
    sample_format: int          # 1 unsigned int, 3 IEEE float
    compression: int
    predictor: int
    byteorder: str
    levels: list[Level]
    origin: tuple[float, float] | None = None   # top-left corner of pixel (0, 0), full res
    pixel_size: tuple[float, float] | None = None
    epsg: int | None = None
    nodata: float | None = None
    extra: dict = field(default_factory=dict, repr=False)

    def level_pixel(self, i: int) -> tuple[float, float]:
        """Pixel size (map units) of level ``i``."""
        full = self.levels[0]
        lv = self.levels[i]
        return (self.pixel_size[0] * full.width / lv.width,
                self.pixel_size[1] * full.height / lv.height)

    def pick_level(self, target_px: float) -> int:
        """Coarsest level whose pixels are still at least as fine as ``target_px``."""
        best = 0
        for i in range(len(self.levels)):
            if self.level_pixel(i)[0] <= target_px * 1.001:
                best = i
        return best

    @property
    def dtype(self) -> np.dtype:
        if self.sample_format == 3:
            return np.dtype(self.byteorder + "f" + str(self.bits // 8))
        return np.dtype(self.byteorder + "u" + str(self.bits // 8))


_BIG_TYPE = {**_TYPE, 16: ("Q", 8), 17: ("q", 8), 18: ("Q", 8)}


def _values(rf: Reader, bo: str, ent: bytes, ftype: int, count: int, big: bool):
    """Values of one IFD entry; classic TIFF keeps up to 4 bytes inline, BigTIFF 8."""
    if not big:
        return _read_values(rf, bo, ent, ftype, count)
    if ftype not in _BIG_TYPE:
        raise Unsupported(f"TIFF field type {ftype}")
    code, size = _BIG_TYPE[ftype]
    total = size * count
    raw = ent[12:12 + total] if total <= 8 else rf.read(struct.unpack(bo + "Q", ent[12:20])[0], total)
    if len(raw) < total:
        raise Unsupported("truncated TIFF metadata")
    vals = struct.unpack(f"{bo}{count}{code}", raw)
    if ftype == 2:
        return bytes(vals).rstrip(b"\0").decode("latin-1")
    return vals


def _ifd(rf: Reader, bo: str, off: int, big: bool = False) -> tuple[dict, int]:
    """Tags of the IFD at ``off`` and the offset of the next one (classic or BigTIFF)."""
    cnt_fmt, ent_len, nxt_fmt = ("Q", 20, "Q") if big else ("H", 12, "I")
    head = 8 if big else 2
    n = struct.unpack(bo + cnt_fmt, rf.read(off, head))[0]
    nxt = 8 if big else 4
    body = rf.read(off + head, n * ent_len + nxt)
    if len(body) < n * ent_len + nxt:
        raise Unsupported("truncated IFD")
    tags: dict = {}
    for i in range(n):
        ent = body[i * ent_len:(i + 1) * ent_len]
        if big:
            tag, ftype, count = struct.unpack(bo + "HHQ", ent[:12])
        else:
            tag, ftype, count = struct.unpack(bo + "HHI", ent[:8])
        try:
            tags[tag] = _values(rf, bo, ent, ftype, count, big)
        except Unsupported:
            continue
    return tags, struct.unpack(bo + nxt_fmt, body[n * ent_len:n * ent_len + nxt])[0]


def parse_cog(rf: Reader) -> Cog:
    head = rf.read(0, 16)
    if head[:2] not in (b"II", b"MM"):
        raise Unsupported("not a TIFF")
    bo = "<" if head[:2] == b"II" else ">"
    magic = struct.unpack(bo + "H", head[2:4])[0]
    big = magic == 43  # BigTIFF (Maxar Open Data visual assets): 64-bit offsets
    if magic not in (42, 43):
        raise Unsupported("bad TIFF magic")
    off = struct.unpack(bo + ("Q", "I")[not big], head[8:16] if big else head[4:8])[0]
    ifds = []
    while off and len(ifds) < MAX_IFDS:
        tags, off = _ifd(rf, bo, off, big)
        ifds.append(tags)
    if not ifds:
        raise Unsupported("no IFD")
    t0 = ifds[0]

    def one(tags: dict, tag: int, default=None):
        v = tags.get(tag)
        return default if v is None else (v[0] if isinstance(v, tuple) else v)

    if T_TILE_OFFSETS not in t0:
        raise Unsupported("striped TIFF (not tiled)")
    if one(t0, T_PLANAR, 1) != 1:
        raise Unsupported("planar configuration 2")
    bps = t0.get(T_BPS, (8,))
    fmt = one(t0, T_SAMPLE_FORMAT, 1)
    if len(set(bps)) != 1 or (fmt == 1 and bps[0] not in (8, 16)) or (fmt == 3 and bps[0] != 32) \
            or fmt not in (1, 3):
        raise Unsupported(f"samples {bps} format {fmt}")
    comp = one(t0, T_COMPRESSION, 1)
    if comp not in (NONE, JPEG, *DEFLATE):
        raise Unsupported(f"compression {comp}")
    pred = one(t0, T_PREDICTOR, 1)
    if pred not in (1, 2, 3):
        raise Unsupported(f"predictor {pred}")
    spp = one(t0, T_SPP, 1)
    levels = []
    for tags in ifds:
        # overviews only (reduced-resolution images of the same samples); masks are skipped
        if tags is not t0 and (one(tags, T_SUBFILE, 0) & 4 or one(tags, T_SPP, 1) != spp
                               or T_TILE_OFFSETS not in tags):
            continue
        tables = tags.get(T_JPEG_TABLES)
        levels.append(Level(one(tags, T_WIDTH), one(tags, T_HEIGHT), one(tags, T_TILE_W),
                            one(tags, T_TILE_H), list(tags[T_TILE_OFFSETS]),
                            list(tags[T_TILE_COUNTS]),
                            bytes(tables) if tables is not None and comp == JPEG else None))
    levels.sort(key=lambda lv: -lv.width)
    c = Cog(bits=bps[0], samples=spp, sample_format=fmt, compression=comp, predictor=pred,
            byteorder=bo, levels=levels)
    scale, tie = t0.get(T_PIXEL_SCALE), t0.get(T_TIEPOINT)
    if scale and tie and len(tie) >= 6:
        c.pixel_size = (scale[0], scale[1])
        c.origin = (tie[3] - tie[0] * scale[0], tie[4] + tie[1] * scale[1])
    gk = t0.get(T_GEOKEYS)
    if gk and len(gk) >= 4:
        for i in range(gk[3]):
            key, loc, _cnt, val = gk[4 + i * 4:8 + i * 4]
            if key == 3072 and loc == 0:
                c.epsg = val
    nd = t0.get(T_NODATA)
    if isinstance(nd, str) and nd.strip():
        try:
            c.nodata = float(nd.strip())
        except ValueError:
            pass
    return c


# ------------------------------------------------------------------ tile decoding

def _unpredict_int(a: np.ndarray) -> np.ndarray:
    """Horizontal differencing (predictor 2) undone per row and sample; wraps like the
    encoder (cumsum in the sample dtype)."""
    return np.cumsum(a, axis=1, dtype=a.dtype)


def _unpredict_float(raw: bytes, h: int, w: int, spp: int, nbytes: int) -> np.ndarray:
    """Floating-point predictor (3): per row, byte-wise differencing over the row's bytes
    stored most-significant-byte plane first."""
    b = np.frombuffer(raw, np.uint8).reshape(h, w * nbytes, spp)  # byte stride = samples
    b = np.cumsum(b, axis=1, dtype=np.uint8).reshape(h, w * spp * nbytes)
    b = b.reshape(h, nbytes, w * spp).transpose(0, 2, 1)  # -> (h, w*spp, nbytes) big-endian
    return np.ascontiguousarray(b).view(">f" + str(nbytes)).reshape(h, w, spp)


def decode_tile(raw: bytes, c: Cog, lv: Level) -> np.ndarray:
    """One tile -> (tile_h, tile_w, samples) array (native byte order)."""
    h, w, spp = lv.tile_h, lv.tile_w, c.samples
    if c.compression == JPEG:
        try:
            from PIL import Image
        except ImportError as exc:  # optional: only Maxar visual assets use JPEG tiles
            raise Unsupported("JPEG tiles need Pillow (uv sync)") from exc
        data = raw
        if lv.jpeg_tables and len(lv.jpeg_tables) > 4:
            # abbreviated tile stream: splice the shared tables in after its SOI marker
            data = lv.jpeg_tables[:-2] + raw[2:]
        with Image.open(io.BytesIO(data)) as im:
            im.draft(None, None)
            arr = np.asarray(im.convert("RGB" if spp == 3 else "L"))
        arr = arr.reshape(arr.shape[0], arr.shape[1], spp)
        out = np.zeros((h, w, spp), np.uint8)
        out[:min(h, arr.shape[0]), :min(w, arr.shape[1])] = arr[:h, :w]
        return out
    data = zlib.decompress(raw) if c.compression in DEFLATE else raw
    nbytes = c.bits // 8
    need = h * w * spp * nbytes
    if len(data) < need:
        raise Unsupported("short tile data")
    data = data[:need]
    if c.predictor == 3:
        return _unpredict_float(data, h, w, spp, nbytes).astype(np.float32)
    a = np.frombuffer(data, c.dtype).reshape(h, w, spp)
    a = a.astype(a.dtype.newbyteorder("="))
    if c.predictor == 2:
        a = _unpredict_int(a)
    return a


# ------------------------------------------------------------------ windows

def read_window(rf: Reader, c: Cog, level: int, col0: int, row0: int, w: int, h: int,
                workers: int = FETCH_WORKERS) -> np.ndarray:
    """Pixel window of a level as (h, w, samples); pixels outside the raster or in missing
    tiles are 0 (NaN for float rasters). Intersecting tiles are fetched in parallel."""
    lv = c.levels[level]
    dt = np.float32 if c.sample_format == 3 else c.dtype.newbyteorder("=")
    out = np.full((h, w, c.samples), np.nan if c.sample_format == 3 else 0, dtype=dt)
    c1, r1 = col0 + w, row0 + h
    if c1 <= 0 or r1 <= 0 or col0 >= lv.width or row0 >= lv.height:
        return out
    tc0, tc1 = max(0, col0) // lv.tile_w, (min(lv.width, c1) - 1) // lv.tile_w
    tr0, tr1 = max(0, row0) // lv.tile_h, (min(lv.height, r1) - 1) // lv.tile_h
    jobs = []
    for tr in range(tr0, tr1 + 1):
        for tc in range(tc0, tc1 + 1):
            idx = tr * lv.tiles_across + tc
            if idx < len(lv.offsets) and lv.counts[idx]:
                jobs.append((tr, tc, idx))

    def fetch(job):
        _tr, _tc, idx = job
        return job, decode_tile(rf.get(lv.offsets[idx], lv.counts[idx]), c, lv)

    if len(jobs) > 1 and workers > 1:
        with ThreadPoolExecutor(min(workers, len(jobs))) as ex:
            done = list(ex.map(fetch, jobs))
    else:
        done = [fetch(j) for j in jobs]
    for (tr, tc, _), tile in done:
        tx0, ty0 = tc * lv.tile_w, tr * lv.tile_h
        xa, xb = max(col0, tx0), min(c1, tx0 + lv.tile_w, lv.width)
        ya, yb = max(row0, ty0), min(r1, ty0 + lv.tile_h, lv.height)
        out[ya - row0:yb - row0, xa - col0:xb - col0] = tile[ya - ty0:yb - ty0, xa - tx0:xb - tx0]
    return out


class HeaderCache:
    """Parsed COG headers by URL (query string stripped), LRU-bounded, thread-safe."""

    def __init__(self, max_items: int = 256) -> None:
        self._d: OrderedDict[str, Cog] = OrderedDict()
        self._lock = threading.Lock()
        self.max_items = max_items

    def get(self, url: str, rf: Reader) -> Cog:
        key = url.split("?", 1)[0]
        with self._lock:
            c = self._d.get(key)
            if c is not None:
                self._d.move_to_end(key)
                return c
        c = parse_cog(rf)
        with self._lock:
            self._d[key] = c
            while len(self._d) > self.max_items:
                self._d.popitem(last=False)
        return c
