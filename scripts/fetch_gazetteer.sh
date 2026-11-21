#!/usr/bin/env bash
# Download the GeoNames offline gazetteer into data/gazetteer (git-ignored).
# Data: GeoNames (https://www.geonames.org), licensed CC-BY 4.0 — attribution required.
# Files: cities15000.zip (places with population >= 15,000, incl. multilingual
# alternate names), countryInfo.txt (ISO codes, names, capitals) and
# admin1CodesASCII.txt (first-level administrative divisions).
# Only data is downloaded; nothing is executed. The gazetteer index cache
# (data/gazetteer/index.sqlite) is rebuilt automatically when these files change.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
DEST="$ROOT/data/gazetteer"
BASE="https://download.geonames.org/export/dump"
mkdir -p "$DEST"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
for f in cities15000.zip countryInfo.txt admin1CodesASCII.txt; do
  curl -fsSL --retry 3 -m 300 -o "$TMP/$f" "$BASE/$f"
  mv "$TMP/$f" "$DEST/$f"
done
rm -f "$DEST/index.sqlite"
echo "GeoNames gazetteer downloaded to $DEST (CC-BY 4.0, attribution: GeoNames)"
