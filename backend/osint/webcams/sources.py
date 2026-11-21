"""Per-source fetch specs and pure parsers (payload -> list[Webcam]). Open, owner-published feeds only."""

import json
import xml.etree.ElementTree as ET
from collections.abc import Callable
from dataclasses import dataclass, field

from osint.webcams.models import Webcam


@dataclass(frozen=True)
class Source:
    id: str
    name: str
    licence: str
    attribution: str
    url: str  # homepage / docs
    commercial_ok: bool
    urls: tuple[str, ...] = ()
    headers: dict[str, str] = field(default_factory=dict)
    refresh_s: int = 60
    country: str = ""


def _f(v) -> float | None:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _valid(lat, lon) -> bool:
    return lat is not None and lon is not None and -90 <= lat <= 90 and -180 <= lon <= 180 and not (lat == 0 and lon == 0)


def _mk(src: Source, native: str, name: str, lat, lon, image_url, now: str, *, video=None, embed=None,
        country: str | None = None, refresh_s: int | None = None) -> Webcam | None:
    lat, lon = _f(lat), _f(lon)
    if not native or not image_url or not _valid(lat, lon):
        return None
    return Webcam(id=f"{src.id}:{native}", source=src.id, name=(name or native).strip(), lat=round(lat, 6),
                  lon=round(lon, 6), country=country if country is not None else src.country,
                  image_url=image_url, video_url=video or None, embed_url=embed or None,
                  refresh_s=refresh_s or src.refresh_s, licence=src.licence, attribution=src.attribution,
                  commercial_ok=src.commercial_ok, last_checked=now)


TFL = Source("tfl", "TfL JamCams", "TfL Open Data (Open Government Licence v2.0 terms)",
             "Powered by TfL Open Data", "https://api.tfl.gov.uk/", True, ("https://api.tfl.gov.uk/Place/Type/JamCam",),
             refresh_s=60, country="GB")
CALTRANS = Source("caltrans", "Caltrans CCTV", "Public data of the California Department of Transportation",
                  "Caltrans / CWWP", "https://cwwp2.dot.ca.gov/", True,
                  tuple(f"https://cwwp2.dot.ca.gov/data/d{n}/cctv/cctvStatusD{n:02d}.json" for n in range(1, 13)),
                  refresh_s=60, country="US")
NYC = Source("nyc", "NYC DOT traffic cameras", "NYC Open Data terms of use", "NYC DOT / NYCTMC",
             "https://webcams.nyctmc.org/", True, ("https://webcams.nyctmc.org/api/cameras",), refresh_s=10,
             country="US")
DIGITRAFFIC = Source("digitraffic", "Digitraffic Finland weather cameras", "CC BY 4.0",
                     "Source: Fintraffic / digitraffic.fi, CC BY 4.0", "https://www.digitraffic.fi/en/road-traffic/",
                     True, ("https://tie.digitraffic.fi/api/weathercam/v1/stations",),
                     headers={"Digitraffic-User": "osint-poc"}, refresh_s=300, country="FI")
SG = Source("sg", "Singapore LTA traffic images", "Singapore Open Data Licence v1.0",
            "Contains information from LTA Traffic Images, accessed via data.gov.sg, under the Singapore Open Data Licence",
            "https://data.gov.sg/", True, ("https://api.data.gov.sg/v1/transport/traffic-images",), refresh_s=60,
            country="SG")
HK = Source("hk", "Hong Kong Transport Department traffic snapshots", "Data.gov.hk Terms and Conditions of Use",
            "Transport Department, HKSAR Government via data.gov.hk",
            "https://data.gov.hk/en-data/dataset/hk-td-tis_2-traffic-snapshot-images", True,
            ("https://static.data.gov.hk/td/traffic-snapshot-images/code/Traffic_Camera_Locations_En.xml",),
            refresh_s=120, country="HK")
WINDY = Source("windy", "Windy Webcams", "Windy Webcams API terms (attribution + link back required)",
               "Webcams provided by windy.com", "https://api.windy.com/webcams/docs", False,
               ("https://api.windy.com/webcams/api/v3/webcams",), refresh_s=600)


def parse_tfl(raw, now: str) -> list[Webcam]:
    out = []
    for p in raw if isinstance(raw, list) else []:
        props = {a.get("key"): a.get("value") for a in p.get("additionalProperties") or []}
        if str(props.get("available", "true")).lower() == "false":
            continue
        native = str(p.get("id", "")).removeprefix("JamCams_")
        w = _mk(TFL, native, p.get("commonName"), p.get("lat"), p.get("lon"), props.get("imageUrl"), now,
                video=props.get("videoUrl"))
        if w:
            out.append(w)
    return out


