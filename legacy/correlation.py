"""
Cross-correlation engine for OSINT data.
Detects patterns across fires, flights, seismic events, and known bases.
"""
import json
import math
import sqlite3
from datetime import datetime, timedelta

IST_OFFSET = timedelta(hours=5, minutes=30)
DB_PATH = "osint.db"


def haversine_nm(lat1, lon1, lat2, lon2):
    """Distance between two points in nautical miles."""
    R = 3440.065  # Earth radius in NM
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = (math.sin(dlat / 2) ** 2 +
         math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) *
         math.sin(dlon / 2) ** 2)
    return R * 2 * math.asin(math.sqrt(a))


def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def correlate_fires_with_flights(hours_back=6, radius_nm=30):
    """
    Find fires that had military aircraft nearby within the time window.
    Returns list of correlations: fire location + nearby mil flights.
    """
    conn = get_db()
    cutoff = (datetime.utcnow() - timedelta(hours=hours_back)).isoformat()

    fires = conn.execute(
        "SELECT DISTINCT lat, lon, MAX(timestamp) as last_seen, "
        "MAX(frp) as max_frp, COUNT(*) as detections "
        "FROM fire_history WHERE timestamp > ? "
        "GROUP BY ROUND(lat, 2), ROUND(lon, 2)", (cutoff,)
    ).fetchall()

    correlations = []
    for fire in fires:
        # Find military flights that were near this fire location
        nearby_flights = conn.execute(
            "SELECT DISTINCT hex, callsign, type, desc, operator, "
            "MIN(timestamp) as first_seen, MAX(timestamp) as last_seen, "
            "MIN(alt_ft) as min_alt "
            "FROM flight_history "
            "WHERE is_mil = 1 AND timestamp > ? "
            "AND ABS(lat - ?) < 1.0 AND ABS(lon - ?) < 1.0 "
            "GROUP BY hex",
            (cutoff, fire["lat"], fire["lon"])
        ).fetchall()

        # Filter by actual distance
        confirmed_flights = []
        for fl in nearby_flights:
            # Get all positions for this aircraft
            positions = conn.execute(
                "SELECT lat, lon, timestamp FROM flight_history "
                "WHERE hex = ? AND timestamp > ? ORDER BY timestamp",
                (fl["hex"], cutoff)
            ).fetchall()

            min_dist = float("inf")
            closest_time = None
            for pos in positions:
                dist = haversine_nm(fire["lat"], fire["lon"], pos["lat"], pos["lon"])
                if dist < min_dist:
                    min_dist = dist
                    closest_time = pos["timestamp"]

            if min_dist <= radius_nm:
                confirmed_flights.append({
                    "callsign": fl["callsign"],
                    "type": fl["type"],
                    "desc": fl["desc"],
                    "operator": fl["operator"],
                    "min_distance_nm": round(min_dist, 1),
                    "closest_time": closest_time,
                    "min_alt_ft": fl["min_alt"],
                })

        if confirmed_flights:
            correlations.append({
                "type": "fire_flight",
                "fire_lat": fire["lat"],
                "fire_lon": fire["lon"],
                "fire_detections": fire["detections"],
                "max_frp": fire["max_frp"],
                "last_fire": fire["last_seen"],
                "nearby_military": confirmed_flights,
                "confidence": min(0.9, 0.3 + 0.1 * len(confirmed_flights) +
                               (0.2 if fire["max_frp"] and float(fire["max_frp"]) > 30 else 0)),
            })

    conn.close()
    return correlations


def correlate_seismic_with_fires(hours_back=12, radius_nm=20):
    """
    Find seismic events near fire detections — possible strike confirmation.
    An explosion + fire at same location = high confidence strike.
    """
    conn = get_db()
    cutoff = (datetime.utcnow() - timedelta(hours=hours_back)).isoformat()

    seismic = conn.execute(
        "SELECT * FROM seismic_history WHERE timestamp > ?", (cutoff,)
    ).fetchall()

    correlations = []
    for event in seismic:
        nearby_fires = conn.execute(
            "SELECT lat, lon, MAX(frp) as max_frp, COUNT(*) as count, "
            "MIN(timestamp) as first, MAX(timestamp) as last "
            "FROM fire_history WHERE timestamp > ? "
            "AND ABS(lat - ?) < 0.5 AND ABS(lon - ?) < 0.5 "
            "GROUP BY ROUND(lat, 2), ROUND(lon, 2)",
            (cutoff, event["lat"], event["lon"])
        ).fetchall()

        confirmed_fires = []
        for fire in nearby_fires:
            dist = haversine_nm(event["lat"], event["lon"], fire["lat"], fire["lon"])
            if dist <= radius_nm:
                confirmed_fires.append({
                    "lat": fire["lat"],
                    "lon": fire["lon"],
                    "max_frp": fire["max_frp"],
                    "detections": fire["count"],
                    "distance_nm": round(dist, 1),
                })

        if confirmed_fires:
            is_explosion = event["event_type"] and "explosion" in event["event_type"].lower()
            correlations.append({
                "type": "seismic_fire",
                "seismic_lat": event["lat"],
                "seismic_lon": event["lon"],
                "magnitude": event["magnitude"],
                "seismic_type": event["event_type"],
                "seismic_place": event["place"],
                "nearby_fires": confirmed_fires,
                "confidence": 0.9 if is_explosion else 0.4,
            })

    conn.close()
    return correlations


