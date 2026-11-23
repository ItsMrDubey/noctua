"""Render a scene window for the imagery viewer: band presets, percentile stretch, lat/lon grid.

Every render lands on the same kind of output grid: a north-up lat/lon (EPSG:4326) raster
over a square window around the site, sized at the scene's native ground resolution (capped
at ``MAX_FULL_PX`` per side; the true metres per output pixel are reported). A lat/lon grid
drapes exactly onto the globe as a single rectangle, and two scenes rendered for the same
window line up pixel for pixel in the compare views.

* Sentinel-2 (Earth Search) and Maxar Open Data COGs are read directly (:mod:`cog`), at the
  coarsest overview that still matches the output pixel size, and resampled (bilinear) from
  their UTM grid onto the output grid.
* Planetary Computer scenes (Landsat, Sentinel-1 RTC / GRD) are read through its public
  TiTiler ``bbox`` endpoint as raw ``.npy`` arrays on the same grid (it signs the blob
  URLs, reads the right overview and, for GRD, georeferences from the scene's GCPs).

Presets (band math on surface reflectance / backscatter, then display stretch):
``true`` natural colour; ``swir`` SWIR-2 / SWIR-1 / NIR-narrow (S2 B12-B11-B8A): active fire
and hot burn scars glow, smoke is transparent; ``nir`` NIR false colour (vegetation red);
``nbr`` normalised burn ratio; ``dnbr`` burn severity between two scenes (USGS classes);
``sar`` / ``sarvh`` VV / VH backscatter in dB; ``sar_rgb`` dual-pol composite (VV, VH,
VV/VH); ``sar_change`` log-ratio (dB) change between two dates, red = brighter, cyan =
darker. Radar is lightly speckle-filtered (3x3 boxcar on power; 5x5 for change).
"""

import io
import math
from dataclasses import dataclass

import httpx
import numpy as np

from osint.imagery import png
from osint.imagery.catalog import PC_DATA
from osint.imagery.cog import HeaderCache, read_window
from osint.imagery.geo import bbox_around, metres_per_degree, utm_forward_np
from osint.imagery.tiff import RangeFile, Unsupported

MAX_FULL_PX = 3072
THUMB_PX = 320
MIN_KM, MAX_KM = 0.25, 25.0

PRESETS: dict[str, dict] = {
    "true": {"label": "True colour", "pair": False},
    "swir": {"label": "SWIR / fire (SWIR2-SWIR1-NIR)", "pair": False},
    "nir": {"label": "NIR false colour (vegetation red)", "pair": False},
    "nbr": {"label": "NBR burn ratio", "pair": False},
    "dnbr": {"label": "dNBR burn severity (2 scenes)", "pair": True},
    "sar": {"label": "SAR VV (dB)", "pair": False},
    "sarvh": {"label": "SAR VH (dB)", "pair": False},
    "sar_rgb": {"label": "SAR dual-pol (VV, VH, VV/VH)", "pair": False},
    "sar_change": {"label": "SAR change (log-ratio, 2 dates)", "pair": True},
}
# preset -> asset names per optical source
OPTICAL = {
    "s2": {"true": ("red", "green", "blue"), "swir": ("swir22", "swir16", "nir08"),
           "nir": ("nir", "red", "green"), "nbr": ("nir08", "swir22"), "dnbr": ("nir08", "swir22")},
    "landsat": {"true": ("red", "green", "blue"), "swir": ("swir22", "swir16", "nir08"),
                "nir": ("nir08", "red", "green"), "nbr": ("nir08", "swir22"),
                "dnbr": ("nir08", "swir22")},
}
# USGS FIREMON dNBR severity classes: (upper bound, RGB, label)
DNBR_CLASSES = (
    (-0.25, (26, 152, 80), "high regrowth"), (-0.1, (145, 207, 96), "low regrowth"),
    (0.1, None, "unburned"), (0.27, (255, 255, 51), "low severity"),
    (0.44, (253, 174, 97), "moderate-low"), (0.66, (244, 109, 67), "moderate-high"),
    (9.0, (165, 0, 38), "high severity"),
)
SCL_CLOUD = (3, 8, 9, 10)  # cloud shadow, cloud medium / high probability, thin cirrus
SCL_VALID_MIN = 1


class RenderError(Exception):
    """The scene cannot be rendered for this window / preset (message is user-facing)."""


