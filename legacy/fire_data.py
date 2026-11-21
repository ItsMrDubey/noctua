import urllib.request
import csv
import json
import time
from datetime import datetime, timedelta
from html import escape
from storage import init_db, store_fires

# NASA FIRMS Map Key
NASA_MAP_KEY = __import__("os").environ.get("FIRMS_MAP_KEY", "")

# Middle East Bounding Box (West, South, East, North)
BOUNDING_BOX = "34,12,55,38"
SOURCES = ["MODIS_NRT", "VIIRS_NOAA21_NRT", "VIIRS_NOAA20_NRT", "VIIRS_SNPP_NRT"]
DAYS = "1"

BASE_URL = "https://firms.modaps.eosdis.nasa.gov/api/area/csv"

IST_OFFSET = timedelta(hours=5, minutes=30)


def utc_to_ist(acq_date, acq_time):
    """Convert FIRMS acq_date + acq_time (HHMM in UTC) to IST string."""
    time_str = str(acq_time).zfill(4)
    utc_dt = datetime.strptime(f"{acq_date} {time_str}", "%Y-%m-%d %H%M")
    ist_dt = utc_dt + IST_OFFSET
    return ist_dt.strftime("%d %b %Y, %I:%M %p IST")


def get_brightness(row):
    """Get brightness temp from either MODIS or VIIRS fields."""
    return row.get("brightness") or row.get("bright_ti4", "N/A")


def get_confidence_label(conf):
    """Normalize confidence across MODIS (0-100) and VIIRS (l/n/h)."""
    mapping = {"l": "Low", "n": "Nominal", "h": "High"}
    if conf in mapping:
        return mapping[conf]
    try:
        val = int(conf)
        if val >= 80:
            return f"High ({val}%)"
        elif val >= 30:
            return f"Nominal ({val}%)"
        else:
            return f"Low ({val}%)"
    except (ValueError, TypeError):
        return str(conf)


def fetch_fires_from_source(source):
    url = f"{BASE_URL}/{NASA_MAP_KEY}/{source}/{BOUNDING_BOX}/{DAYS}"
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    response = urllib.request.urlopen(req, timeout=30)
    lines = [l.decode("utf-8") for l in response.readlines()]
    reader = csv.DictReader(lines)
    rows = list(reader)
    for row in rows:
        row["_source"] = source
    return rows


def build_description(row):
    """Build an HTML description table for the KML popup."""
    ist_time = utc_to_ist(row["acq_date"], row["acq_time"])
    brightness = get_brightness(row)
    confidence = get_confidence_label(row.get("confidence", ""))
    frp = row.get("frp", "N/A")
    satellite = row.get("satellite", "N/A")
    instrument = row.get("instrument", "N/A")
    daynight = "Day" if row.get("daynight") == "D" else "Night"
    scan = row.get("scan", "N/A")
    track = row.get("track", "N/A")

    return f"""<![CDATA[
<style>
  table {{ font-family: Arial; font-size: 13px; border-collapse: collapse; width: 100%; }}
  td {{ padding: 4px 8px; border-bottom: 1px solid #444; }}
  .label {{ color: #aaa; font-weight: bold; white-space: nowrap; }}
  .val {{ color: #fff; }}
  .header {{ background: #b22; color: white; padding: 6px 8px; font-size: 14px; font-weight: bold; }}
</style>
<div style="background:#1a1a1a; border-radius:6px; overflow:hidden; min-width:250px;">
  <div class="header">Fire Detection</div>
  <table>
    <tr><td class="label">Time (IST)</td><td class="val">{escape(ist_time)}</td></tr>
    <tr><td class="label">Coordinates</td><td class="val">{row['latitude']}, {row['longitude']}</td></tr>
    <tr><td class="label">Brightness</td><td class="val">{brightness} K</td></tr>
    <tr><td class="label">Fire Power (FRP)</td><td class="val">{frp} MW</td></tr>
    <tr><td class="label">Confidence</td><td class="val">{confidence}</td></tr>
    <tr><td class="label">Satellite</td><td class="val">{escape(str(satellite))} ({escape(str(instrument))})</td></tr>
    <tr><td class="label">Day/Night</td><td class="val">{daynight}</td></tr>
    <tr><td class="label">Scan/Track</td><td class="val">{scan} / {track}</td></tr>
  </table>
</div>
]]>"""


