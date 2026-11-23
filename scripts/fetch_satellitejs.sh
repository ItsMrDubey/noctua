#!/usr/bin/env bash
# Download the satellite.js UMD build (SGP4, MIT) into frontend/vendor/satellite (git-ignored).
# The satellite layer's worker falls back to the same pinned version on jsDelivr / cdnjs when
# the vendored copy is missing.
set -euo pipefail
VERSION="${1:-6.0.1}"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
TMP="$(mktemp -d)"
curl -fsSL -o "$TMP/sat.tgz" "https://registry.npmjs.org/satellite.js/-/satellite.js-$VERSION.tgz"
tar -xzf "$TMP/sat.tgz" -C "$TMP" package/dist/satellite.min.js package/LICENSE.md
mkdir -p "$ROOT/frontend/vendor/satellite"
mv "$TMP/package/dist/satellite.min.js" "$TMP/package/LICENSE.md" "$ROOT/frontend/vendor/satellite/"
rm -rf "$TMP"
echo "satellite.js $VERSION installed in frontend/vendor/satellite"
