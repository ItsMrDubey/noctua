"""Intel tests never reach the network: the Open-Meteo point service is replaced by a canned
forecast, and per-process caches are cleared between tests."""
from datetime import UTC, datetime, timedelta

import pytest

from osint.intel import context

# real CelesTrak GP rows (compact layout, osint.signals.satellites.FIELDS), epoch 2026-11-22/23
SAT_ROWS = {
    "42063": [42063, "SENTINEL-2B", "2017-013A", "2026-11-22T22:46:15.830112", 14.30815042,
              0.00011338, 98.5717, 39.4593, 91.8948, 268.2365, 2.761484e-05, 2.9e-07, 0.0],
    "62261": [62261, "SENTINEL-1C", "2024-235A", "2026-11-23T00:18:10.362816", 14.59199737,
              0.0001264, 98.1822, 332.0542, 89.1816, 270.9528, 3.886781e-05, 1.37e-06, 0.0],
    "39084": [39084, "LANDSAT 8", "2013-008A", "2026-11-22T22:14:26.071296", 14.57117134,
              0.00011877, 98.214, 35.0254, 91.5335, 268.5999, 5.2767326e-05, 1.92e-06, 0.0],
    "25544": [25544, "ISS (ZARYA)", "1998-067A", "2026-11-22T22:00:00.000000", 15.5, 0.0004,
              51.64, 10.0, 20.0, 30.0, 1e-4, 1e-5, 0.0],
}


def canned_forecast(now: datetime) -> dict:
    """Open-Meteo shaped payload: hourly from 3 days back to 4 days ahead."""
    t0 = (now - timedelta(days=3)).replace(minute=0, second=0, microsecond=0)
    times = [(t0 + timedelta(hours=i)).strftime("%Y-%m-%dT%H:00") for i in range(7 * 24)]
    n = len(times)
    return {"elevation": 120.0, "fetched_at": now.isoformat(timespec="seconds"), "grid": [47.75, 39.95],
            "current": {"time": now.strftime("%Y-%m-%dT%H:00"), "temperature_2m": 3.5,
                        "relative_humidity_2m": 71, "cloud_cover": 80, "precipitation": 0.0,
                        "weather_code": 3, "wind_speed_10m": 4.4, "wind_direction_10m": 190,
                        "wind_gusts_10m": 10.7, "visibility": 37500.0, "is_day": 1},
            "hourly": {"time": times, "cloud_cover": [i % 101 for i in range(n)],
                       "cloud_cover_low": [10] * n, "precipitation": [0.1] * n,
                       "wind_speed_10m": [5.0] * n, "wind_direction_10m": [270] * n,
                       "visibility": [20000.0] * n, "temperature_2m": [4.0] * n}}


class FakeWeather:
    def __init__(self):
        self.calls = 0

    def get(self, lat, lon):
        self.calls += 1
        return canned_forecast(datetime.now(UTC))


@pytest.fixture(autouse=True)
def _offline(monkeypatch, tmp_path_factory):
    from osint.infra import loader

    fake = FakeWeather()
    monkeypatch.setattr(context, "WEATHER", fake)
    # no repository data/reference in tests: an empty infrastructure directory by default
    empty = loader.Infra(tmp_path_factory.mktemp("infra-empty"))
    monkeypatch.setattr(loader, "get_infra", lambda: empty)
    context.PASS_CACHE._d.clear()
    context.PV_CACHE._d.clear()
    context._settlements.clear()
    context._country_names.clear()
    yield fake