def update_fires():
    """Fetches fire data from multiple satellite sources and writes KML + JSON."""
    print("Fetching Middle East fire data...")

    all_fires = []
    seen = set()
    for source in SOURCES:
        try:
            rows = fetch_fires_from_source(source)
            for row in rows:
                key = (row["latitude"], row["longitude"])
                if key not in seen:
                    seen.add(key)
                    all_fires.append(row)
            print(f"  {source}: {len(rows)} detections")
        except Exception as e:
            print(f"  {source}: failed ({e})")

    print(f"Total unique fires: {len(all_fires)}")

    kml = '<?xml version="1.0" encoding="UTF-8"?>\n'
    kml += '<kml xmlns="http://www.opengis.net/kml/2.2">\n<Document>\n'
    kml += '<name>Live Fires - Middle East</name>\n'

    # Styles for different fire intensities based on FRP
    for size_id, color, scale in [
        ("fire_low", "ff4444ff", 0.7),
        ("fire_nom", "ff0066ff", 0.9),
        ("fire_high", "ff0000ff", 1.2),
        ("fire_extreme", "ff00ffff", 1.5),
    ]:
        kml += f"""<Style id="{size_id}">
  <IconStyle>
    <color>{color}</color>
    <scale>{scale}</scale>
    <Icon><href>http://maps.google.com/mapfiles/kml/shapes/placemark_circle.png</href></Icon>
  </IconStyle>
  <BalloonStyle>
    <bgColor>ff1a1a1a</bgColor>
    <textColor>ffffffff</textColor>
  </BalloonStyle>
</Style>
"""

    for row in all_fires:
        lat = row["latitude"]
        lon = row["longitude"]
        ist_time = utc_to_ist(row["acq_date"], row["acq_time"])
        frp = row.get("frp", "0")
        confidence = row.get("confidence", "")

        # Pick style based on FRP
        try:
            frp_val = float(frp)
            if frp_val >= 100:
                style = "fire_extreme"
            elif frp_val >= 30:
                style = "fire_high"
            elif frp_val >= 10:
                style = "fire_nom"
            else:
                style = "fire_low"
        except (ValueError, TypeError):
            style = "fire_nom"

        desc = build_description(row)
        conf_label = get_confidence_label(confidence)

        kml += f"""<Placemark>
  <name>Fire ({conf_label}) - {ist_time}</name>
  <description>{desc}</description>
  <styleUrl>#{style}</styleUrl>
  <Point><coordinates>{lon},{lat},0</coordinates></Point>
</Placemark>
"""

    kml += "</Document>\n</kml>"

    with open("LiveFires.kml", "w") as f:
        f.write(kml)

    # Write JSON for the dashboard UI
    fires_json = []
    for row in all_fires:
        ist_time = utc_to_ist(row["acq_date"], row["acq_time"])
        try:
            frp_val = float(row.get("frp", 0))
        except (ValueError, TypeError):
            frp_val = 0
        fires_json.append({
            "time_ist": ist_time,
            "lat": row["latitude"],
            "lon": row["longitude"],
            "brightness": get_brightness(row),
            "frp": row.get("frp", "N/A"),
            "frp_val": frp_val,
            "confidence": get_confidence_label(row.get("confidence", "")),
            "satellite": f"{row.get('satellite', 'N/A')} ({row.get('instrument', 'N/A')})",
            "daynight": "Day" if row.get("daynight") == "D" else "Night",
        })

    fires_json.sort(key=lambda x: x["time_ist"], reverse=True)

    now_ist = (datetime.utcnow() + IST_OFFSET).strftime("%d %b %Y, %I:%M:%S %p IST")
    with open("fires.json", "w") as f:
        json.dump({"updated": now_ist, "total": len(fires_json), "fires": fires_json}, f)

    # Persist to historical DB
    try:
        store_fires(fires_json)
    except Exception as e:
        print(f"  [DB] Fire storage error: {e}")

    print(f"LiveFires.kml + fires.json updated ({len(all_fires)} fires)")


# Run in a loop, updating every 2 minutes
if __name__ == "__main__":
    while True:
        update_fires()
        time.sleep(120)
