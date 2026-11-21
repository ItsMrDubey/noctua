"""
OSINT ME Command Center
Runs all data fetchers, correlation engine, and serves the dashboard.
Usage: python serve.py
Then open http://localhost:8080/dashboard.html
"""
import http.server
import threading
import os
import time

os.chdir(os.path.dirname(os.path.abspath(__file__)))

from storage import init_db, cleanup_old_data
from fire_data import update_fires
from flight_data import update_flights
from ship_data import update_ships
from seismic_data import update_seismic
from correlation import run_all_correlations


def fire_loop():
    while True:
        try:
            update_fires()
        except Exception as e:
            print(f"[Fire error]: {e}")
        time.sleep(120)


def flight_loop():
    while True:
        try:
            update_flights()
        except Exception as e:
            print(f"[Flight error]: {e}")
        time.sleep(15)


def ship_loop():
    while True:
        try:
            update_ships()
        except Exception as e:
            print(f"[Ship error]: {e}")
        time.sleep(60)


def seismic_loop():
    while True:
        try:
            update_seismic()
        except Exception as e:
            print(f"[Seismic error]: {e}")
        time.sleep(120)


def correlation_loop():
    # Wait for initial data to accumulate
    time.sleep(30)
    while True:
        try:
            run_all_correlations()
        except Exception as e:
            print(f"[Correlation error]: {e}")
        time.sleep(60)


def cleanup_loop():
    while True:
        try:
            cleanup_old_data(days=30)
        except Exception:
            pass
        time.sleep(86400)  # Daily cleanup


print("=" * 55)
print("   OSINT // ME COMMAND CENTER")
print("   Intelligence, Surveillance & Reconnaissance")
print("=" * 55)

# Initialize database
init_db()
print("[DB] SQLite database initialized")

# Generate bases KML if available
try:
    from bases import generate_bases_kml, get_bases_json
    import json
    generate_bases_kml()
    with open("bases.json", "w") as f:
        json.dump(get_bases_json(), f)
    print("[Bases] KML + JSON generated")
except Exception as e:
    print(f"[Bases] Not available: {e}")

# Initial data fetch
print("\n[Init] Fetching initial data...")
update_fires()
update_flights()
update_ships()
update_seismic()

# Start background threads
loops = [
    ("Fires", fire_loop),
    ("Flights", flight_loop),
    ("Ships", ship_loop),
    ("Seismic", seismic_loop),
    ("Correlations", correlation_loop),
    ("Cleanup", cleanup_loop),
]

for name, loop_fn in loops:
    t = threading.Thread(target=loop_fn, daemon=True, name=name)
    t.start()

PORT = 8080
handler = http.server.SimpleHTTPRequestHandler
handler.extensions_map.update({".json": "application/json"})

print(f"\n{'=' * 55}")
print(f"   Dashboard: http://localhost:{PORT}/dashboard.html")
print(f"   Fires: 2min | Flights: 15s | Seismic: 2min")
print(f"   Correlations: 1min | Ships: 1min")
print(f"   Historical data: osint.db (SQLite)")
print(f"   Press Ctrl+C to stop")
print(f"{'=' * 55}\n")

with http.server.HTTPServer(("", PORT), handler) as httpd:
    httpd.serve_forever()
