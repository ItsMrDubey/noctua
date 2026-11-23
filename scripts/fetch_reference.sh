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
# NGA World Port Index; NGA's MSI site often serves a 503 apology page, so try the RIMES CKAN
# datastore copy next and fall back to Natural Earth ports when both are unavailable.
wpi_ok() { [ -s "$DIR/wpi.csv" ] && ! head -c 200 "$DIR/wpi.csv" | grep -qi "<!doctype\|<html"; }
get "https://msi.nga.mil/api/publications/download?key=16694622/SFH00000/UpdatedPub150.csv&type=download" wpi.csv
wpi_ok || { rm -f "$DIR/wpi.csv"; get "https://ckan.rimes.int/datastore/dump/23538e38-830f-4df1-b69d-4469fa6ee7af" wpi.csv; }
if ! wpi_ok; then
  rm -f "$DIR/wpi.csv"
  echo "WPI unavailable -> falling back to Natural Earth ports"
  get "$NE/ne_10m_ports.geojson" ne_10m_ports.geojson
fi

# Known nuclear sites from Wikidata (CC0) via the public SPARQL endpoint; raw SPARQL JSON results
# (the loader normalises them). Query: scripts/nuclear_wikidata.rq.
if [ -s "$DIR/nuclear_wikidata.json" ]; then echo "have nuclear_wikidata.json"
elif curl -fsS -G -m 180 -H "User-Agent: osint-poc/0.1 (reference data fetch)" \
       -H "Accept: application/sparql-results+json" \
       --data-urlencode "query@$(dirname "$0")/nuclear_wikidata.rq" \
       -o "$DIR/nuclear_wikidata.json.part" https://query.wikidata.org/sparql \
     && grep -q '"bindings"' "$DIR/nuclear_wikidata.json.part"; then
  mv "$DIR/nuclear_wikidata.json.part" "$DIR/nuclear_wikidata.json"; echo "ok   nuclear_wikidata.json"
else
  rm -f "$DIR/nuclear_wikidata.json.part"; echo "FAIL nuclear_wikidata.json (query.wikidata.org)"
fi

# Critical infrastructure layers + sanctions watchlists (cables, pipelines, power plants,
# refineries, LNG, dams, ground stations, OpenSanctions): `uv run osint infra-fetch`.
