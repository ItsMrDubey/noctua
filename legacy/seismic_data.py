"""
seismic_data.py - Fetch earthquake/seismic data from the USGS API for the Middle East.

Large explosions (airstrikes, ammo depot detonations) can register as seismic
events, making this useful for conflict-monitoring OSINT.

Uses only stdlib modules (urllib.request, json). No API key required.
"""

import json
import time
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

IST_OFFSET = timedelta(hours=5, minutes=30)
IST = timezone(IST_OFFSET)

# Middle East bounding box
MIN_LAT = 10
MAX_LAT = 45
MIN_LON = 20
MAX_LON = 65

QUERY_ENDPOINT = "https://earthquake.usgs.gov/fdsnws/event/1/query"
REALTIME_FEED = (
    "https://earthquake.usgs.gov/earthquakes/feed/v1.0/summary/all_hour.geojson"
)

UPDATE_INTERVAL_SEC = 120  # 2 minutes

# KML style helpers
TYPE_COLORS = {
    "earthquake": "7f00ffff",   # yellow  (aaBBGGRR in KML)
    "explosion": "ff0000ff",    # red
    "mining explosion": "ff0000ff",
    "quarry blast": "ff0066ff", # orange-red
}
DEFAULT_COLOR = "ff00ff00"  # green for anything else


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _fetch_json(url: str) -> dict:
    """Fetch a URL and return parsed JSON."""
    req = urllib.request.Request(url, headers={"User-Agent": "OSINT-SeismicMonitor/1.0"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _unix_ms_to_utc(ms: int) -> datetime:
    """Convert USGS unix-millisecond timestamp to a UTC datetime."""
    return datetime.fromtimestamp(ms / 1000.0, tz=timezone.utc)


def _to_ist(dt: datetime) -> datetime:
    return dt.astimezone(IST)


def _fmt(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%d %H:%M:%S %Z")


def _icon_scale(mag: float) -> float:
    """Return a KML icon scale proportional to magnitude."""
    if mag is None or mag < 0:
        return 0.5
    if mag < 2:
        return 0.6
    if mag < 4:
        return 1.0
    if mag < 6:
        return 1.6
    return 2.2


def _in_bbox(lat: float, lon: float) -> bool:
    return MIN_LAT <= lat <= MAX_LAT and MIN_LON <= lon <= MAX_LON


# ---------------------------------------------------------------------------
# Core fetch
# ---------------------------------------------------------------------------

def _fetch_query_events() -> list:
    """Fetch last-24h events inside the ME bounding box via the query API."""
    now = datetime.now(tz=timezone.utc)
    start = now - timedelta(hours=24)
    params = (
        f"?format=geojson"
        f"&starttime={start.strftime('%Y-%m-%dT%H:%M:%S')}"
        f"&endtime={now.strftime('%Y-%m-%dT%H:%M:%S')}"
        f"&minlatitude={MIN_LAT}&maxlatitude={MAX_LAT}"
        f"&minlongitude={MIN_LON}&maxlongitude={MAX_LON}"
        f"&orderby=time"
    )
    url = QUERY_ENDPOINT + params
    data = _fetch_json(url)
    return data.get("features", [])


def _fetch_realtime_events() -> list:
    """Fetch the all-events-last-hour feed filtered to the ME bbox."""
    data = _fetch_json(REALTIME_FEED)
    results = []
    for feat in data.get("features", []):
        coords = feat["geometry"]["coordinates"]
        lon, lat = coords[0], coords[1]
        if _in_bbox(lat, lon):
            results.append(feat)
    return results


def _parse_feature(feat: dict) -> dict:
    """Normalise a GeoJSON feature into our event dict."""
    props = feat["properties"]
    coords = feat["geometry"]["coordinates"]
    lon, lat, depth = coords[0], coords[1], coords[2] if len(coords) > 2 else None

    time_utc = _unix_ms_to_utc(props["time"])
    time_ist = _to_ist(time_utc)

    event_type = (props.get("type") or "earthquake").lower()

    return {
        "time_ist": _fmt(time_ist),
        "time_utc": _fmt(time_utc),
        "lat": round(lat, 4),
        "lon": round(lon, 4),
        "magnitude": props.get("mag"),
        "depth_km": round(depth, 2) if depth is not None else None,
        "place": props.get("place"),
        "type": event_type,
        "felt_reports": props.get("felt") or 0,
        "tsunami_warning": bool(props.get("tsunami")),
        "url": props.get("url"),
    }


# ---------------------------------------------------------------------------
# KML generation
# ---------------------------------------------------------------------------

def _build_kml(events: list) -> str:
    """Build a KML document string from the event list."""
    kml_ns = "http://www.opengis.net/kml/2.2"
    root = ET.Element("kml", xmlns=kml_ns)
    doc = ET.SubElement(root, "Document")
    ET.SubElement(doc, "name").text = "Middle East Seismic Events (last 24 h)"
    ET.SubElement(doc, "description").text = (
        "USGS seismic data filtered to the Middle East. "
        "Explosion-type events are flagged red — possible military strikes."
    )

    # --- Styles (one per type) ---
    created_styles = set()
    for evt in events:
        etype = evt["type"]
        style_id = "style_" + etype.replace(" ", "_")
        if style_id in created_styles:
            continue
        created_styles.add(style_id)

        style = ET.SubElement(doc, "Style", id=style_id)
        icon_style = ET.SubElement(style, "IconStyle")
        color = TYPE_COLORS.get(etype, DEFAULT_COLOR)
        ET.SubElement(icon_style, "color").text = color
        ET.SubElement(icon_style, "scale").text = "1.0"
        icon = ET.SubElement(icon_style, "Icon")
        ET.SubElement(icon, "href").text = (
            "http://maps.google.com/mapfiles/kml/shapes/shaded_dot.png"
        )

    # --- Placemarks ---
    for evt in events:
        pm = ET.SubElement(doc, "Placemark")

        etype = evt["type"]
        is_explosion = "explosion" in etype

        label = evt["place"] or "Unknown"
        if is_explosion:
            label = f"*** EXPLOSION *** {label}"

        ET.SubElement(pm, "name").text = label

        desc_lines = [
            f"Type: {etype.upper()}" if is_explosion else f"Type: {etype}",
            f"Magnitude: {evt['magnitude']}",
            f"Depth: {evt['depth_km']} km",
            f"Time (IST): {evt['time_ist']}",
            f"Time (UTC): {evt['time_utc']}",
            f"Felt reports: {evt['felt_reports']}",
            f"Tsunami warning: {'YES' if evt['tsunami_warning'] else 'No'}",
        ]
        if is_explosion:
            desc_lines.insert(0, "=== POSSIBLE MILITARY STRIKE / LARGE EXPLOSION ===")
        if evt["url"]:
            desc_lines.append(f"Detail: {evt['url']}")
        ET.SubElement(pm, "description").text = "\n".join(desc_lines)

        style_id = "style_" + etype.replace(" ", "_")
        ET.SubElement(pm, "styleUrl").text = f"#{style_id}"

        # Override icon scale per-event based on magnitude
        style_ovr = ET.SubElement(pm, "Style")
        is_ovr = ET.SubElement(style_ovr, "IconStyle")
        ET.SubElement(is_ovr, "scale").text = str(_icon_scale(evt["magnitude"]))

        point = ET.SubElement(pm, "Point")
        ET.SubElement(point, "coordinates").text = f"{evt['lon']},{evt['lat']},{0}"

    tree = ET.ElementTree(root)
    ET.indent(tree, space="  ")

    import io
    buf = io.BytesIO()
    tree.write(buf, xml_declaration=True, encoding="UTF-8")
    return buf.getvalue().decode("utf-8")


# ---------------------------------------------------------------------------
# Main public function
# ---------------------------------------------------------------------------

def update_seismic() -> dict:
    """
    Fetch seismic events, write seismic.json and LiveSeismic.kml.
    Returns the data dict written to JSON.
    """
    # 1. Gather events from both endpoints, de-duplicate by USGS id
    seen_ids = set()
    raw_features = []

    try:
        for feat in _fetch_query_events():
            fid = feat.get("id")
            if fid not in seen_ids:
                seen_ids.add(fid)
                raw_features.append(feat)
    except Exception as exc:
        print(f"[WARN] Query API failed: {exc}")

    try:
        for feat in _fetch_realtime_events():
            fid = feat.get("id")
            if fid not in seen_ids:
                seen_ids.add(fid)
                raw_features.append(feat)
    except Exception as exc:
        print(f"[WARN] Real-time feed failed: {exc}")

    # 2. Parse into normalised event dicts
    events = [_parse_feature(f) for f in raw_features]
    events.sort(key=lambda e: e["time_utc"], reverse=True)

    # Flag summary counts
    explosions = [e for e in events if "explosion" in e["type"]]

    now_ist = datetime.now(tz=IST)
    result = {
        "updated_ist": _fmt(now_ist),
        "total_events": len(events),
        "explosion_events": len(explosions),
        "note": (
            "Explosion-type events may indicate military strikes, "
            "ammo-depot detonations, or industrial blasts. Verify with "
            "other OSINT sources."
        ),
        "events": events,
    }

    # 3. Write JSON
    with open("seismic.json", "w", encoding="utf-8") as fh:
        json.dump(result, fh, indent=2, ensure_ascii=False)
    print(f"[OK] seismic.json written  --  {len(events)} events, "
          f"{len(explosions)} explosion-type")

    # 4. Write KML
    kml_text = _build_kml(events)
    with open("LiveSeismic.kml", "w", encoding="utf-8") as fh:
        fh.write(kml_text)
    print(f"[OK] LiveSeismic.kml written")

    return result


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    print("Seismic monitor for the Middle East (USGS data)")
    print(f"Bounding box: lat {MIN_LAT}-{MAX_LAT}, lon {MIN_LON}-{MAX_LON}")
    print(f"Update interval: {UPDATE_INTERVAL_SEC}s\n")

    while True:
        try:
            data = update_seismic()
            if data["explosion_events"]:
                print(
                    f"  *** {data['explosion_events']} EXPLOSION-TYPE EVENT(S) "
                    f"DETECTED — review seismic.json for details ***"
                )
        except Exception as exc:
            print(f"[ERROR] Update failed: {exc}")

        print(f"Next update in {UPDATE_INTERVAL_SEC}s ...\n")
        time.sleep(UPDATE_INTERVAL_SEC)
