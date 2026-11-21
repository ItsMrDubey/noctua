from array import array

import httpx
import pytest
import respx

from osint.imagery.tiff import RangeFile, Unsupported, parse_tiff, read_window

from .util import MemFile, build_tiff


def px(x, y):
    return ((x * 7 + y) % 256, (x + y * 5) % 256, (x * y) % 256)


def test_parse_ifd_and_georef():
    data = build_tiff(20, 12, 8, px, origin=(500000.0, 5600000.0), pixel=(10.0, 10.0))
    t = parse_tiff(MemFile(data))
    assert (t.width, t.height, t.tile_w, t.tile_h) == (20, 12, 8, 8)
    assert (t.bits, t.samples, t.compression, t.predictor) == (8, 3, 8, 2)
    assert len(t.offsets) == len(t.counts) == 3 * 2
    assert t.origin == (500000.0, 5600000.0)
    assert t.pixel_size == (10.0, 10.0)
    assert t.epsg == 32635


def test_window_matches_source_pixels_and_fetches_only_needed_tiles():
    mf = MemFile(build_tiff(20, 12, 8, px))
    t = parse_tiff(mf)
    win = read_window(mf, t, 2, 1, 5, 4)  # entirely inside tile (0, 0)
    assert len(mf.gets) == 1
    for y in range(4):
        for x in range(5):
            i = (y * 5 + x) * 3
            assert tuple(win.data[i:i + 3]) == px(2 + x, 1 + y)
    mf.gets.clear()
    win = read_window(mf, t, 6, 6, 6, 4)  # spans 2x2 tiles
    assert len(mf.gets) == 4
    assert tuple(win.data[0:3]) == px(6, 6)
    assert tuple(win.data[(3 * 6 + 5) * 3:][:3]) == px(11, 9)


def test_window_pads_outside_raster_and_edge_tiles():
    mf = MemFile(build_tiff(20, 12, 8, px))
    t = parse_tiff(mf)
    win = read_window(mf, t, 16, 8, 8, 8)  # right/bottom edge: only 4x4 valid
    assert tuple(win.data[0:3]) == px(16, 8)
    assert tuple(win.data[4 * 3:][:3]) == (0, 0, 0)
    assert tuple(win.data[(5 * 8) * 3:][:3]) == (0, 0, 0)
    assert not any(read_window(mf, t, 100, 100, 4, 4).data)
    neg = read_window(mf, t, -2, -2, 4, 4)
    assert tuple(neg.data[(2 * 4 + 2) * 3:][:3]) == px(0, 0)


@pytest.mark.parametrize("compression,predictor", [(1, 1), (8, 2)])
def test_16bit(compression, predictor):
    data = build_tiff(8, 8, 8, lambda x, y: (x * 1000 + y,), spp=1, bits=16,
                      compression=compression, predictor=predictor)
    mf = MemFile(data)
    a = array("H")
    a.frombytes(read_window(mf, parse_tiff(mf), 0, 0, 8, 8).data)
    assert a[3 * 8 + 5] == 5 * 1000 + 3


def test_unsupported_inputs():
    with pytest.raises(Unsupported):
        parse_tiff(MemFile(b"notatiffnotatiff"))
    with pytest.raises(Unsupported):
        parse_tiff(MemFile(b"II+\x00" + bytes(20)))  # BigTIFF
    with pytest.raises(Unsupported):
        parse_tiff(MemFile(build_tiff(8, 8, 8, px, compression=7)))  # JPEG tiles


@respx.mock
def test_rangefile_http_ranges():
    data = build_tiff(20, 12, 8, px)
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        rng = request.headers["Range"]
        seen.append(rng)
        a, b = (int(x) for x in rng.removeprefix("bytes=").split("-"))
        return httpx.Response(206, content=data[a:b + 1])

    respx.get("https://cog.test/x.tif").mock(side_effect=handler)
    with httpx.Client() as c:
        rf = RangeFile(c, "https://cog.test/x.tif")
        t = parse_tiff(rf)
        assert len(seen) == 1  # whole tiny file fits one header block
        win = read_window(rf, t, 0, 0, 4, 4)
    assert tuple(win.data[0:3]) == px(0, 0)
    assert len(seen) == 2 and seen[1].startswith("bytes=8-")  # tile 0 follows the header
