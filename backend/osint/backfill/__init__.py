"""Historical backfill: ``osint backfill <source>`` (gdelt, usgs, firms, ucdp, adsb_history).

Runs as a separate process next to the live server. It writes the same Parquet lake
(append-only, unique part files) and SQLite (WAL, busy timeout) and checkpoints each
finished unit (day / chunk / file) in ``source_state`` as ``backfill:<source>``.
"""

SOURCES = ("gdelt", "usgs", "firms", "ucdp", "adsb_history")
