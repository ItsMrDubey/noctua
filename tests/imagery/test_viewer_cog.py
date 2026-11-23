"""Imagery viewer COG reader: levels, float predictor, BigTIFF, JPEG tiles, windows."""

import io
import struct
import zlib

import numpy as np
import pytest

from osint.imagery.cog import (
    Cog,
    HeaderCache,
    Level,
    _unpredict_float,
    decode_tile,
    parse_cog,
    read_window,
)
from osint.imagery.geo import bbox_around, metres_per_degree, utm_forward, utm_forward_np

from .util import MemFile, build_tiff


def test_parse_levels_and_read_overview_window():
    px = lambda x, y: ((x * 3 + y) % 256, (x + y * 7) % 256, 5)
    mf = MemFile(build_tiff(20, 12, 8, px, origin=(500000.0, 5600000.0)))
    c = parse_cog(mf)
    assert len(c.levels) == 1 and c.levels[0].width == 20
    assert (c.samples, c.bits, c.predictor, c.epsg) == (3, 8, 2, 32635)
    w = read_window(mf, c, 0, 6, 6, 6, 4)
    assert w.shape == (4, 6, 3)
    assert tuple(w[0, 0]) == px(6, 6) and tuple(w[3, 5]) == px(11, 9)
    # outside the raster: zeros, no fetch
    mf.gets.clear()
    assert not read_window(mf, c, 0, 100, 100, 4, 4).any() and not mf.gets


def test_pick_level_uses_coarsest_level_finer_than_target():
    lv = lambda w: Level(w, w, 256, 256, [], [])
    c = Cog(16, 1, 1, 8, 2, "<", [lv(1000), lv(500), lv(250)], pixel_size=(10.0, 10.0))
    assert c.level_pixel(2) == (40.0, 40.0)
    assert c.pick_level(10) == 0 and c.pick_level(25) == 1 and c.pick_level(100) == 2


def _fp_predict(values: np.ndarray) -> bytes:
    """Encoder of TIFF predictor 3 (one sample per pixel): big-endian byte planes, then
    byte-wise horizontal differencing per row."""
    _h, w = values.shape
    out = bytearray()
    for row in values.astype(">f4"):
        b = np.frombuffer(row.tobytes(), np.uint8).reshape(w, 4).T.reshape(-1)
        d = np.diff(b.astype(np.int16), prepend=0).astype(np.uint8)
        out += d.tobytes()
    return bytes(out)


def test_float_predictor_round_trip():
    vals = np.array([[0.5, 1.25, -3.0, 1e-3], [7.0, 0.0, 2.5, 100.0]], np.float32)
    got = _unpredict_float(_fp_predict(vals), 2, 4, 1, 4)
    assert got.shape == (2, 4, 1)
    assert np.allclose(got[..., 0], vals)
    c = Cog(32, 1, 3, 8, 3, "<", [Level(4, 2, 4, 2, [0], [1])])
    tile = decode_tile(zlib.compress(_fp_predict(vals)), c, c.levels[0])
    assert tile.dtype == np.float32 and np.allclose(tile[..., 0], vals)


def test_integer_predictor_wraps_like_encoder():
    vals = np.array([[65000, 100, 65535, 3]], np.uint16)
    diff = np.diff(vals.astype(np.int64), prepend=0, axis=1) % 65536
    c = Cog(16, 1, 1, 1, 2, "<", [Level(4, 1, 4, 1, [0], [1])])
    tile = decode_tile(diff.astype("<u2").tobytes(), c, c.levels[0])
    assert tile[0, :, 0].tolist() == vals[0].tolist()


def _bigtiff(width: int, height: int, tile: int, data: np.ndarray) -> bytes:
    """Minimal little-endian BigTIFF, uint8 single band, deflate, one tile."""
    raw = zlib.compress(data.astype(np.uint8).tobytes())
    tile_off = 16
    entries = [(256, 4, 1, width), (257, 4, 1, height), (258, 3, 1, 8), (259, 3, 1, 8),
               (277, 3, 1, 1), (322, 3, 1, tile), (323, 3, 1, tile),
               (324, 16, 1, tile_off), (325, 16, 1, len(raw))]
    ifd_off = tile_off + len(raw)
    body = struct.pack("<Q", len(entries))
    for tag, typ, cnt, val in entries:
        body += struct.pack("<HHQ", tag, typ, cnt) + struct.pack("<Q", val)
    body += struct.pack("<Q", 0)
    return b"II" + struct.pack("<HHHQ", 43, 8, 0, ifd_off) + raw + body


def test_bigtiff_header_and_window():
    data = (np.arange(64).reshape(8, 8) * 3).astype(np.uint8)
    mf = MemFile(_bigtiff(8, 8, 8, data))
    c = parse_cog(mf)
    assert (c.levels[0].width, c.levels[0].offsets[0]) == (8, 16)
    w = read_window(mf, c, 0, 2, 3, 3, 2)
    assert w[..., 0].tolist() == data[3:5, 2:5].tolist()


def test_jpeg_tile_with_shared_tables():
    pil = pytest.importorskip("PIL.Image")
    img = np.zeros((16, 16, 3), np.uint8)
    img[:, :8] = (200, 40, 40)
    img[:, 8:] = (30, 60, 220)
    buf = io.BytesIO()
    pil.fromarray(img).save(buf, "JPEG", quality=95)
    full = buf.getvalue()
    # an "abbreviated" stream: tables in JPEGTables, tile = SOI + the rest (here: whole file,
    # which the splice must keep decodable)
    c = Cog(8, 3, 1, 7, 1, "<", [Level(16, 16, 16, 16, [0], [len(full)], jpeg_tables=b"\xff\xd8\xff\xd9")])
    tile = decode_tile(full, c, c.levels[0])
    assert tile.shape == (16, 16, 3)
    assert abs(int(tile[8, 2, 0]) - 200) < 20 and abs(int(tile[8, 13, 2]) - 220) < 20


def test_header_cache_strips_query():
    mf = MemFile(build_tiff(8, 8, 8, lambda x, y: (1, 2, 3)))
    hc = HeaderCache()
    a = hc.get("https://x.test/a.tif?sig=1", mf)
    n = len(mf.gets)
    assert hc.get("https://x.test/a.tif?sig=2", mf) is a and len(mf.gets) == n


def test_utm_vectorised_matches_scalar_and_bbox():
    lat = np.array([47.76, 50.45, -33.9]); lon = np.array([39.93, 30.52, 18.4])
    for epsg, i in ((32637, 0), (32636, 1), (32734, 2)):
        e, n = utm_forward_np(lat[i:i + 1], lon[i:i + 1], epsg)
        se, sn = utm_forward(float(lat[i]), float(lon[i]), epsg)
        assert abs(e[0] - se) < 1e-6 and abs(n[0] - sn) < 1e-6
    w, s, e, n = bbox_around(47.8, 39.86, 2.5)
    mx, my = metres_per_degree(47.8)
    assert abs((e - w) * mx - 5000) < 1e-6 and abs((n - s) * my - 5000) < 1e-6
