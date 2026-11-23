"""Extension points for feature branches that add data to the intel pane.

TRACK_FLAGGERS   callables(runtime, tracks: list[dict]) -> None that add entries to each
                 track's ``flags`` list in place, e.g. {"kind": "sanctions", "label": ...,
                 "source": ...} for a watchlist hit. Run on nearby / passing aircraft and ships.
ASSET_PROVIDERS  callables(runtime, lat, lon, radius_km) -> list[dict] of nearby reference
                 assets: {"type", "name", "lat", "lon", "source"[, "detail", "url"]};
                 distance / bearing are added by the aggregator (infrastructure layers).

A failing hook is logged and skipped; it never breaks the pane.
"""

from collections.abc import Callable

TRACK_FLAGGERS: list[Callable] = []
ASSET_PROVIDERS: list[Callable] = []