def detect_flight_patterns(hex_code, hours_back=4):
    """
    Analyze flight track to detect behavior patterns.
    Returns: 'transit', 'orbiting' (ISR), 'racetrack' (tanker), 'loitering', 'descending'
    """
    conn = get_db()
    cutoff = (datetime.utcnow() - timedelta(hours=hours_back)).isoformat()

    positions = conn.execute(
        "SELECT lat, lon, alt_ft, heading_deg, speed_kts, timestamp "
        "FROM flight_history WHERE hex = ? AND timestamp > ? "
        "ORDER BY timestamp",
        (hex_code, cutoff)
    ).fetchall()

    conn.close()

    if len(positions) < 5:
        return {"pattern": "insufficient_data", "details": {}}

    # Analyze heading changes
    headings = [p["heading_deg"] for p in positions if p["heading_deg"] is not None]
    if len(headings) < 3:
        return {"pattern": "insufficient_data", "details": {}}

    # Calculate total heading change
    total_heading_change = 0
    for i in range(1, len(headings)):
        diff = abs(headings[i] - headings[i - 1])
        if diff > 180:
            diff = 360 - diff
        total_heading_change += diff

    # Calculate area covered (bounding box)
    lats = [p["lat"] for p in positions]
    lons = [p["lon"] for p in positions]
    lat_range = max(lats) - min(lats)
    lon_range = max(lons) - min(lons)
    area_spread_nm = haversine_nm(min(lats), min(lons), max(lats), max(lons))

    # Check altitude trend
    alts = [p["alt_ft"] for p in positions if p["alt_ft"] is not None]
    alt_trend = None
    if len(alts) >= 3:
        alt_change = alts[-1] - alts[0] if alts[-1] and alts[0] else 0
        if alt_change < -5000:
            alt_trend = "descending"
        elif alt_change > 5000:
            alt_trend = "climbing"

    # Duration in minutes
    try:
        t_first = datetime.fromisoformat(positions[0]["timestamp"])
        t_last = datetime.fromisoformat(positions[-1]["timestamp"])
        duration_min = (t_last - t_first).total_seconds() / 60
    except (ValueError, TypeError):
        duration_min = 0

    avg_heading_change_per_point = total_heading_change / max(len(headings) - 1, 1)

    # Pattern detection
    if total_heading_change > 600 and area_spread_nm < 40:
        # Lots of turning in a small area = orbiting (ISR pattern)
        return {
            "pattern": "orbiting",
            "details": {
                "total_heading_change": round(total_heading_change),
                "area_nm": round(area_spread_nm, 1),
                "duration_min": round(duration_min),
                "assessment": "Possible ISR/surveillance mission — aircraft orbiting in confined area",
            }
        }
    elif total_heading_change > 400 and 20 < area_spread_nm < 80:
        # Moderate turning in elongated area = racetrack (tanker pattern)
        return {
            "pattern": "racetrack",
            "details": {
                "total_heading_change": round(total_heading_change),
                "area_nm": round(area_spread_nm, 1),
                "duration_min": round(duration_min),
                "assessment": "Possible tanker/refueling racetrack pattern",
            }
        }
    elif alt_trend == "descending" and area_spread_nm < 30:
        return {
            "pattern": "descending",
            "details": {
                "alt_change_ft": round(alts[-1] - alts[0]) if alts else 0,
                "area_nm": round(area_spread_nm, 1),
                "assessment": "Descending approach — possible landing at nearby base",
            }
        }
    elif area_spread_nm < 15 and duration_min > 30:
        return {
            "pattern": "loitering",
            "details": {
                "area_nm": round(area_spread_nm, 1),
                "duration_min": round(duration_min),
                "assessment": "Loitering in small area — possible holding or on-station",
            }
        }
    else:
        return {
            "pattern": "transit",
            "details": {
                "area_nm": round(area_spread_nm, 1),
                "duration_min": round(duration_min),
                "avg_heading_change": round(avg_heading_change_per_point, 1),
                "assessment": "Normal transit flight",
            }
        }


def run_all_correlations():
    """Run all correlation checks and return combined results."""
    results = {
        "timestamp": (datetime.utcnow() + IST_OFFSET).strftime("%d %b %Y, %I:%M:%S %p IST"),
        "fire_flight": correlate_fires_with_flights(),
        "seismic_fire": correlate_seismic_with_fires(),
        "flight_patterns": {},
    }

    # Detect patterns for all recent military flights
    conn = get_db()
    cutoff = (datetime.utcnow() - timedelta(hours=4)).isoformat()
    mil_flights = conn.execute(
        "SELECT DISTINCT hex, callsign, type, desc FROM flight_history "
        "WHERE is_mil = 1 AND timestamp > ?", (cutoff,)
    ).fetchall()
    conn.close()

    for fl in mil_flights:
        pattern = detect_flight_patterns(fl["hex"])
        if pattern["pattern"] != "insufficient_data":
            results["flight_patterns"][fl["hex"]] = {
                "callsign": fl["callsign"],
                "type": fl["type"],
                "desc": fl["desc"],
                **pattern,
            }

    # Write correlations JSON for dashboard
    with open("correlations.json", "w") as f:
        json.dump(results, f)

    n_ff = len(results["fire_flight"])
    n_sf = len(results["seismic_fire"])
    n_fp = len(results["flight_patterns"])
    print(f"  Correlations: {n_ff} fire-flight, {n_sf} seismic-fire, {n_fp} flight patterns")

    return results


if __name__ == "__main__":
    run_all_correlations()
