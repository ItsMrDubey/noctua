import json
import time
from datetime import datetime, timedelta

# AISStream.io - free real-time AIS ship data
# Get your free API key at: https://aisstream.io (just sign up)
# Leave empty to skip ship tracking until you have a key
AISSTREAM_API_KEY = __import__("os").environ.get("AISSTREAM_API_KEY", "")

IST_OFFSET = timedelta(hours=5, minutes=30)

# Middle East bounding box
ME_BBOX = [[12, 34], [38, 55]]  # [[lat_min, lon_min], [lat_max, lon_max]]

# Ship type codes to human-readable names
SHIP_TYPES = {
    0: "Not Available",
    30: "Fishing",
    31: "Towing",
    32: "Towing (Large)",
    33: "Dredging",
    34: "Diving Ops",
    35: "Military Ops",
    36: "Sailing",
    37: "Pleasure Craft",
    40: "High Speed Craft",
    50: "Pilot Vessel",
    51: "Search & Rescue",
    52: "Tug",
    53: "Port Tender",
    55: "Law Enforcement",
    58: "Medical Transport",
    60: "Passenger",
    70: "Cargo",
    80: "Tanker",
    90: "Other",
}


def get_ship_type(code):
    """Get ship type name from AIS type code."""
    if code is None:
        return "Unknown"
    code = int(code)
    # Check exact match first
    if code in SHIP_TYPES:
        return SHIP_TYPES[code]
    # Check decade range (e.g., 70-79 are all cargo)
    decade = (code // 10) * 10
    if decade in SHIP_TYPES:
        return SHIP_TYPES[decade]
    return f"Type {code}"


# Store latest ship positions (AIS sends updates per-ship)
ship_positions = {}


def collect_ships_snapshot():
    """
    AISStream uses WebSockets for real-time data. This function connects
    briefly to collect current ship positions in the ME region.
    Requires the 'websockets' package: pip install websockets
    """
    if not AISSTREAM_API_KEY:
        print("  Ships: No API key set. Get free key at https://aisstream.io")
        return []

    try:
        import asyncio
        import websockets
    except ImportError:
        print("  Ships: Install websockets package: pip install websockets")
        return []

    async def _collect():
        subscribe = {
            "APIKey": AISSTREAM_API_KEY,
            "BoundingBoxes": [ME_BBOX],
            "FilterMessageTypes": ["PositionReport", "ShipStaticData"],
        }

        collected = {}
        try:
            async with websockets.connect("wss://stream.aisstream.io/v0/stream") as ws:
                await ws.send(json.dumps(subscribe))

                # Collect for 30 seconds to get a good snapshot
                end_time = time.time() + 30
                while time.time() < end_time:
                    try:
                        msg = await asyncio.wait_for(ws.recv(), timeout=5)
                        data = json.loads(msg)
                        msg_type = data.get("MessageType", "")
                        meta = data.get("MetaData", {})
                        mmsi = str(meta.get("MMSI", ""))

                        if not mmsi:
                            continue

                        if mmsi not in collected:
                            collected[mmsi] = {
                                "mmsi": mmsi,
                                "name": meta.get("ShipName", "").strip() or "Unknown",
                                "lat": meta.get("latitude"),
                                "lon": meta.get("longitude"),
                                "country": meta.get("country", "Unknown"),
                            }

                        if msg_type == "PositionReport":
                            pos = data.get("Message", {}).get("PositionReport", {})
                            collected[mmsi].update({
                                "lat": pos.get("Latitude", collected[mmsi]["lat"]),
                                "lon": pos.get("Longitude", collected[mmsi]["lon"]),
                                "speed": pos.get("Sog"),
                                "course": pos.get("Cog"),
                                "heading": pos.get("TrueHeading"),
                                "nav_status": pos.get("NavigationalStatus"),
                            })

                        elif msg_type == "ShipStaticData":
                            static = data.get("Message", {}).get("ShipStaticData", {})
                            collected[mmsi].update({
                                "ship_type": get_ship_type(static.get("Type")),
                                "destination": static.get("Destination", "").strip() or "N/A",
                                "callsign": static.get("CallSign", "").strip() or "N/A",
                            })

                    except asyncio.TimeoutError:
                        continue

        except Exception as e:
            print(f"  AISStream error: {e}")

        return list(collected.values())

    return asyncio.run(_collect())


def update_ships():
    """Fetch ship data and write KML + JSON."""
    print("Fetching Middle East ship data...")

    ships = collect_ships_snapshot()
    if not ships:
        # Write empty files so dashboard doesn't break
        now_ist = (datetime.utcnow() + IST_OFFSET).strftime("%d %b %Y, %I:%M:%S %p IST")
        with open("ships.json", "w") as f:
            json.dump({"updated": now_ist, "total": 0, "ships": [],
                        "status": "no_key" if not AISSTREAM_API_KEY else "no_data"}, f)
        return

    print(f"  {len(ships)} vessels tracked")

    now_ist = (datetime.utcnow() + IST_OFFSET).strftime("%d %b %Y, %I:%M:%S %p IST")

    # --- KML ---
    kml = '<?xml version="1.0" encoding="UTF-8"?>\n'
    kml += '<kml xmlns="http://www.opengis.net/kml/2.2">\n<Document>\n'
    kml += '<name>Live Ships - Middle East</name>\n'

    kml += """<Style id="ship_moving">
  <IconStyle>
    <color>ffffff00</color>
    <scale>0.8</scale>
    <Icon><href>http://maps.google.com/mapfiles/kml/shapes/sailing.png</href></Icon>
  </IconStyle>
  <BalloonStyle><bgColor>ff1a1a1a</bgColor><textColor>ffffffff</textColor></BalloonStyle>
</Style>
<Style id="ship_anchored">
  <IconStyle>
    <color>ff88aaff</color>
    <scale>0.6</scale>
    <Icon><href>http://maps.google.com/mapfiles/kml/shapes/sailing.png</href></Icon>
  </IconStyle>
  <BalloonStyle><bgColor>ff1a1a1a</bgColor><textColor>ffffffff</textColor></BalloonStyle>
</Style>
"""

    ships_json = []
    for ship in ships:
        lat = ship.get("lat")
        lon = ship.get("lon")
        if lat is None or lon is None:
            continue

        name = ship.get("name", "Unknown")
        mmsi = ship.get("mmsi", "N/A")
        country = ship.get("country", "Unknown")
        ship_type = ship.get("ship_type", "Unknown")
        speed = ship.get("speed")
        course = ship.get("course")
        destination = ship.get("destination", "N/A")
        callsign = ship.get("callsign", "N/A")

        speed_str = f"{speed} kts" if speed is not None else "N/A"
        course_str = f"{round(course)}deg" if course is not None else "N/A"
        is_moving = speed is not None and speed > 0.5
        style = "ship_moving" if is_moving else "ship_anchored"

        desc = f"""<![CDATA[
<style>
  table {{ font-family: Arial; font-size: 13px; border-collapse: collapse; width: 100%; }}
  td {{ padding: 4px 8px; border-bottom: 1px solid #444; }}
  .label {{ color: #aaa; font-weight: bold; white-space: nowrap; }}
  .val {{ color: #fff; }}
  .header {{ background: #069; color: white; padding: 6px 8px; font-size: 14px; font-weight: bold; }}
</style>
<div style="background:#1a1a1a; border-radius:6px; overflow:hidden; min-width:260px;">
  <div class="header">Vessel: {name}</div>
  <table>
    <tr><td class="label">MMSI</td><td class="val">{mmsi}</td></tr>
    <tr><td class="label">Callsign</td><td class="val">{callsign}</td></tr>
    <tr><td class="label">Type</td><td class="val">{ship_type}</td></tr>
    <tr><td class="label">Country</td><td class="val">{country}</td></tr>
    <tr><td class="label">Speed</td><td class="val">{speed_str}</td></tr>
    <tr><td class="label">Course</td><td class="val">{course_str}</td></tr>
    <tr><td class="label">Destination</td><td class="val">{destination}</td></tr>
    <tr><td class="label">Position</td><td class="val">{lat}, {lon}</td></tr>
  </table>
</div>
]]>"""

        kml += f"""<Placemark>
  <name>{name} ({country})</name>
  <description>{desc}</description>
  <styleUrl>#{style}</styleUrl>
  <Point><coordinates>{lon},{lat},0</coordinates></Point>
</Placemark>
"""

        ships_json.append({
            "name": name,
            "mmsi": mmsi,
            "callsign": callsign,
            "type": ship_type,
            "country": country,
            "lat": lat,
            "lon": lon,
            "speed": speed_str,
            "course": course_str,
            "destination": destination,
            "moving": is_moving,
        })

    kml += "</Document>\n</kml>"

    with open("LiveShips.kml", "w") as f:
        f.write(kml)

    with open("ships.json", "w") as f:
        json.dump({"updated": now_ist, "total": len(ships_json), "ships": ships_json, "status": "ok"}, f)

    print(f"  LiveShips.kml + ships.json updated ({len(ships_json)} vessels)")


if __name__ == "__main__":
    while True:
        update_ships()
        time.sleep(60)
