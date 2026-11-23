"""Emergency squawks (7500 / 7600 / 7700 and ADS-B emergency status), one episode per flight.

An episode is keyed by (ICAO hex, code, callsign) and lasts while the aircraft keeps
reporting the code; after ``gap_s`` without it, a reappearance is a new episode. Episodes are
kept in the collector cursor so a restart does not re-announce an ongoing emergency."""

from __future__ import annotations

from typing import Any

SQUAWKS = {"7500": "Unlawful interference", "7600": "Radio failure", "7700": "General emergency"}
# readsb ``emergency`` values (ADS-B emergency/priority status) besides "none"
EMERGENCY_STATUS = {"general": "General emergency", "nordo": "Radio failure",
                    "unlawful": "Unlawful interference", "minfuel": "Minimum fuel",
                    "downed": "Downed aircraft", "lifeguard": "Lifeguard / medical"}
STATUS_CODE = {"general": "7700", "nordo": "7600", "unlawful": "7500"}
DEFAULT_IGNORE_STATUS = ("lifeguard",)  # priority status, not an emergency
SEVERITY = {"7500": "high", "7700": "high", "7600": "warn"}


def emergency_code(ac: dict, ignore: tuple[str, ...] = DEFAULT_IGNORE_STATUS) -> str | None:
    """'7500' / '7600' / '7700' / 'minfuel' / 'downed' ... for an aircraft in an emergency."""
    sq = str(ac.get("squawk") or "").strip()
    if sq in SQUAWKS:
        return sq
    status = str(ac.get("emergency") or "none").strip().lower()
    if status in ("", "none", "reserved") or status in ignore:
        return None
    return STATUS_CODE.get(status, status)


def code_label(code: str) -> str:
    return SQUAWKS.get(code) or EMERGENCY_STATUS.get(code) or code


class SquawkTracker:
    def __init__(self, gap_s: float = 1800.0, keep_s: float = 86400.0,
                 ignore_status: tuple[str, ...] = DEFAULT_IGNORE_STATUS) -> None:
        self.gap_s = gap_s
        self.keep_s = keep_s
        self.ignore = tuple(ignore_status)
        self.episodes: dict[str, dict[str, Any]] = {}  # episode id -> record

    @staticmethod
    def _flight_key(hex_code: str, code: str, flight: str) -> str:
        return f"{hex_code}:{code}:{flight}"

    def update(self, aircraft: list[dict], now_s: float, source: str = "") -> list[dict]:
        """Fold one poll's aircraft in; returns the episodes that started now."""
        latest: dict[str, dict] = {}
        for e in self.episodes.values():
            k = self._flight_key(e["hex"], e["code"], e["flight"])
            if k not in latest or e["last_seen"] > latest[k]["last_seen"]:
                latest[k] = e
        started = []
        for ac in aircraft:
            code = emergency_code(ac, self.ignore)
            hex_code = str(ac.get("hex") or "").strip().lower()
            if code is None or not hex_code or hex_code.startswith("~"):
                continue
            lat, lon = ac.get("lat"), ac.get("lon")
            age = ac.get("seen_pos", ac.get("seen", 0))
            seen = now_s - (float(age) if isinstance(age, (int, float)) else 0.0)
            flight = str(ac.get("flight") or "").strip()
            k = self._flight_key(hex_code, code, flight)
            ep = latest.get(k)
            if ep is None or seen - ep["last_seen"] > self.gap_s:
                ep = {"id": f"{hex_code}:{code}:{int(seen)}", "hex": hex_code, "code": code,
                      "label": code_label(code), "flight": flight, "first_seen": seen,
                      "source": source}
                self.episodes[ep["id"]] = ep
                latest[k] = ep
                started.append(ep)
            ep["last_seen"] = max(seen, ep.get("last_seen", seen))
            alt = ac.get("alt_baro")
            ep.update({
                "reg": ac.get("r") or ep.get("reg"), "type": ac.get("t") or ep.get("type"),
                "desc": ac.get("desc") or ep.get("desc"),
                "squawk": str(ac.get("squawk") or ""), "emergency": ac.get("emergency"),
                "alt_ft": 0 if alt == "ground" else alt if isinstance(alt, (int, float))
                else ep.get("alt_ft"),
                "gs": ac.get("gs", ep.get("gs")), "track": ac.get("track", ep.get("track")),
                "mil": bool((ac.get("dbFlags") or 0) & 1) or ep.get("mil", False),
            })
            if isinstance(lat, (int, float)) and isinstance(lon, (int, float)):
                ep["lat"], ep["lon"] = float(lat), float(lon)
                ep.setdefault("first_lat", float(lat))
                ep.setdefault("first_lon", float(lon))
        return started

    def prune(self, now_s: float) -> None:
        self.episodes = {k: e for k, e in self.episodes.items()
                         if now_s - e["last_seen"] <= self.keep_s}

    def items(self, now_s: float) -> list[dict]:
        """All kept episodes, newest first, with ``active`` (seen within ``gap_s``)."""
        out = [{**e, "active": now_s - e["last_seen"] <= min(self.gap_s, 300.0)}
               for e in self.episodes.values()]
        out.sort(key=lambda e: -e["last_seen"])
        return out

    def to_cursor(self) -> dict:
        return {"episodes": list(self.episodes.values())}

    def load_cursor(self, cursor: dict | None) -> None:
        for e in (cursor or {}).get("episodes") or []:
            if isinstance(e, dict) and e.get("id") and "last_seen" in e:
                self.episodes[e["id"]] = e