@dataclass
class Grid:
    west: float
    south: float
    east: float
    north: float
    width: int
    height: int

    @property
    def bbox(self) -> tuple[float, float, float, float]:
        return (self.west, self.south, self.east, self.north)

    def m_per_px(self) -> float:
        mx, _ = metres_per_degree((self.south + self.north) / 2)
        return (self.east - self.west) * mx / self.width

    def centres(self) -> tuple[np.ndarray, np.ndarray]:
        """(lat, lon) 2-D arrays of pixel centres."""
        lons = self.west + (np.arange(self.width) + 0.5) * (self.east - self.west) / self.width
        lats = self.north - (np.arange(self.height) + 0.5) * (self.north - self.south) / self.height
        return np.meshgrid(lats, lons, indexing="ij")


def make_grid(lat: float, lon: float, km: float, gsd_m: float, size: str = "full") -> Grid:
    """Square window of side ``km`` (clamped) at native ``gsd_m`` (or thumbnail size)."""
    km = min(MAX_KM, max(MIN_KM, km))
    w, s, e, n = bbox_around(lat, lon, km / 2)
    native = max(1, round(km * 1000 / max(0.1, gsd_m)))
    px = min(native, THUMB_PX if size == "thumb" else MAX_FULL_PX)
    return Grid(w, s, e, n, px, px)


# ------------------------------------------------------------------ readers

def _bilinear(img: np.ndarray, col: np.ndarray, row: np.ndarray) -> np.ndarray:
    """Sample (h, w, c) ``img`` at fractional pixel-centre coords; NaN outside / nodata."""
    h, w, _c = img.shape
    x0 = np.floor(col).astype(np.int64)
    y0 = np.floor(row).astype(np.int64)
    fx = (col - x0)[..., None].astype(np.float32)
    fy = (row - y0)[..., None].astype(np.float32)
    x0c, x1c = np.clip(x0, 0, w - 1), np.clip(x0 + 1, 0, w - 1)
    y0c, y1c = np.clip(y0, 0, h - 1), np.clip(y0 + 1, 0, h - 1)
    a, b = img[y0c, x0c], img[y0c, x1c]
    cc, d = img[y1c, x0c], img[y1c, x1c]
    out = (a * (1 - fx) + b * fx) * (1 - fy) + (cc * (1 - fx) + d * fx) * fy
    outside = (col < -0.5) | (row < -0.5) | (col > w - 0.5) | (row > h - 0.5)
    out[outside] = np.nan
    return out


def sample_cog(client: httpx.Client, headers: HeaderCache, url: str, grid: Grid,
               epsg_hint: int | None = None, zero_is_nodata: bool = True) -> tuple[np.ndarray, float]:
    """A COG resampled onto ``grid`` -> ((h, w, samples) float32 with NaN nodata, source m/px)."""
    rf = RangeFile(client, url)
    cog = headers.get(url, rf)
    epsg = cog.epsg or epsg_hint
    if not epsg or cog.origin is None or cog.pixel_size is None:
        raise Unsupported("scene is not georeferenced in UTM")
    lvl = cog.pick_level(grid.m_per_px())
    px, py = cog.level_pixel(lvl)
    lat, lon = grid.centres()
    e, n = utm_forward_np(lat, lon, int(epsg))
    col = (e - cog.origin[0]) / px - 0.5
    row = (cog.origin[1] - n) / py - 0.5
    c0, r0 = math.floor(np.nanmin(col)) - 1, math.floor(np.nanmin(row)) - 1
    c1, r1 = math.ceil(np.nanmax(col)) + 2, math.ceil(np.nanmax(row)) + 2
    if (c1 - c0) * (r1 - r0) > 64e6:
        raise RenderError("window too large for this scene")
    win = read_window(rf, cog, lvl, c0, r0, c1 - c0, r1 - r0).astype(np.float32)
    if cog.nodata is not None and not math.isnan(cog.nodata):
        win[win == cog.nodata] = np.nan
    if zero_is_nodata:
        win[(win == 0).all(axis=2)] = np.nan
    return _bilinear(win, col - c0, row - r0), px


