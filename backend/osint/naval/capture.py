"""Which ADS-B aircraft the naval model wants that the military feed (``/v2/mil``) misses.

The ``adsb`` collector only sees aircraft flagged military in the aggregator database
(``dbFlags & 1``). Carrier-associated / maritime-patrol types and listed callsign patterns
that are NOT flagged are captured here from type, region and hex queries (``adsb_naval``) and
from the regional all-aircraft polls ``adsb_signals`` already makes (no extra requests).
Flagged aircraft are skipped: the ``adsb`` collector already records them every cycle."""

from pathlib import Path

from osint.naval.airfix import AircraftModel
from osint.naval.config import NavalConfig


class NavalCapture:
    def __init__(self, cfg: NavalConfig) -> None:
        self.model = AircraftModel(cfg)
        self.types = {t for t, m in self.model.mults.items() if m > 0} | self.model.support

    @classmethod
    def from_config_dir(cls, config_dir: Path) -> "NavalCapture":
        return cls(NavalConfig.load(Path(config_dir) / "naval.yaml"))

    def reason(self, ac: dict) -> str | None:
        """Why this aircraft record is captured (None = not wanted / already covered)."""
        try:
            if int(ac.get("dbFlags") or 0) & 1:
                return None
        except (TypeError, ValueError):
            pass
        t = str(ac.get("t") or "").upper().strip()
        cs = self.model.callsign_match(ac.get("flight"))
        if cs is not None:
            return f"callsign {str(ac.get('flight') or '').strip()} ({cs.get('label')})"
        if t in self.types:
            return f"type {t}"
        return None
