"""Cut a ~5x5 km window around a point out of a scene (COG over HTTP) or fall back to preview."""

from array import array
from dataclasses import dataclass

import httpx

from osint.imagery.geo import pixel_window, utm_forward, window_bounds
from osint.imagery.stac import Scene
from osint.imagery.tiff import RangeFile, Tiff, Unsupported, Window, parse_tiff, read_window


class NoData(Exception):
    """The event lies outside the valid (non-nodata) area of the scene."""


@dataclass
class Crop:
    width: int
    height: int
    rgb: bytes
    bounds: dict


# Landsat C2 L2 surface reflectance DN -> 8-bit display (gamma stretch of 0..0.3), memoised
_LUT: dict[int, int] = {}


def _dn_to_8bit(dn: int) -> int:
    if dn == 0:
        return 0
    v = _LUT.get(dn)
    if v is None:
        refl = min(1.0, max(0.0, (dn * 2.75e-5 - 0.2) / 0.3))
        v = _LUT[dn] = max(1, round(255 * refl ** (1 / 2.2)))
    return v


def _open(client: httpx.Client, url: str, scene: Scene) -> tuple[RangeFile, Tiff, int]:
    rf = RangeFile(client, url)
    t = parse_tiff(rf)
    epsg = t.epsg or scene.epsg
    if not epsg or t.origin is None or t.pixel_size is None:
        raise Unsupported("scene is not georeferenced in UTM")
    return rf, t, int(epsg)


def _window(client: httpx.Client, url: str, scene: Scene, lat: float, lon: float,
            half_m: float) -> tuple[Window, dict]:
    rf, t, epsg = _open(client, url, scene)
    e, n = utm_forward(lat, lon, epsg)
    col, row, w, h = pixel_window(e, n, t.origin[0], t.origin[1], t.pixel_size[0],
                                  t.pixel_size[1], half_m)
    win = read_window(rf, t, col, row, w, h)
    return win, {"epsg": epsg, "utm": [t.origin[0] + col * t.pixel_size[0],
                                       t.origin[1] - (row + h) * t.pixel_size[1],
                                       t.origin[0] + (col + w) * t.pixel_size[0],
                                       t.origin[1] - row * t.pixel_size[1]],
                 "gsd": t.pixel_size[0]}


def crop_scene(client: httpx.Client, scene: Scene, lat: float, lon: float, half_km: float
               ) -> Crop:
    """Full-resolution RGB crop. Raises Unsupported / NoData / httpx errors / zlib.error."""
    half_m = half_km * 1000
    if scene.visual:
        win, meta = _window(client, scene.visual, scene, lat, lon, half_m)
        if win.samples != 3 or win.bits != 8:
            raise Unsupported("visual asset is not 8-bit RGB")
        rgb = win.data
        w, h = win.width, win.height
    else:
        planes = []
        meta = {}
        for band in ("red", "green", "blue"):
            win, meta = _window(client, scene.bands[band], scene, lat, lon, half_m)
            if win.samples != 1 or win.bits != 16:
                raise Unsupported("band asset is not 16-bit single band")
            a = array("H")
            a.frombytes(win.data)
            planes.append(bytes(_dn_to_8bit(x) for x in a))
        w, h = win.width, win.height
        buf = bytearray(w * h * 3)
        for i, p in enumerate(planes):
            buf[i::3] = p
        rgb = bytes(buf)
    if not any(rgb):
        raise NoData("no valid pixels around the event in this scene")
    bounds = {**window_bounds(lat, lon, half_km), **meta, "kind": "crop", "px": [w, h],
              "marker": {"x": 0.5, "y": 0.5}}
    return Crop(w, h, rgb, bounds)


def fetch_preview(client: httpx.Client, scene: Scene, lat: float, lon: float
                  ) -> tuple[bytes, str, dict]:
    """Whole-scene preview image (bytes, extension, bounds). The event is not drawn onto it
    (no image decoder available); bounds.marker gives its approximate fractional position."""
    if not scene.thumbnail or not scene.thumbnail.startswith("http"):
        raise Unsupported("scene has no readable preview")
    r = client.get(scene.thumbnail)
    r.raise_for_status()
    ctype = r.headers.get("content-type", "")
    ext = "png" if "png" in ctype or r.content[:4] == b"\x89PNG" else "jpg"
    west, south, east, north = scene.bbox
    marker = None
    if east > west and north > south:
        marker = {"x": min(1.0, max(0.0, (lon - west) / (east - west))),
                  "y": min(1.0, max(0.0, (north - lat) / (north - south)))}
    bounds = {"west": west, "south": south, "east": east, "north": north,
              "kind": "preview", "marker": marker}
    return r.content, ext, bounds
