"""Signals & space layers: GNSS interference, emergency squawks, satellites, internet outages,
space weather and radiation.

The collectors (``osint.collectors.adsb_signals`` / ``celestrak`` / ``ioda`` / ``swpc`` /
``radiation``) run in the worker. Each one writes a small live snapshot to
``data_dir/signals/<name>.json`` (served by ``osint.api.signals``; the API process makes no
outbound requests) and, where the record has a place and a history worth keeping, lake
observations. ``osint.signals.job`` turns notable signals into sensor-derived events.
"""
