"""KML rendering for Google Earth Pro."""

from urllib.parse import quote
from xml.sax.saxutils import escape

KML_MEDIA = "application/vnd.google-earth.kml+xml"
ICON = "http://maps.google.com/mapfiles/kml/shapes/"
FT_TO_M = 0.3048
MAX_PLACEMARKS = 20000

RED, ORANGE, WHITE = "ff0000ff", "ff00a5ff", "ffffffff"  # aabbggrr

_NS = '<?xml version="1.0" encoding="UTF-8"?>\n<kml xmlns="http://www.opengis.net/kml/2.2">'


def _e(v: object) -> str:
    return escape("" if v is None else str(v))


def _table(rows: list[tuple[str, object]]) -> str:
    body = "".join(
        f"<tr><td><b>{_e(k)}</b></td><td>{_e(v)}</td></tr>" for k, v in rows if v not in (None, "")
    )
    return f"<table>{body}</table>"


def _style(icon: str, color: str = WHITE, scale: float = 1.0,
           heading: float | None = None) -> str:
    h = f"<heading>{heading:.0f}</heading>" if heading is not None else ""
    return (f"<Style><IconStyle><color>{color}</color><scale>{scale:.2f}</scale>{h}"
            f"<Icon><href>{icon}</href></Icon></IconStyle></Style>")


def _placemark(name: str, desc: str, style: str, lon: float, lat: float,
               alt_m: float | None = None, when: str | None = None) -> str:
    if alt_m is not None:
        coords = f"{lon},{lat},{alt_m:.0f}"
        mode = "<altitudeMode>absolute</altitudeMode>"
    else:
        coords, mode = f"{lon},{lat}", ""
    ts = f"<TimeStamp><when>{_e(when)}</when></TimeStamp>" if when else ""
    cdata = desc.replace("]]>", "]]]]><![CDATA[>")
    return (f"<Placemark><name>{_e(name)}</name>"
            f"<description><![CDATA[{cdata}]]></description>"
            f"{ts}{style}<Point>{mode}<coordinates>{coords}</coordinates></Point></Placemark>")


ATTRIBUTION = ("Data: ADS-B aggregators (adsb.lol ODbL, airplanes.live, adsb.fi), AISStream, NASA FIRMS, USGS, "
               "GDELT, news via AlterLab, UCDP GED (CC BY 4.0), Copernicus Sentinel. "
               "Licences and required credits: /api/sources/licences")


def _doc(name: str, placemarks: list[str]) -> str:
    credit = (f"<Snippet maxLines=\"1\">{_e('Data sources and licences: see description')}</Snippet>"
              f"<description>{_e(ATTRIBUTION)}</description>")
    return f"{_NS}<Document><name>{_e(name)}</name>{credit}{''.join(placemarks)}</Document></kml>"


def _track_style(item: dict, icon: str, aircraft: bool) -> str:
    if item.get("high_priority"):
        color = RED
    elif item.get("is_mil"):
        color = ORANGE if aircraft else RED
    else:
        color = WHITE
    h = item.get("heading_deg") if aircraft else None
    return _style(icon, color, 0.9, h)


def _extra(item: dict) -> list[tuple[str, object]]:
    return list((item.get("extra") or {}).items())


def aircraft_kml(items: list[dict]) -> str:
    pms = []
    for it in items[:MAX_PLACEMARKS]:
        alt = it.get("alt_ft")
        rows = [("Callsign", it["name"]), ("ID", it["id"]), ("Type", it["type_code"]),
                ("Operator", it["operator"]),
                ("Altitude", None if alt is None else f"{alt} ft"),
                ("Speed (kts)", it["speed_kts"]), ("Heading", it["heading_deg"]),
                ("Time", it["ts"]), *_extra(it)]
        pms.append(_placemark(it["name"] or it["id"], _table(rows),
                              _track_style(it, ICON + "airports.png", True), it["lon"], it["lat"],
                              None if alt is None else alt * FT_TO_M, it["ts"]))
    return _doc("Aircraft", pms)


def ships_kml(items: list[dict]) -> str:
    pms = []
    for it in items[:MAX_PLACEMARKS]:
        rows = [("Name", it["name"]), ("ID", it["id"]), ("Type", it["type_code"]),
                ("Speed (kts)", it["speed_kts"]), ("Heading", it["heading_deg"]),
                ("Time", it["ts"]), *_extra(it)]
        pms.append(_placemark(it["name"] or it["id"], _table(rows),
                              _track_style(it, ICON + "sailing.png", False),
                              it["lon"], it["lat"], None, it["ts"]))
    return _doc("Ships", pms)


def fires_kml(items: list[dict]) -> str:
    pms = []
    for it in items[:MAX_PLACEMARKS]:
        frp = it.get("frp")
        scale = 0.5 + min((frp or 0) / 50.0, 1.0)
        color = RED if (frp or 0) >= 50 else ORANGE
        rows = [("FRP (MW)", frp), ("Confidence", it["confidence"]),
                ("Satellite", it["satellite"]), ("Day/Night", it["daynight"]),
                ("Time", it["ts"])]
        name = f"FRP {frp:g} MW" if frp is not None else "Fire"
        pms.append(_placemark(name, _table(rows), _style(ICON + "firedept.png", color, scale),
                              it["lon"], it["lat"], None, it["ts"]))
    return _doc("Fires", pms)


def quakes_kml(items: list[dict]) -> str:
    pms = []
    for it in items[:MAX_PLACEMARKS]:
        mag = it.get("mag")
        scale = 0.5 + max(mag or 0, 0) / 4.0
        rows = [("Magnitude", mag), ("Depth (km)", it["depth_km"]), ("Place", it["place"]),
                ("Type", it["event_type"]), ("Time", it["ts"]), ("URL", it["url"])]
        name = f"M{mag:.1f}" if mag is not None else "Quake"
        pms.append(_placemark(name, _table(rows), _style(ICON + "earthquake.png", WHITE, scale),
                              it["lon"], it["lat"], None, it["ts"]))
    return _doc("Quakes", pms)


def master_kml(base_url: str, token: str | None = None) -> str:
    base = base_url.rstrip("/")
    query = f"?token={quote(token, safe='')}" if token else ""
    links = []
    for name, path, secs in (("Aircraft", "aircraft", 30), ("Ships", "ships", 60),
                             ("Fires", "fires", 600), ("Quakes", "quakes", 120),
                             ("Events", "events", 120)):
        links.append(
            f"<NetworkLink><name>{name}</name><Link><href>{_e(f"{base}/kml/{path}.kml{query}")}</href>"
            f"<refreshMode>onInterval</refreshMode><refreshInterval>{secs}</refreshInterval>"
            "</Link></NetworkLink>")
    return _doc("OSINT Platform", links)
