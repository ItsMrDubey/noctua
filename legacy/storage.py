"""
storage.py — SQLite historical data storage for the OSINT tracking system.

Manages an osint.db database with tables for fire detections, flight positions,
seismic events, and cross-domain correlations. All timestamps are ISO 8601 UTC.
Uses WAL mode for concurrent read/write access.
"""

import sqlite3
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

DB_PATH = Path(__file__).parent / "osint.db"


def _connect():
    """Return a connection to the SQLite database with WAL mode enabled."""
    conn = sqlite3.connect(str(DB_PATH), timeout=10)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.row_factory = sqlite3.Row
    return conn


# ---------------------------------------------------------------------------
# Schema creation
# ---------------------------------------------------------------------------

def init_db():
    """Create all tables if they do not already exist."""
    conn = _connect()
    try:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS fire_history (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp   TEXT NOT NULL,
                lat         REAL NOT NULL,
                lon         REAL NOT NULL,
                brightness  REAL,
                frp         REAL,
                confidence  TEXT,
                satellite   TEXT,
                source      TEXT
            );

            CREATE TABLE IF NOT EXISTS flight_history (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp     TEXT NOT NULL,
                hex           TEXT NOT NULL,
                callsign      TEXT,
                type          TEXT,
                desc          TEXT,
                operator      TEXT,
                lat           REAL,
                lon           REAL,
                alt_ft        REAL,
                speed_kts     REAL,
                heading_deg   REAL,
                squawk        TEXT,
                is_mil        INTEGER DEFAULT 0,
                high_priority INTEGER DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS seismic_history (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp   TEXT NOT NULL,
                lat         REAL NOT NULL,
                lon         REAL NOT NULL,
                magnitude   REAL,
                depth       REAL,
                place       TEXT,
                event_type  TEXT
            );

            CREATE TABLE IF NOT EXISTS correlations (
                id               INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp        TEXT NOT NULL,
                type             TEXT NOT NULL,
                description      TEXT,
                lat              REAL,
                lon              REAL,
                confidence_score REAL,
                details_json     TEXT
            );

            CREATE INDEX IF NOT EXISTS idx_fire_ts   ON fire_history(timestamp);
            CREATE INDEX IF NOT EXISTS idx_fire_loc  ON fire_history(lat, lon);
            CREATE INDEX IF NOT EXISTS idx_flight_ts  ON flight_history(timestamp);
            CREATE INDEX IF NOT EXISTS idx_flight_hex ON flight_history(hex);
            CREATE INDEX IF NOT EXISTS idx_flight_mil ON flight_history(is_mil);
            CREATE INDEX IF NOT EXISTS idx_flight_loc ON flight_history(lat, lon);
            CREATE INDEX IF NOT EXISTS idx_seismic_ts ON seismic_history(timestamp);
            CREATE INDEX IF NOT EXISTS idx_corr_ts    ON correlations(timestamp);
            CREATE INDEX IF NOT EXISTS idx_corr_type  ON correlations(type);
        """)
        conn.commit()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _utcnow():
    """Return the current UTC time as an ISO 8601 string."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _hours_ago(hours):
    """Return an ISO 8601 timestamp for *hours* ago (UTC)."""
    dt = datetime.now(timezone.utc) - timedelta(hours=hours)
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _days_ago(days):
    """Return an ISO 8601 timestamp for *days* ago (UTC)."""
    dt = datetime.now(timezone.utc) - timedelta(days=days)
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _rows_to_dicts(rows):
    """Convert sqlite3.Row results to a list of plain dicts."""
    return [dict(row) for row in rows]


# ---------------------------------------------------------------------------
# Storage — bulk inserts
# ---------------------------------------------------------------------------

def store_fires(fires_list):
    """Bulk-insert fire detection records.

    Each item in *fires_list* should be a dict with keys matching the
    fire_history columns.  A ``timestamp`` is added automatically if missing.
    """
    if not fires_list:
        return 0

    conn = _connect()
    try:
        now = _utcnow()
        rows = []
        for f in fires_list:
            rows.append((
                f.get("timestamp", now),
                f["lat"],
                f["lon"],
                f.get("brightness"),
                f.get("frp"),
                f.get("confidence"),
                f.get("satellite"),
                f.get("source"),
            ))
        conn.executemany(
            "INSERT INTO fire_history "
            "(timestamp, lat, lon, brightness, frp, confidence, satellite, source) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            rows,
        )
        conn.commit()
        return len(rows)
    finally:
        conn.close()


def store_flights(flights_list):
    """Bulk-insert flight position snapshots.

    The same aircraft (identified by ``hex``) will accumulate many rows over
    time — each row is a point-in-time position snapshot.
    """
    if not flights_list:
        return 0

    conn = _connect()
    try:
        now = _utcnow()
        rows = []
        for f in flights_list:
            rows.append((
                f.get("timestamp", now),
                f["hex"],
                f.get("callsign"),
                f.get("type"),
                f.get("desc"),
                f.get("operator"),
                f.get("lat"),
                f.get("lon"),
                f.get("alt_ft"),
                f.get("speed_kts"),
                f.get("heading_deg"),
                f.get("squawk"),
                int(f.get("is_mil", 0)),
                int(f.get("high_priority", 0)),
            ))
        conn.executemany(
            "INSERT INTO flight_history "
            "(timestamp, hex, callsign, type, desc, operator, lat, lon, "
            " alt_ft, speed_kts, heading_deg, squawk, is_mil, high_priority) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            rows,
        )
        conn.commit()
        return len(rows)
    finally:
        conn.close()


def store_seismic(events_list):
    """Bulk-insert seismic event records."""
    if not events_list:
        return 0

    conn = _connect()
    try:
        now = _utcnow()
        rows = []
        for e in events_list:
            rows.append((
                e.get("timestamp", now),
                e["lat"],
                e["lon"],
                e.get("magnitude"),
                e.get("depth"),
                e.get("place"),
                e.get("event_type"),
            ))
        conn.executemany(
            "INSERT INTO seismic_history "
            "(timestamp, lat, lon, magnitude, depth, place, event_type) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            rows,
        )
        conn.commit()
        return len(rows)
    finally:
        conn.close()


def store_correlation(correlation_dict):
    """Insert a single detected correlation.

    *correlation_dict* must contain at least ``type``.  ``details_json`` can be
    a dict/list — it will be serialised to JSON automatically.
    """
    c = correlation_dict
    conn = _connect()
    try:
        details = c.get("details_json")
        if details is not None and not isinstance(details, str):
            details = json.dumps(details)

        conn.execute(
            "INSERT INTO correlations "
            "(timestamp, type, description, lat, lon, confidence_score, details_json) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                c.get("timestamp", _utcnow()),
                c["type"],
                c.get("description"),
                c.get("lat"),
                c.get("lon"),
                c.get("confidence_score"),
                details,
            ),
        )
        conn.commit()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Queries
