import urllib.request
import json
import csv
import time
import os
from datetime import datetime, timedelta
from storage import init_db, store_flights
from mil_types import get_mil_category_from_type, ALL_MIL_TYPES, is_high_priority

# Free unfiltered ADS-B APIs - no keys needed, show ALL military aircraft
APIS = [
    ("airplanes.live", "https://api.airplanes.live/v2"),
    ("adsb.fi", "https://opendata.adsb.fi/api/v2"),
    ("adsb.one", "https://api.adsb.one/v2"),
    ("adsb.lol", "https://api.adsb.lol/v2"),
]

# Multiple search points to cover the full ME region (250NM radius each)
# Covers: Eastern Med, Red Sea, Persian Gulf, Arabian Sea, Iraq, Syria, Iran
ME_SEARCH_POINTS = [
    (33.5, 36.0),   # Syria / Lebanon / Israel
    (33.0, 44.0),   # Iraq
    (27.0, 50.0),   # Persian Gulf / Saudi / Bahrain / Qatar
    (25.0, 56.0),   # UAE / Oman
    (15.0, 43.0),   # Yemen / Red Sea
    (32.0, 55.0),   # Iran
    (36.0, 40.0),   # Turkey/Syria border
]

IST_OFFSET = timedelta(hours=5, minutes=30)

# Load military aircraft database for enrichment
_mil_db = {}
_mil_db_loaded = False


