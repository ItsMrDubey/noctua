#!/usr/bin/env bash
# Download the CesiumJS build into frontend/vendor/cesium (git-ignored).
# Serving Cesium locally avoids a slow 6 MB CDN load on every page view.
set -euo pipefail
VERSION="${1:-1.147.0}"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
TMP="$(mktemp -d)"
curl -fsSL -o "$TMP/cesium.tgz" "https://registry.npmjs.org/cesium/-/cesium-$VERSION.tgz"
tar -xzf "$TMP/cesium.tgz" -C "$TMP" package/Build/Cesium
rm -rf "$ROOT/frontend/vendor/cesium"
mkdir -p "$ROOT/frontend/vendor"
mv "$TMP/package/Build/Cesium" "$ROOT/frontend/vendor/cesium"
rm -rf "$TMP"
echo "CesiumJS $VERSION installed in frontend/vendor/cesium"
