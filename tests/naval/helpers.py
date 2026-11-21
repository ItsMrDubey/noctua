"""Synthetic geography for naval tests: one 'continent' around (0, 0) with an airport."""

from datetime import UTC, datetime, timedelta

import h3

from osint.config import ROOT
from osint.naval.config import NavalConfig
from osint.naval.geo import AirportIndex, Geo, OceanMask

NOW = datetime(2026, 11, 21, 12, 0, tzinfo=UTC)


def synthetic_geo(extra_airports=()) -> Geo:
    land = set(h3.grid_disk(h3.latlng_to_cell(0.0, 0.0, 4), 8))  # ~250 km radius island (ocean beyond ~2.4 deg)
    return Geo(OceanMask(land), AirportIndex([(0.0, 0.0, "XLND"), *extra_airports]))


def cfg() -> NavalConfig:
    return NavalConfig.load(ROOT / "config" / "naval.yaml")


def ts(minutes: float) -> datetime:
    return NOW + timedelta(minutes=minutes)