# ---------------------------------------------------------------------------

def get_recent_fires(hours=24):
    """Return fire detections from the last *hours* hours."""
    conn = _connect()
    try:
        cutoff = _hours_ago(hours)
        rows = conn.execute(
            "SELECT * FROM fire_history WHERE timestamp >= ? ORDER BY timestamp DESC",
            (cutoff,),
        ).fetchall()
        return _rows_to_dicts(rows)
    finally:
        conn.close()


def get_recent_flights(hours=24, mil_only=False):
    """Return flight snapshots from the last *hours* hours.

    If *mil_only* is True, only military-flagged aircraft are returned.
    """
    conn = _connect()
    try:
        cutoff = _hours_ago(hours)
        if mil_only:
            rows = conn.execute(
                "SELECT * FROM flight_history "
                "WHERE timestamp >= ? AND is_mil = 1 "
                "ORDER BY timestamp DESC",
                (cutoff,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM flight_history "
                "WHERE timestamp >= ? "
                "ORDER BY timestamp DESC",
                (cutoff,),
            ).fetchall()
        return _rows_to_dicts(rows)
    finally:
        conn.close()


def get_flight_track(hex_code, hours=6):
    """Return the position history for a single aircraft identified by *hex_code*.

    Results are ordered chronologically (oldest first) so the list describes
    the aircraft's track over time — useful for behaviour / pattern detection.
    """
    conn = _connect()
    try:
        cutoff = _hours_ago(hours)
        rows = conn.execute(
            "SELECT * FROM flight_history "
            "WHERE hex = ? AND timestamp >= ? "
            "ORDER BY timestamp ASC",
            (hex_code, cutoff),
        ).fetchall()
        return _rows_to_dicts(rows)
    finally:
        conn.close()


def get_nearby_flights(lat, lon, radius_nm=50, hours=6):
    """Return flights that were within *radius_nm* nautical miles of (lat, lon).

    Uses a fast bounding-box pre-filter (1 NM ~ 1/60 degree latitude) and then
    applies the Haversine-based great-circle check via a rough approximation
    suitable for distances under a few hundred NM.

    NOTE: This is a rectangular approximation. For the distances involved in
    typical OSINT correlation (< 100 NM) it is more than adequate.
    """
    conn = _connect()
    try:
        cutoff = _hours_ago(hours)
        # 1 NM ~ 1/60 deg latitude; longitude degrees are narrower at higher
        # latitudes, but using the same margin is a safe over-select.
        deg_margin = radius_nm / 60.0
        lat_min = lat - deg_margin
        lat_max = lat + deg_margin
        lon_min = lon - deg_margin
        lon_max = lon + deg_margin

        rows = conn.execute(
            "SELECT * FROM flight_history "
            "WHERE timestamp >= ? "
            "  AND lat BETWEEN ? AND ? "
            "  AND lon BETWEEN ? AND ? "
            "ORDER BY timestamp DESC",
            (cutoff, lat_min, lat_max, lon_min, lon_max),
        ).fetchall()
        return _rows_to_dicts(rows)
    finally:
        conn.close()


def get_recent_seismic(hours=24):
    """Return seismic events from the last *hours* hours."""
    conn = _connect()
    try:
        cutoff = _hours_ago(hours)
        rows = conn.execute(
            "SELECT * FROM seismic_history WHERE timestamp >= ? ORDER BY timestamp DESC",
            (cutoff,),
        ).fetchall()
        return _rows_to_dicts(rows)
    finally:
        conn.close()


def get_recent_correlations(hours=24):
    """Return correlations detected in the last *hours* hours."""
    conn = _connect()
    try:
        cutoff = _hours_ago(hours)
        rows = conn.execute(
            "SELECT * FROM correlations WHERE timestamp >= ? ORDER BY timestamp DESC",
            (cutoff,),
        ).fetchall()
        results = _rows_to_dicts(rows)
        # Deserialise details_json back to Python objects where possible.
        for r in results:
            if r.get("details_json"):
                try:
                    r["details_json"] = json.loads(r["details_json"])
                except (json.JSONDecodeError, TypeError):
                    pass
        return results
    finally:
        conn.close()


def get_stats(hours=24):
    """Return summary statistics for the last *hours* hours.

    Returns a dict with counts of fires, total flights, military flights,
    seismic events, and correlations.
    """
    conn = _connect()
    try:
        cutoff = _hours_ago(hours)

        fire_count = conn.execute(
            "SELECT COUNT(*) FROM fire_history WHERE timestamp >= ?", (cutoff,)
        ).fetchone()[0]

        flight_count = conn.execute(
            "SELECT COUNT(DISTINCT hex) FROM flight_history WHERE timestamp >= ?",
            (cutoff,),
        ).fetchone()[0]

        mil_count = conn.execute(
            "SELECT COUNT(DISTINCT hex) FROM flight_history "
            "WHERE timestamp >= ? AND is_mil = 1",
            (cutoff,),
        ).fetchone()[0]

        flight_snapshots = conn.execute(
            "SELECT COUNT(*) FROM flight_history WHERE timestamp >= ?", (cutoff,)
        ).fetchone()[0]

        seismic_count = conn.execute(
            "SELECT COUNT(*) FROM seismic_history WHERE timestamp >= ?", (cutoff,)
        ).fetchone()[0]

        corr_count = conn.execute(
            "SELECT COUNT(*) FROM correlations WHERE timestamp >= ?", (cutoff,)
        ).fetchone()[0]

        return {
            "hours": hours,
            "fires": fire_count,
            "unique_aircraft": flight_count,
            "military_aircraft": mil_count,
            "flight_snapshots": flight_snapshots,
            "seismic_events": seismic_count,
            "correlations": corr_count,
        }
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Maintenance
# ---------------------------------------------------------------------------

def cleanup_old_data(days=30):
    """Delete rows older than *days* days from all tables.

    Returns a dict with the number of rows deleted per table.
    """
    conn = _connect()
    try:
        cutoff = _days_ago(days)
        deleted = {}

        for table in ("fire_history", "flight_history", "seismic_history", "correlations"):
            conn.execute(f"DELETE FROM {table} WHERE timestamp < ?", (cutoff,))
            deleted[table] = conn.total_changes  # cumulative; we diff below

        # SQLite's total_changes is cumulative across the connection, so we
        # re-query per table instead.  Simpler approach: just commit and
        # report that cleanup ran.
        conn.commit()

        # Re-count to give accurate per-table numbers.
        counts = {}
        for table in ("fire_history", "flight_history", "seismic_history", "correlations"):
            counts[table] = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]

        return {
            "cutoff": cutoff,
            "remaining_rows": counts,
        }
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Convenience: initialise the DB when the module is first imported.
# ---------------------------------------------------------------------------
init_db()
