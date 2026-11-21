#!/usr/bin/env bash
# Download the GeoNames offline gazetteer into data/gazetteer (git-ignored).
# Data: GeoNames (https://www.geonames.org), licensed CC-BY 4.0 — attribution required.
# Files: cities15000.zip (places with population >= 15,000, incl. multilingual
# alternate names), countryInfo.txt (ISO codes, names, capitals) and
# admin1CodesASCII.txt (first-level administrative divisions), plus cities1000.zip
# (places >= 1,000 people) for the article-extraction geocoder.
# Only data is downloaded; nothing is executed. The gazetteer index cache
# (data/gazetteer/index.sqlite) is rebuilt automatically when these files change.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
DEST="$ROOT/data/gazetteer"
BASE="https://download.geonames.org/export/dump"
mkdir -p "$DEST"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
# ONLY=cities1000 fetches just the extraction geocoder's file (no triage index rebuild)
if [ "${ONLY:-}" != "cities1000" ]; then
  for f in cities15000.zip countryInfo.txt admin1CodesASCII.txt; do
    curl -fsSL --retry 3 -m 300 -o "$TMP/$f" "$BASE/$f"
    mv "$TMP/$f" "$DEST/$f"
  done
  rm -f "$DEST/index.sqlite"
fi
# cities1000.zip (population >= 1,000): the article-extraction geocoder looks places up in
# it constrained to the stated country (triage keeps cities15000). Its lookup index
# (cities1000.sqlite) is rebuilt automatically when the zip changes.
curl -fsSL --retry 3 -m 600 -o "$TMP/cities1000.zip" "$BASE/cities1000.zip"
mv "$TMP/cities1000.zip" "$DEST/cities1000.zip"
rm -f "$DEST/cities1000.sqlite"
echo "GeoNames gazetteer downloaded to $DEST (CC-BY 4.0, attribution: GeoNames)"