def titiler_npy(client: httpx.Client, scene: dict, assets: list[str], grid: Grid) -> np.ndarray:
    """Planetary Computer TiTiler bbox read -> (len(assets), h, w) float32, NaN = masked."""
    params = [("collection", scene["collection"]), ("item", scene["id"]),
              ("resampling", "bilinear")] + [("assets", a) for a in assets]
    url = (f"{PC_DATA}/item/bbox/{grid.west:.7f},{grid.south:.7f},{grid.east:.7f},"
           f"{grid.north:.7f}/{grid.width}x{grid.height}.npy")
    r = client.get(url, params=params)
    if r.status_code in (404, 422):
        raise RenderError(f"Planetary Computer tiler: {r.text[:200]}")
    r.raise_for_status()
    arr = np.load(io.BytesIO(r.content), allow_pickle=False)
    if arr.ndim != 3 or arr.shape[0] < len(assets):
        raise RenderError(f"unexpected tiler array {arr.shape}")
    data = arr[:len(assets)].astype(np.float32)
    if arr.shape[0] > len(assets):
        data[:, arr[-1] == 0] = np.nan
    return data


# ------------------------------------------------------------------ stretch / colour

def _valid(a: np.ndarray) -> np.ndarray:
    v = a[np.isfinite(a)]
    return v if v.size else np.zeros(1, np.float32)


def stretch(a: np.ndarray, lo: float, hi: float, gamma: float = 1.0) -> np.ndarray:
    """Linear stretch to 0..1 between lo and hi, then gamma (NaN stays NaN)."""
    x = np.clip((a - lo) / max(1e-9, hi - lo), 0, 1)
    return x ** (1 / gamma) if gamma != 1.0 else x


def percentiles(a: np.ndarray, lo_p: float, hi_p: float) -> tuple[float, float]:
    v = _valid(a)
    lo, hi = np.percentile(v, [lo_p, hi_p])
    return float(lo), float(hi if hi > lo else lo + 1e-6)


def to_rgba(rgb01: np.ndarray, valid: np.ndarray | None = None) -> np.ndarray:
    """(h, w, 3) floats 0..1 (NaN = nodata) -> (h, w, 4) uint8 with transparent nodata."""
    nan = ~np.isfinite(rgb01).all(axis=2)
    if valid is not None:
        nan |= ~valid
    out = np.zeros(rgb01.shape[:2] + (4,), np.uint8)
    out[..., :3] = np.round(np.nan_to_num(rgb01, nan=0.0) * 255).clip(0, 255).astype(np.uint8)
    out[..., 3] = np.where(nan, 0, 255)
    return out


def boxcar(a: np.ndarray, k: int) -> np.ndarray:
    """NaN-aware k x k mean filter (integral images)."""
    if k <= 1:
        return a
    m = np.isfinite(a)
    v = np.where(m, a, 0.0).astype(np.float64)
    pad = k // 2

    def box(x: np.ndarray) -> np.ndarray:
        x = np.pad(x, pad, mode="edge")
        c = x.cumsum(0).cumsum(1)
        c = np.pad(c, ((1, 0), (1, 0)))
        return c[k:, k:] - c[:-k, k:] - c[k:, :-k] + c[:-k, :-k]

    s, n = box(v), box(m.astype(np.float64))
    out = np.where(n > 0, s / np.maximum(n, 1), np.nan)
    out[~m] = np.nan
    return out.astype(np.float32)


def true_colour(r: np.ndarray, g: np.ndarray, b: np.ndarray) -> tuple[np.ndarray, dict]:
    """Joint (colour-preserving) stretch of reflectance: 1st percentile to the 98th,
    the upper bound kept within 0.12-0.6 so haze is cut and clouds do not darken ground."""
    allv = np.concatenate([_valid(x) for x in (r, g, b)])
    lo = float(np.percentile(allv, 1))
    hi = float(np.clip(np.percentile(allv, 98), max(lo + 0.05, 0.12), 0.6))
    rgb = np.stack([stretch(x, lo, hi, 1.25) for x in (r, g, b)], axis=2)
    return rgb, {"lo": round(lo, 4), "hi": round(hi, 4), "gamma": 1.25, "mode": "joint p1-p98"}


def false_colour(bands: list[np.ndarray]) -> tuple[np.ndarray, dict]:
    """Per-band 1st-99.5th percentile stretch (small hot / bright features stay saturated)."""
    out, st = [], []
    for x in bands:
        lo, hi = percentiles(x, 1, 99.5)
        out.append(stretch(x, lo, hi))
        st.append([round(lo, 4), round(hi, 4)])
    return np.stack(out, axis=2), {"per_band": st, "mode": "per-band p1-p99.5"}


