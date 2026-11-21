#!/usr/bin/env bash
# Download public-domain reference datasets into data/reference/ (idempotent; existing files are kept).
set -u
DIR="$(cd "$(dirname "$0")/.." && pwd)/data/reference"
mkdir -p "$DIR"
NE=https://raw.githubusercontent.com/nvkelso/natural-earth-vector/master/geojson

get() { # url dest
  if [ -s "$DIR/$2" ]; then echo "have $2"; return 0; fi
  if curl -fsSL -m 300 -o "$DIR/$2.part" "$1"; then mv "$DIR/$2.part" "$DIR/$2"; echo "ok   $2"; return 0; fi
  rm -f "$DIR/$2.part"; echo "FAIL $2 ($1)"; return 1
}

get https://davidmegginson.github.io/ourairports-data/airports.csv airports.csv
get https://davidmegginson.github.io/ourairports-data/runways.csv runways.csv
for f in ne_50m_admin_0_boundary_lines_land ne_10m_admin_0_boundary_lines_disputed_areas \
         ne_10m_admin_0_boundary_lines_maritime_indicator ne_50m_admin_0_countries; do
  get "$NE/$f.geojson" "$f.geojson"
done
# NGA World Port Index; fall back to Natural Earth ports when NGA is unavailable (it returned 503 in testing).
if ! get "https://msi.nga.mil/api/publications/download?key=16694622/SFH00000/UpdatedPub150.csv&type=download" wpi.csv \
   || head -c 200 "$DIR/wpi.csv" 2>/dev/null | grep -qi "<!doctype\|<html"; then
  rm -f "$DIR/wpi.csv"
  echo "WPI unavailable -> falling back to Natural Earth ports"
  get "$NE/ne_10m_ports.geojson" ne_10m_ports.geojson
fi
