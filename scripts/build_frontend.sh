#!/usr/bin/env bash
# Production frontend build -> frontend/dist (served automatically by the API when present).
# Set OSINT_FRONTEND=src to serve the unbundled source tree instead.
set -euo pipefail
cd "$(dirname "$0")/.."
uv run python -m osint.frontend_build