def nbr(nir: np.ndarray, swir2: np.ndarray) -> np.ndarray:
    with np.errstate(invalid="ignore", divide="ignore"):
        return (nir - swir2) / (nir + swir2)


def ramp(x: np.ndarray, stops: list[tuple[float, tuple[int, int, int]]]) -> np.ndarray:
    """Piecewise-linear colour ramp -> (h, w, 3) floats 0..1 (NaN stays NaN)."""
    xs = [s[0] for s in stops]
    out = np.stack([np.interp(x, xs, [s[1][i] / 255 for s in stops]) for i in range(3)], axis=2)
    out[~np.isfinite(x)] = np.nan
    return out


NBR_RAMP = [(-0.5, (84, 48, 5)), (-0.1, (191, 129, 45)), (0.1, (246, 232, 195)),
            (0.35, (128, 205, 193)), (0.7, (1, 102, 94))]


def dnbr_rgb(d: np.ndarray, grey: np.ndarray) -> tuple[np.ndarray, dict]:
    """USGS severity classes over a greyscale context image; unburned stays grey."""
    out = np.repeat(grey[..., None] * 0.85, 3, axis=2)
    counts = {}
    lower = -np.inf
    valid = np.isfinite(d)
    for upper, colour, label in DNBR_CLASSES:
        m = valid & (d >= lower) & (d < upper)
        counts[label] = round(float(m.sum()) * 100 / max(1, int(valid.sum())), 2)
        if colour is not None:
            out[m] = np.array(colour, np.float32) / 255
        lower = upper
    out[~valid] = np.nan
    return out, {"classes_pct": counts}


def sar_db(power: np.ndarray, k: int = 3) -> np.ndarray:
    p = boxcar(power, k)
    with np.errstate(invalid="ignore", divide="ignore"):
        db = 10 * np.log10(p)
    db[~np.isfinite(db)] = np.nan
    return db


def sar_power(a: np.ndarray, calibrated: bool) -> np.ndarray:
    """RTC is linear gamma0 power; GRD is amplitude DN (power ~ DN^2, uncalibrated)."""
    a = np.where(a > 0, a, np.nan)
    return a if calibrated else a.astype(np.float64) ** 2


def change_rgb(r_db: np.ndarray, grey: np.ndarray, thr: float = 3.0, full: float = 9.0
               ) -> tuple[np.ndarray, dict]:
    """Log-ratio change over a dimmed greyscale: red = backscatter up, cyan = down."""
    base = np.repeat(grey[..., None] * 0.6, 3, axis=2)
    up = np.clip((r_db - thr) / (full - thr), 0, 1)
    dn = np.clip((-r_db - thr) / (full - thr), 0, 1)
    up, dn = np.nan_to_num(up), np.nan_to_num(dn)
    red, cyan = np.array([1.0, 0.18, 0.2], np.float32), np.array([0.2, 0.9, 1.0], np.float32)
    a_up, a_dn = (up > 0)[..., None] * (0.55 + 0.45 * up[..., None]), (dn > 0)[..., None] * (0.55 + 0.45 * dn[..., None])
    out = base * (1 - a_up - a_dn) + red * a_up + cyan * a_dn
    out[~np.isfinite(r_db)] = np.nan
    valid = np.isfinite(r_db)
    nv = max(1, int(valid.sum()))
    return out, {"increase_pct": round(float((valid & (r_db > thr)).sum()) * 100 / nv, 2),
                 "decrease_pct": round(float((valid & (r_db < -thr)).sum()) * 100 / nv, 2),
                 "threshold_db": thr}


# ------------------------------------------------------------------ per-source band reads