def load_mil_db():
    global _mil_db, _mil_db_loaded
    if _mil_db_loaded:
        return
    db_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "plane-alert-mil.csv")
    if not os.path.exists(db_path):
        print("  [MilDB] plane-alert-mil.csv not found, skipping enrichment")
        _mil_db_loaded = True
        return
    with open(db_path, "r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            hex_code = row.get("$ICAO", "").strip().lower()
            if hex_code:
                _mil_db[hex_code] = {
                    "reg": row.get("$Registration", ""),
                    "operator": row.get("$Operator", ""),
                    "type": row.get("$Type", ""),
                    "icao_type": row.get("$ICAO Type", ""),
                    "category": row.get("Category", ""),
                    "tag1": row.get("$Tag 1", ""),
                    "tag2": row.get("$#Tag 2", ""),
                }
    print(f"  [MilDB] Loaded {len(_mil_db)} military aircraft")
    _mil_db_loaded = True


def fetch_military_global(api_name, base_url):
    """Fetch all military aircraft globally from a free API."""
    url = f"{base_url}/mil"
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    response = urllib.request.urlopen(req, timeout=20)
    data = json.loads(response.read().decode("utf-8"))
    return data.get("ac", [])


def fetch_area(api_name, base_url, lat, lon, radius=250):
    """Fetch all aircraft in a radius around a point."""
    url = f"{base_url}/point/{lat}/{lon}/{radius}"
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    response = urllib.request.urlopen(req, timeout=20)
    data = json.loads(response.read().decode("utf-8"))
    return data.get("ac", [])


def ms_to_knots(gs):
    """Ground speed from API is already in knots."""
    if gs is None:
        return None
    return round(gs, 1)


def heading_label(deg):
    if deg is None:
        return "N/A"
    dirs = ["N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE",
            "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW"]
    return dirs[round(deg / 22.5) % 16]


def is_in_me_region(lat, lon):
    """Check if aircraft is in the broader ME region."""
    return lat is not None and lon is not None and 10 <= lat <= 45 and 20 <= lon <= 65


def enrich_aircraft(ac):
    """Enrich aircraft data with military database info."""
    hex_code = ac.get("hex", "").strip().lower()
    db_entry = _mil_db.get(hex_code)

    # Data from API
    callsign = (ac.get("flight") or "").strip() or "UNKNOWN"
    lat = ac.get("lat")
    lon = ac.get("lon")
    alt_baro = ac.get("alt_baro")
    alt_geom = ac.get("alt_geom")
    gs = ac.get("gs")
    track = ac.get("track")
    vert_rate = ac.get("baro_rate") or ac.get("geom_rate")
    squawk = ac.get("squawk") or "N/A"
    reg = ac.get("r") or ""
    ac_type = ac.get("t") or ""
    desc = ac.get("desc") or ""
    db_flags = ac.get("dbFlags", 0) or 0

    # Three-layer military classification:
    # 1. API dbFlags (from feeder databases)
    # 2. Hex code in plane-alert military database
    # 3. ICAO type code is exclusively military
    type_is_mil, type_category = get_mil_category_from_type(ac_type)
    is_mil = bool(db_flags & 1) or hex_code in _mil_db or type_is_mil

    # Altitude
    alt = alt_baro if alt_baro and alt_baro != "ground" else alt_geom
    if alt == "ground":
        alt = None
        on_ground = True
    else:
        on_ground = ac.get("alt_baro") == "ground"

    alt_ft = int(alt) if alt is not None else None

    # Enrichment from military database
    if db_entry:
        is_mil = True
        if not reg:
            reg = db_entry["reg"]
        if not ac_type:
            ac_type = db_entry["icao_type"]
        if not desc:
            desc = db_entry["type"]
        operator = db_entry["operator"]
        mil_category = db_entry["category"]
        mil_role = db_entry["tag1"]
    else:
        operator = ""
        mil_category = type_category or ""
        mil_role = type_category or ""

    # Vertical rate
    if vert_rate is not None:
        if vert_rate > 100:
            vrate_str = f"Climbing ({round(vert_rate)} ft/min)"
        elif vert_rate < -100:
            vrate_str = f"Descending ({round(vert_rate)} ft/min)"
        else:
            vrate_str = "Level"
    else:
        vrate_str = "N/A"

    heading_str = f"{heading_label(track)} ({round(track)}deg)" if track is not None else "N/A"
    speed_kts = round(gs, 1) if gs is not None else None

    return {
        "hex": hex_code,
        "callsign": callsign,
        "reg": reg,
        "type": ac_type,
        "desc": desc,
        "operator": operator,
        "mil_category": mil_category,
        "mil_role": mil_role,
        "lat": lat,
        "lon": lon,
        "alt_ft": alt_ft,
        "speed_kts": speed_kts,
        "heading": heading_str,
        "heading_deg": track,
        "vertical": vrate_str,
        "squawk": squawk,
        "on_ground": on_ground,
        "is_mil": is_mil,
        "high_priority": is_mil and is_high_priority(ac_type),
    }


def update_flights():
    print("Fetching flight data (unfiltered ADS-B)...")
    load_mil_db()

    all_aircraft = {}  # keyed by hex to deduplicate

    # 1. Fetch military aircraft from ALL APIs, dedup by hex
    for api_name, base_url in APIS:
        try:
            mil_ac = fetch_military_global(api_name, base_url)
            me_mil = [a for a in mil_ac if is_in_me_region(a.get("lat"), a.get("lon"))]
            new = 0
            for a in me_mil:
                hex_code = a.get("hex", "").lower()
                if hex_code and hex_code not in all_aircraft:
                    all_aircraft[hex_code] = a
                    new += 1
            print(f"  {api_name} /mil: {len(mil_ac)} global, {len(me_mil)} ME, {new} new")
        except Exception as e:
            print(f"  {api_name} /mil failed: {e}")
        time.sleep(1)

    # 2. Fetch area data from ALL APIs for each ME search point
    for api_name, base_url in APIS:
        before = len(all_aircraft)
        for lat, lon in ME_SEARCH_POINTS:
            try:
                area_ac = fetch_area(api_name, base_url, lat, lon, 250)
                for a in area_ac:
                    hex_code = a.get("hex", "").lower()
                    if hex_code and hex_code not in all_aircraft:
                        all_aircraft[hex_code] = a
            except Exception:
                pass
            time.sleep(1)  # Rate limit: 1 req/sec
        added = len(all_aircraft) - before
        print(f"  {api_name} area: +{added} new (total: {len(all_aircraft)})")


    # 3. Supplement with OpenSky for volume (more receivers in ME)
    try:
        opensky_url = "https://opensky-network.org/api/states/all?lamin=10&lomin=20&lamax=45&lomax=65"
        req = urllib.request.Request(opensky_url, headers={"User-Agent": "Mozilla/5.0"})
        resp = urllib.request.urlopen(req, timeout=30)
        odata = json.loads(resp.read().decode("utf-8"))
        ostates = odata.get("states", []) or []
        new_from_os = 0
        for s in ostates:
            hex_code = (s[0] or "").lower()
            if not hex_code or hex_code in all_aircraft:
                continue
            lat, lon = s[6], s[5]
            if lat is None or lon is None:
                continue
            # Convert OpenSky format to community API format
            all_aircraft[hex_code] = {
                "hex": hex_code,
                "flight": (s[1] or "").strip(),
                "lat": lat,
                "lon": lon,
                "alt_baro": s[7] * 3.281 if s[7] else ("ground" if s[8] else None),
                "gs": s[9] * 1.944 if s[9] else None,
                "track": s[10],
                "baro_rate": s[11] * 196.85 if s[11] else None,  # m/s to ft/min
                "squawk": s[14],
                "t": "",
                "r": "",
                "desc": "",
                "dbFlags": 0,
            }
            new_from_os += 1
        print(f"  OpenSky supplement: +{new_from_os} new (total: {len(all_aircraft)})")
    except Exception as e:
        print(f"  OpenSky supplement failed: {e}")

    # Enrich and classify (filter out ground stations/towers)
    GROUND_TYPES = {"TWR", "GND", "GRND"}
    enriched = []
    mil_count = 0
    for ac in all_aircraft.values():
        e = enrich_aircraft(ac)
        if e["lat"] is None or e["lon"] is None:
            continue
        if e["type"].upper() in GROUND_TYPES:
            continue
        enriched.append(e)
        if e["is_mil"]:
            mil_count += 1

    # Sort: military first, then by altitude descending
    enriched.sort(key=lambda x: (not x["is_mil"], -(x["alt_ft"] or 0)))

    print(f"  Total: {len(enriched)} aircraft | {mil_count} military")

    now_ist = (datetime.utcnow() + IST_OFFSET).strftime("%d %b %Y, %I:%M:%S %p IST")

    # --- Build KML ---
    kml = '<?xml version="1.0" encoding="UTF-8"?>\n'
    kml += '<kml xmlns="http://www.opengis.net/kml/2.2">\n<Document>\n'
    kml += '<name>Live Flights - ME OSINT (Unfiltered)</name>\n'

    flights_json = []

    for ac in enriched:
        lat, lon = ac["lat"], ac["lon"]
        callsign = ac["callsign"]
        is_mil = ac["is_mil"]
        alt_ft = ac["alt_ft"]
        heading_deg = ac["heading_deg"]

        rotation = round(heading_deg) if heading_deg is not None else 0
        alt_kml = alt_ft * 0.3048 if alt_ft and not ac["on_ground"] else 0

        if is_mil:
            icon_color = "ff0000ff"  # Red
            icon_scale = 1.2
            header_bg = "#c00"
            name_prefix = "[MIL] "
        else:
            icon_color = "88888888"
            icon_scale = 0.4
            header_bg = "#555"
            name_prefix = ""

        if ac["on_ground"]:
            icon_scale *= 0.5

        alt_display = f"{alt_ft:,} ft" if alt_ft else "GND"
        speed_display = f"{ac['speed_kts']} kts" if ac["speed_kts"] else "N/A"

        # Build mil info rows for popup
        mil_info = ""
        if is_mil:
            if ac["operator"]:
                mil_info += f'<tr><td class="label">Operator</td><td class="val" style="color:#ff6666">{ac["operator"]}</td></tr>'
            if ac["desc"]:
                mil_info += f'<tr><td class="label">Aircraft</td><td class="val" style="color:#ff6666">{ac["desc"]}</td></tr>'
            if ac["mil_role"]:
                mil_info += f'<tr><td class="label">Role</td><td class="val">{ac["mil_role"]}</td></tr>'
            if ac["mil_category"]:
                mil_info += f'<tr><td class="label">Category</td><td class="val">{ac["mil_category"]}</td></tr>'

        desc_kml = f"""<![CDATA[
<style>
  table {{ font-family: Arial; font-size: 13px; border-collapse: collapse; width: 100%; }}
  td {{ padding: 4px 8px; border-bottom: 1px solid #444; }}
  .label {{ color: #aaa; font-weight: bold; white-space: nowrap; }}
  .val {{ color: #fff; }}
  .header {{ background: {header_bg}; color: white; padding: 6px 8px; font-size: 14px; font-weight: bold; }}
</style>
<div style="background:#1a1a1a; border-radius:6px; overflow:hidden; min-width:280px;">
  <div class="header">{'MILITARY: ' if is_mil else ''}{callsign}</div>
  <table>
    {mil_info}
    <tr><td class="label">Hex</td><td class="val">{ac['hex']}</td></tr>
    <tr><td class="label">Registration</td><td class="val">{ac['reg'] or 'N/A'}</td></tr>
    <tr><td class="label">Type</td><td class="val">{ac['type']} - {ac['desc'] or 'N/A'}</td></tr>
    <tr><td class="label">Altitude</td><td class="val">{alt_display}</td></tr>
    <tr><td class="label">Speed</td><td class="val">{speed_display}</td></tr>
    <tr><td class="label">Heading</td><td class="val">{ac['heading']}</td></tr>
    <tr><td class="label">Vertical</td><td class="val">{ac['vertical']}</td></tr>
    <tr><td class="label">Squawk</td><td class="val">{ac['squawk']}</td></tr>
    <tr><td class="label">Position</td><td class="val">{lat}, {lon}</td></tr>
  </table>
</div>
]]>"""

        kml += f"""<Placemark>
  <name>{name_prefix}{callsign} ({ac['type']})</name>
  <description>{desc_kml}</description>
  <Style>
    <IconStyle>
      <color>{icon_color}</color>
      <scale>{icon_scale}</scale>
      <heading>{rotation}</heading>
      <Icon><href>http://maps.google.com/mapfiles/kml/shapes/airports.png</href></Icon>
    </IconStyle>
    <BalloonStyle><bgColor>ff1a1a1a</bgColor><textColor>ffffffff</textColor></BalloonStyle>
  </Style>
  <Point>
    <altitudeMode>absolute</altitudeMode>
    <coordinates>{lon},{lat},{alt_kml}</coordinates>
  </Point>
</Placemark>
"""

        flights_json.append({
            "callsign": callsign,
            "hex": ac["hex"],
            "reg": ac["reg"],
            "type": ac["type"],
            "desc": ac["desc"],
            "operator": ac["operator"],
            "mil_role": ac["mil_role"],
            "mil_category": ac["mil_category"],
            "lat": lat,
            "lon": lon,
            "alt_ft": alt_ft,
            "speed_kts": ac["speed_kts"],
            "heading": ac["heading"],
            "heading_deg": heading_deg,
            "vertical": ac["vertical"],
            "squawk": ac["squawk"],
            "on_ground": ac["on_ground"],
            "is_mil": is_mil,
            "high_priority": ac["high_priority"],
        })

    kml += "</Document>\n</kml>"

    with open("LiveFlights.kml", "w", encoding="utf-8") as f:
        f.write(kml)

    with open("flights.json", "w", encoding="utf-8") as f:
        json.dump({
            "updated": now_ist,
            "total": len(flights_json),
            "military": mil_count,
            "flights": flights_json,
        }, f)

    # Persist to historical DB
    try:
        store_flights(flights_json)
    except Exception as e:
        print(f"  [DB] Flight storage error: {e}")

    print(f"  LiveFlights.kml + flights.json updated")


if __name__ == "__main__":
    while True:
        update_flights()
        time.sleep(15)