def parse_caltrans(raw, now: str) -> list[Webcam]:
    out = []
    for row in (raw or {}).get("data", []) if isinstance(raw, dict) else []:
        c = row.get("cctv") or {}
        if str(c.get("inService", "true")).lower() != "true":
            continue
        loc, img = c.get("location") or {}, c.get("imageData") or {}
        static = img.get("static") or {}
        try:
            refresh = max(60, int(static.get("currentImageUpdateFrequency")) * 60)
        except (TypeError, ValueError):
            refresh = 60
        video = img.get("streamingVideoURL") or ""
        native = f"d{loc.get('district', '')}-{c.get('index', '')}"
        w = _mk(CALTRANS, native, loc.get("locationName"), loc.get("latitude"), loc.get("longitude"),
                static.get("currentImageURL"), now, video=video if video.startswith("https://") else None,
                refresh_s=refresh)
        if w:
            out.append(w)
    return out


def parse_nyc(raw, now: str) -> list[Webcam]:
    out = []
    for c in raw if isinstance(raw, list) else []:
        if c.get("isOnline") in (False, "false"):
            continue
        cid = str(c.get("id") or "")
        if not cid.replace("-", "").replace("_", "").isalnum():
            continue
        w = _mk(NYC, cid, c.get("name"), c.get("latitude"), c.get("longitude"),
                f"https://webcams.nyctmc.org/api/cameras/{cid}/image", now)
        if w:
            out.append(w)
    return out


def parse_digitraffic(raw, now: str) -> list[Webcam]:
    out = []
    for f in (raw or {}).get("features", []) if isinstance(raw, dict) else []:
        p = f.get("properties") or {}
        coords = (f.get("geometry") or {}).get("coordinates") or []
        if len(coords) < 2 or p.get("collectionStatus") not in (None, "GATHERING"):
            continue
        presets = [x.get("id") for x in p.get("presets") or [] if x.get("inCollection", True) and x.get("id")]
        if not presets:
            continue
        w = _mk(DIGITRAFFIC, str(p.get("id") or f.get("id")), (p.get("name") or "").replace("_", " "), coords[1],
                coords[0], f"https://weathercam.digitraffic.fi/{sorted(presets)[0]}.jpg", now)
        if w:
            out.append(w)
    return out


def parse_sg(raw, now: str) -> list[Webcam]:
    out = []
    items = ((raw or {}).get("items") or []) if isinstance(raw, dict) else []
    for c in (items[0].get("cameras") or []) if items else []:
        loc = c.get("location") or {}
        cid = str(c.get("camera_id") or "")
        w = _mk(SG, cid, f"Traffic camera {cid}", loc.get("latitude"), loc.get("longitude"), c.get("image"), now)
        if w:
            out.append(w)
    return out


def parse_hk(raw, now: str) -> list[Webcam]:
    out = []
    try:
        root = ET.fromstring(raw)
    except (ET.ParseError, TypeError, ValueError):
        return out
    for im in root.iter("image"):
        key = (im.findtext("key") or "").strip()
        url = (im.findtext("url") or "").strip()
        if not url.startswith("https://"):
            url = f"https://tdcctv.data.one.gov.hk/{key}.JPG" if key else ""
        name = (im.findtext("description") or "").strip() or key
        w = _mk(HK, key, name, im.findtext("latitude"), im.findtext("longitude"), url, now)
        if w:
            out.append(w)
    return out


def parse_windy(raw, now: str) -> list[Webcam]:
    out = []
    for c in (raw or {}).get("webcams", []) if isinstance(raw, dict) else []:
        if c.get("status") not in (None, "active"):
            continue
        loc = c.get("location") or {}
        cur = ((c.get("images") or {}).get("current")) or {}
        player = c.get("player") or {}
        embed = player.get("live") or player.get("day")
        urls = c.get("urls") or {}
        w = _mk(WINDY, str(c.get("webcamId") or ""), c.get("title"), loc.get("latitude"), loc.get("longitude"),
                cur.get("preview") or cur.get("thumbnail"), now, embed=embed,
                country=(loc.get("country_code") or "").upper())
        if w:
            if urls.get("detail"):
                w.attribution = f"{WINDY.attribution} ({urls['detail']})"
            out.append(w)
    return out


def parse_json_bytes(b: bytes):
    return json.loads(b.decode("utf-8-sig"))


# id -> (Source, payload decoder, parser). Order = refresh order.
REGISTRY: dict[str, tuple[Source, Callable, Callable]] = {
    "tfl": (TFL, parse_json_bytes, parse_tfl),
    "caltrans": (CALTRANS, parse_json_bytes, parse_caltrans),
    "nyc": (NYC, parse_json_bytes, parse_nyc),
    "digitraffic": (DIGITRAFFIC, parse_json_bytes, parse_digitraffic),
    "sg": (SG, parse_json_bytes, parse_sg),
    "hk": (HK, lambda b: b, parse_hk),
    "windy": (WINDY, parse_json_bytes, parse_windy),
}