class Renderer:
    """Turns (scene, preset, window) into an RGBA array + metadata. Thread-safe."""

    def __init__(self, headers: HeaderCache | None = None) -> None:
        self.headers = headers or HeaderCache()

    def optical_bands(self, client: httpx.Client, scene: dict, names: tuple[str, ...],
                      grid: Grid) -> tuple[list[np.ndarray], float | None]:
        """Surface reflectance planes on the grid (+ source m/px read)."""
        tiles = [scene, *scene.get("alts", [])]  # same pass, neighbouring tiles
        if scene["source"] == "landsat":
            out: list[np.ndarray] | None = None
            for t in tiles:
                data = titiler_npy(client, {**scene, **{k: t[k] for k in ("id", "collection")}},
                                   list(names), grid)
                planes = []
                for i, n in enumerate(names):
                    sc, off = t["assets"].get(n, {}).get("scale_offset", (2.75e-5, -0.2))
                    x = data[i]
                    x[x == 0] = np.nan
                    planes.append(x * sc + off)
                out = planes if out is None else [np.where(np.isfinite(a), a, b)
                                                  for a, b in zip(out, planes, strict=True)]
                if all(np.isfinite(a).all() for a in out):
                    break
            return out, scene["gsd_m"]
        from concurrent.futures import ThreadPoolExecutor

        def one(t: dict, n: str):
            a = t["assets"].get(n)
            if not a:
                raise RenderError(f"scene has no {n} band")
            epsg = t.get("epsg") if t is not scene else scene["extra"].get("epsg")
            arr, px = sample_cog(client, self.headers, a["href"], grid, epsg)
            sc, off = a.get("scale_offset", (1e-4, 0.0))
            return arr[..., 0] * sc + off, px

        planes: list[np.ndarray] | None = None
        src_px = None
        for t in tiles:
            with ThreadPoolExecutor(len(names)) as ex:
                res = list(ex.map(lambda n, t=t: one(t, n), names))
            got = [r[0] for r in res]
            src_px = min([r[1] for r in res] + ([src_px] if src_px else []))
            planes = got if planes is None else [np.where(np.isfinite(a), a, b)
                                                 for a, b in zip(planes, got, strict=True)]
            if all(np.isfinite(a).all() for a in planes):
                break
        return planes, src_px

    def site_cloud(self, client: httpx.Client, scene: dict, grid: Grid) -> float | None:
        """Share (%) of the window flagged cloud / shadow / cirrus by the S2 scene
        classification (SCL, 20 m)."""
        a = scene["assets"].get("scl")
        if scene["source"] != "s2" or not a:
            return None
        g = Grid(*grid.bbox, min(grid.width, 256), min(grid.height, 256))
        scl, _ = sample_cog(client, self.headers, a["href"], g, scene["extra"].get("epsg"))
        v = np.round(scl[..., 0])
        valid = np.isfinite(v) & (v >= SCL_VALID_MIN)
        if not valid.any():
            return None
        return round(float(np.isin(v[valid], SCL_CLOUD).sum()) * 100 / int(valid.sum()), 1)

    def sar_planes(self, client: httpx.Client, scene: dict, pols: tuple[str, ...], grid: Grid
                   ) -> list[np.ndarray]:
        data = titiler_npy(client, scene, list(pols), grid)
        cal = bool(scene["extra"].get("calibrated"))
        return [sar_power(data[i], cal) for i in range(len(pols))]

    def render(self, client: httpx.Client, scene: dict, preset: str, grid: Grid,
               other: dict | None = None) -> tuple[np.ndarray, dict]:
        if preset not in PRESETS or preset not in scene["presets"]:
            raise RenderError(f"preset {preset!r} not available for {scene['name']}")
        if PRESETS[preset]["pair"]:
            if not other:
                raise RenderError(f"{PRESETS[preset]['label']} needs a second scene")
            if other["source"] != scene["source"]:
                raise RenderError("change presets need two scenes from the same sensor")
            if other["key"] == scene["key"]:
                raise RenderError("pick two different dates")
        src = scene["source"]
        meta: dict = {"preset": preset, "label": PRESETS[preset]["label"]}
        if src == "maxar":
            arr, px = sample_cog(client, self.headers, scene["assets"]["visual"]["href"], grid,
                                 scene["extra"].get("epsg"))
            meta["source_m_per_px"] = round(px, 3)
            meta["stretch"] = {"mode": "as delivered (8-bit visual)"}
            return to_rgba(arr / 255.0), meta
        if src in OPTICAL:
            names = OPTICAL[src][preset]
            planes, px = self.optical_bands(client, scene, names, grid)
            meta["source_m_per_px"] = round(px, 3) if px else None
            meta["bands"] = list(names)
            if preset == "true":
                rgb, meta["stretch"] = true_colour(*planes)
            elif preset in ("swir", "nir"):
                rgb, meta["stretch"] = false_colour(planes)
            elif preset == "nbr":
                v = nbr(*planes)
                rgb = ramp(v, NBR_RAMP)
                meta["stretch"] = {"mode": "NBR -0.5..0.7 ramp", "median": round(float(np.nanmedian(v)), 3)
                                   if np.isfinite(v).any() else None}
            else:  # dnbr: pre-fire NBR minus post-fire NBR (earlier scene first)
                pre, post = sorted((scene, other), key=lambda s: s["acquired_at"])
                pb, _ = self.optical_bands(client, pre, names, grid)
                qb, _ = (planes, None) if post is scene else self.optical_bands(client, post, names, grid)
                d = nbr(*pb) - nbr(*qb)
                grey = stretch(qb[0], *percentiles(qb[0], 1, 99))
                rgb, extra = dnbr_rgb(d, grey)
                meta["stretch"] = {"mode": "USGS dNBR classes", **extra}
                meta["pair"] = {"pre": pre["key"], "post": post["key"]}
                meta["note"] = ("NBR also drops with harvest, ploughing and senescence: compare "
                                "scenes close in time either side of the fire")
            if src == "s2":
                try:
                    meta["site_cloud_pct"] = self.site_cloud(client, scene, grid)
                except (Unsupported, httpx.HTTPError, RenderError):
                    meta["site_cloud_pct"] = None
            return to_rgba(rgb), meta
        if src in ("s1", "s1grd"):
            meta["source_m_per_px"] = 10.0
            meta["calibrated"] = bool(scene["extra"].get("calibrated"))
            if preset in ("sar", "sarvh"):
                (p,) = self.sar_planes(client, scene, ("vv" if preset == "sar" else "vh",), grid)
                db = sar_db(p)
                lo, hi = percentiles(db, 2, 98)
                meta["stretch"] = {"db": [round(lo, 1), round(hi, 1)], "speckle": "3x3 boxcar"}
                g = stretch(db, lo, hi)
                return to_rgba(np.repeat(g[..., None], 3, axis=2)), meta
            if preset == "sar_rgb":
                vv, vh = self.sar_planes(client, scene, ("vv", "vh"), grid)
                dvv, dvh = sar_db(vv), sar_db(vh)
                ratio = dvv - dvh
                chans, st = [], []
                for x in (dvv, dvh, ratio):
                    lo, hi = percentiles(x, 2, 98)
                    chans.append(stretch(x, lo, hi))
                    st.append([round(lo, 1), round(hi, 1)])
                meta["stretch"] = {"db": st, "channels": ["VV", "VH", "VV-VH (dB)"],
                                   "speckle": "3x3 boxcar"}
                return to_rgba(np.stack(chans, axis=2)), meta
            # sar_change: VV log-ratio, later minus earlier
            pre, post = sorted((scene, other), key=lambda s: s["acquired_at"])
            (a,) = self.sar_planes(client, pre, ("vv",), grid)
            (b,) = self.sar_planes(client, post, ("vv",), grid)
            da, db_ = sar_db(a, 5), sar_db(b, 5)
            lo, hi = percentiles(db_, 2, 98)
            rgb, extra = change_rgb(db_ - da, stretch(db_, lo, hi))
            meta["stretch"] = {"mode": "VV log-ratio, 5x5 boxcar", **extra}
            meta["pair"] = {"pre": pre["key"], "post": post["key"]}
            same_orbit = (pre["extra"].get("rel_orbit") == post["extra"].get("rel_orbit"))
            meta["same_orbit"] = same_orbit
            if not same_orbit:
                meta["note"] = "different orbits: viewing geometry differs, so does backscatter"
            return to_rgba(rgb), meta
        raise RenderError(f"{scene['name']} is not rendered server-side")


def encode(rgba: np.ndarray, fmt: str = "webp") -> tuple[bytes, str]:
    """(bytes, media type). WebP (lossy q90, alpha kept) for display, PNG for download;
    without Pillow only an RGB PNG is possible."""
    try:
        from PIL import Image
    except ImportError:
        h, w = rgba.shape[:2]
        return png.encode_rgb(w, h, rgba[..., :3].tobytes()), "image/png"
    im = Image.fromarray(rgba, "RGBA")
    buf = io.BytesIO()
    if fmt == "png":
        im.save(buf, "PNG", compress_level=6)
        return buf.getvalue(), "image/png"
    im.save(buf, "WEBP", quality=90, method=4)
    return buf.getvalue(), "image/webp"
