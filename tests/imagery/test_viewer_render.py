"""Imagery viewer rendering: band maths, stretch, resampling, readers, cache."""

import io
import json

import httpx
import numpy as np
import pytest
import respx

from osint.imagery import render as R
from osint.imagery.catalog import PC_DATA, scene
from osint.imagery.cog import HeaderCache
from osint.imagery.viewer import ViewerService

from .util import scene_tiff

LAT, LON = 50.45466, 30.5238


def test_make_grid_native_and_capped():
    g = R.make_grid(LAT, LON, 5, 10.0)
    assert (g.width, g.height) == (500, 500) and abs(g.m_per_px() - 10.0) < 1e-6
    assert R.make_grid(LAT, LON, 5, 10.0, "thumb").width == R.THUMB_PX
    big = R.make_grid(LAT, LON, 5, 0.3)
    assert big.width == R.MAX_FULL_PX and big.m_per_px() > 0.3  # downsampled, and says so
    assert R.make_grid(LAT, LON, 999, 10.0).width == R.MAX_KM * 100  # window clamped


def test_stretch_percentiles_and_rgba_alpha():
    a = np.array([[0.0, 0.5], [1.0, np.nan]], np.float32)
    s = R.stretch(a, 0.0, 1.0)
    assert s[0, 1] == 0.5 and np.isnan(s[1, 1])
    lo, hi = R.percentiles(np.arange(101, dtype=np.float32), 2, 98)
    assert (lo, hi) == (2.0, 98.0)
    rgba = R.to_rgba(np.stack([s, s, s], axis=2))
    assert rgba[1, 1, 3] == 0 and rgba[0, 0, 3] == 255 and rgba[1, 0, 0] == 255


def test_true_colour_clamps_upper_bound_for_clouds():
    r = np.full((10, 10), 0.08, np.float32)
    r[:5] = 0.9  # half the window is cloud
    rgb, st = R.true_colour(r, r, r)
    assert st["hi"] == 0.6 and rgb.shape == (10, 10, 3)


def test_nbr_dnbr_classes():
    nir, sw = np.array([[0.4, 0.3]]), np.array([[0.1, 0.3]])
    assert np.allclose(R.nbr(nir, sw), [[0.6, 0.0]])
    d = np.array([[-0.3, 0.0, 0.2, 0.5, 0.8, np.nan]])
    rgb, st = R.dnbr_rgb(d, np.full(d.shape, 0.5))
    c = st["classes_pct"]
    assert c["high regrowth"] == c["unburned"] == c["low severity"] == c["high severity"] == 20.0
    assert np.allclose(rgb[0, 1], 0.425)  # unburned stays grey
    assert np.allclose(rgb[0, 4], np.array([165, 0, 38]) / 255) and np.isnan(rgb[0, 5]).all()


def test_boxcar_nan_aware_and_sar_change():
    a = np.ones((5, 5), np.float32)
    a[2, 2] = 10.0
    a[0, 0] = np.nan
    b = R.boxcar(a, 3)
    assert np.isnan(b[0, 0]) and abs(b[2, 2] - 2.0) < 1e-6 and abs(b[1, 1] - 17 / 8) < 1e-6  # NaN neighbour skipped
    r_db = np.array([[6.0, -6.0, 0.0, np.nan]])
    rgb, st = R.change_rgb(r_db, np.full(r_db.shape, 0.5))
    assert st["increase_pct"] == st["decrease_pct"] == pytest.approx(33.33, abs=0.01)
    assert rgb[0, 0, 0] > rgb[0, 0, 2] and rgb[0, 1, 2] > rgb[0, 1, 0] and np.isnan(rgb[0, 3]).all()
    assert R.sar_db(np.full((3, 3), 0.1), 1)[1, 1] == pytest.approx(-10.0)
    assert R.sar_power(np.array([10.0, 0.0]), calibrated=False)[0] == 100.0


def _serve(content: bytes):
    def handler(request):
        a, b = (int(x) for x in request.headers["Range"].removeprefix("bytes=").split("-"))
        return httpx.Response(206, content=content[a:b + 1])
    return handler


@respx.mock
def test_sample_cog_resamples_utm_onto_latlon_grid():
    data = scene_tiff(LAT, LON, size=80, tile=32, pixels=lambda x, y: (x + 1, y + 1, 7))
    respx.get("https://cogs.test/s.tif").mock(side_effect=_serve(data))
    g = R.make_grid(LAT, LON, 0.3, 10.0)  # 30 x 30 px around the centre
    with httpx.Client() as c:
        arr, px = R.sample_cog(c, HeaderCache(), "https://cogs.test/s.tif", g, 32635)
    assert arr.shape == (30, 30, 3) and px == 10.0
    # centre of the output sits on the centre of the 80 px raster; x grows east, y south
    assert abs(arr[15, 15, 0] - 41) < 1.5 and abs(arr[15, 15, 1] - 41) < 1.5
    assert arr[15, 25, 0] > arr[15, 5, 0] and arr[25, 15, 1] > arr[5, 15, 1]
    assert np.allclose(arr[..., 2], 7)


