import math
import struct
import zlib

import pytest

from osint.imagery import png
from osint.imagery.geo import K0, pixel_window, utm_forward, utm_zone_params, window_bounds


def test_zone_params():
    assert utm_zone_params(32635) == (27.0, False)
    assert utm_zone_params(32736) == (33.0, True)
    with pytest.raises(ValueError):
        utm_zone_params(4326)


def test_equator_on_central_meridian():
    e, n = utm_forward(0.0, 27.0, 32635)
    assert e == pytest.approx(500000.0, abs=1e-6)
    assert n == pytest.approx(0.0, abs=1e-6)


def _meridian_arc(lat_deg):
    a, e2 = 6378137.0, 0.0066943799901413165

    def f(p):
        return a * (1 - e2) / (1 - e2 * math.sin(p) ** 2) ** 1.5

    n, p1 = 20000, math.radians(lat_deg)
    h = p1 / n
    return h / 3 * (f(0) + f(p1) + sum((4 if i % 2 else 2) * f(i * h) for i in range(1, n)))


def test_northing_on_central_meridian_matches_meridian_arc():
    for lat in (30.0, 45.0, 60.0):
        _, n = utm_forward(lat, 27.0, 32635)
        assert n == pytest.approx(K0 * _meridian_arc(lat), abs=0.01)


def test_known_points():
    # 3 degrees off the central meridian at 45N: easting ~ 500000 + 3 deg * 78.8 km * k0
    e, n = utm_forward(45.0, 30.0, 32635)
    assert e == pytest.approx(736_570, abs=1500)
    assert n == pytest.approx(4_990_000, abs=3000)
    # Kyiv sits inside the Sentinel-2 tile 35UQR (origin 699960E, 5600040N, 109.8 km square)
    e, n = utm_forward(50.45466, 30.5238, 32635)
    assert 699960 < e < 809760 and 5490240 < n < 5600040
    # southern hemisphere carries the 10,000 km false northing
    _, n = utm_forward(-33.9, 18.4, 32734)
    assert 6_200_000 < n < 6_300_000
    # symmetry across the central meridian
    e1, n1 = utm_forward(40.0, 30.0, 32635)
    e2, n2 = utm_forward(40.0, 24.0, 32635)
    assert e1 - 500000 == pytest.approx(500000 - e2, abs=1e-3)
    assert n1 == pytest.approx(n2, abs=1e-3)


def test_pixel_window_math():
    # top-left (1000, 5000), 10 m pixels, point at (1250, 4750) -> pixel (25, 25)
    col, row, w, h = pixel_window(1250, 4750, 1000, 5000, 10, 10, 100)
    assert (w, h) == (20, 20)
    assert (col, row) == (15, 15)
    assert pixel_window(0, 0, 0, 0, 30, 30, 2500)[2:] == (167, 167)  # 30 m pixels


def test_window_bounds():
    b = window_bounds(60.0, 10.0, 2.5)
    assert b["north"] - b["south"] == pytest.approx(5000 / 111320 * 2 / 2, rel=1e-6) \
        or b["north"] - b["south"] == pytest.approx(2 * 2500 / 111320, rel=1e-6)
    assert (b["east"] - b["west"]) > (b["north"] - b["south"])  # lon degrees shrink with lat


def test_png_roundtrip_and_structure():
    w, h = 5, 4
    rgb = bytes((x * 40 + y * 3 + c) % 256 for y in range(h) for x in range(w) for c in range(3))
    data = png.encode_rgb(w, h, rgb)
    assert data[:8] == png.SIGNATURE
    length, kind = struct.unpack(">I4s", data[8:16])
    assert kind == b"IHDR" and length == 13
    assert struct.unpack(">IIBBBBB", data[16:29]) == (w, h, 8, 2, 0, 0, 0)
    assert struct.unpack(">I", data[29:33])[0] == zlib.crc32(data[12:29]) & 0xFFFFFFFF
    assert png.decode_rgb(data) == (w, h, rgb)
    with pytest.raises(ValueError):
        png.encode_rgb(2, 2, b"123")


def test_png_decoder_handles_filters():
    w, h = 4, 3
    rows = [bytes(range(i, i + 12)) for i in (0, 20, 50)]
    raw = b"\x00" + rows[0]
    raw += b"\x02" + bytes((rows[1][i] - rows[0][i]) % 256 for i in range(12))  # Up
    raw += b"\x01" + bytes((rows[2][i] - (rows[2][i - 3] if i >= 3 else 0)) % 256
                           for i in range(12))  # Sub
    ihdr = struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0)

    def chunk(k, b):
        return struct.pack(">I", len(b)) + k + b + struct.pack(">I", zlib.crc32(k + b))

    data = (png.SIGNATURE + chunk(b"IHDR", ihdr) + chunk(b"IDAT", zlib.compress(raw))
            + chunk(b"IEND", b""))
    assert png.decode_rgb(data) == (w, h, b"".join(rows))


def test_change_score():
    a = bytes([100, 100, 100] * 4)
    assert png.change_score(a, a) == 0.0
    assert png.change_score(a, bytes([0, 0, 0] * 4)) is None  # all nodata
    b = bytes([200, 100, 100] * 4)
    assert png.change_score(a, b) == pytest.approx(100 / (3 * 255))
    c = bytes([0, 0, 0]) + bytes([200, 100, 100] * 3)  # nodata pixels are ignored
    assert png.change_score(a, c) == pytest.approx(100 / (3 * 255))