def _npy(arr: np.ndarray) -> bytes:
    buf = io.BytesIO()
    np.save(buf, arr)
    return buf.getvalue()


@respx.mock
def test_titiler_npy_masks_and_sar_render():
    vv = np.full((4, 4), 0.1, np.float32)
    vh = np.full((4, 4), 0.02, np.float32)
    mask = np.full((4, 4), 255, np.float32)
    mask[0, 0] = 0
    route = respx.get(url__startswith=f"{PC_DATA}/item/bbox/").mock(
        return_value=httpx.Response(200, content=_npy(np.stack([vv, vh, mask]))))
    sc = scene("s1", "S1C_X_rtc", "sentinel-1-rtc", "2026-11-19T16:37:59Z", renderer="titiler",
               extra={"calibrated": True, "polarizations": ["vv", "vh"]})
    g = R.Grid(30.0, 50.0, 30.01, 50.01, 4, 4)
    with httpx.Client() as c:
        d = R.titiler_npy(c, sc, ["vv", "vh"], g)
        assert np.isnan(d[0, 0, 0]) and d[1, 1, 1] == pytest.approx(0.02)
        rgba, meta = R.Renderer().render(c, sc, "sar_rgb", g)
    q = route.calls[0].request.url
    assert "/4x4.npy" in str(q) and q.params.get_list("assets") == ["vv", "vh"]
    assert rgba.shape == (4, 4, 4) and rgba[0, 0, 3] == 0 and meta["calibrated"] is True
    with pytest.raises(R.RenderError):  # a change product needs a second scene
        R.Renderer().render(None, sc, "sar_change", g)


def test_render_rejects_mismatched_pairs():
    a = scene("s2", "A", "sentinel-2-l2a", "2026-11-01T00:00:00Z")
    b = scene("s1", "B", "sentinel-1-rtc", "2026-11-02T00:00:00Z")
    g = R.Grid(30.0, 50.0, 30.01, 50.01, 4, 4)
    with pytest.raises(R.RenderError, match="same sensor"):
        R.Renderer().render(None, a, "dnbr", g, b)
    with pytest.raises(R.RenderError, match="not available"):
        R.Renderer().render(None, a, "sar", g)


def test_encode_webp_and_png():
    rgba = np.zeros((4, 4, 4), np.uint8)
    rgba[..., 3] = 255
    data, media = R.encode(rgba, "png")
    assert media == "image/png" and data[:4] == b"\x89PNG"
    data, media = R.encode(rgba)
    assert media == "image/webp" and data[8:12] == b"WEBP"


class _FakeRenderer:
    calls = 0

    def render(self, client, sc, preset, grid, other=None):
        type(self).calls += 1
        rgba = np.full((grid.height, grid.width, 4), 200, np.uint8)
        return rgba, {"preset": preset, "label": "True colour"}


def test_viewer_service_caches_renders_and_trims(tmp_path):
    svc = ViewerService(tmp_path, client_factory=httpx.Client)
    svc.renderer = _FakeRenderer()
    sc = scene("s2", "S2B_X", "sentinel-2-l2a", "2026-11-13T09:31:39Z", gsd_m=10.0)
    wb = scene("wayback", "123", "world-imagery-wayback", "2024-03-29T00:00:00Z", renderer="tiles")
    svc.catalogue.register([sc, wb])
    m1 = svc.render(sc["key"], "true", LAT, LON, 1.0, "thumb")
    m2 = svc.render(sc["key"], "true", LAT, LON, 1.0, "thumb")
    assert _FakeRenderer.calls == 1 and m2["cached"] and m1["file"] == m2["file"]
    assert m1["px"] == [100, 100] and m1["attribution"].endswith("2026")
    meta = json.loads((svc.dir / m1["file"].replace(".webp", ".json")).read_text())
    assert meta["bounds"]["west"] < LON < meta["bounds"]["east"]
    with pytest.raises(R.RenderError, match="Wayback"):
        svc.render(wb["key"], "true", LAT, LON, 1.0)
    with pytest.raises(R.RenderError, match="unknown scene"):
        svc.render("s2:nope", "true", LAT, LON, 1.0)
    svc.max_bytes = 0
    svc.trim()
    assert not list(svc.dir.glob("*.*"))
