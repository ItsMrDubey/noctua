"""Military movements from the positions lake + track metadata (blocking; call off-loop)."""

import threading
from collections import defaultdict
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

import h3

from osint.core.geo import utcnow
from osint.movements import analysis
from osint.movements.airports import AirportIndex
from osint.movements.config import MovementsConfig
from osint.movements.legs import Fix, downsample, segment
from osint.movements.roles import track_role

US_PER_H = 3_600_000_000
_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
LOOKBACK_H = 12  # extra history read before a legs window so early legs get their origin

_AIR_SQL = ("SELECT track_id, epoch_us(ts), lat, lon, alt_ft, speed_kts FROM positions "
            "WHERE kind = 'air' AND ts >= ? AND ts <= ? ORDER BY track_id, ts")
_SURGE_SQL = ("SELECT DISTINCT track_id, h3_3, epoch_us(ts) // ? FROM positions "
              "WHERE kind = 'air' AND ts >= ? AND ts <= ?")
_FIRST_SQL = "SELECT epoch_us(min(ts)) FROM positions WHERE kind = 'air' AND ts >= ? AND ts <= ?"
_SEA_IDS_SQL = "SELECT DISTINCT track_id FROM positions WHERE kind = 'sea' AND ts >= ? AND ts <= ?"


def _us(dt: datetime) -> int:
    return (dt - _EPOCH) // timedelta(microseconds=1)


class MovementsService:
    def __init__(self, lake, store, airports: Callable[[], list[dict]], cfg: MovementsConfig | None = None,
                 clock: Callable[[], datetime] = utcnow) -> None:
        self.lake, self.store, self._airports = lake, store, airports
        self.cfg = cfg or MovementsConfig()
        self.clock = clock
        self._index: AirportIndex | None = None
        self._lock = threading.Lock()
        self._naval_rx = analysis.naval_regex(self.cfg.naval.name_prefixes)
        self._parents: dict[int, str] = {}

    @property
    def index(self) -> AirportIndex:
        with self._lock:
            if self._index is None:
                self._index = AirportIndex(self._airports())
            return self._index

    def _meta(self, ids: list[str]) -> dict[str, dict]:
        return self.store.get_tracks(ids) if ids else {}

    # ------------------------------------------------------------------ legs

    def legs(self, hours: int, role: str | None = None) -> list[dict]:
        now = self.clock()
        since = now - timedelta(hours=hours)
        rows = self.lake.query(_AIR_SQL, [since - timedelta(hours=LOOKBACK_H), now],
                               start=since - timedelta(hours=LOOKBACK_H), end=now)
        by_track: dict[str, list[Fix]] = defaultdict(list)
        for tid, ts_us, lat, lon, alt, spd in rows:
            by_track[tid].append(Fix(ts_us, lat, lon, alt, spd))
        meta = self._meta(list(by_track))
        since_us, now_us = _us(since), _us(now)
        index = self.index
        out = []
        for tid, fixes in by_track.items():
            m = meta.get(tid)
            if not m or not m.get("is_mil"):
                continue
            r = track_role(m)
            if role and r != role:
                continue
            for lg in segment(tid, fixes, index, self.cfg.legs, now_us, m, r):
                if lg["t_end_us"] >= since_us:
                    out.append(lg)
        out.sort(key=lambda x: x["t_end_us"], reverse=True)
        return out

    def corridors(self, hours: int, role: str | None = None) -> list[dict]:
        return analysis.corridors(self.legs(hours, role), self.cfg.corridors, role)

    # ---------------------------------------------------------------- surges

    def _parent(self, c3: int) -> str:
        p = self._parents.get(c3)
        if p is None:
            p = h3.cell_to_parent(h3.int_to_str(c3), self.cfg.surges.h3_res)
            if len(self._parents) < 200_000:
                self._parents[c3] = p
        return p

    def surges(self, hours: int) -> dict:
        s = self.cfg.surges
        now = self.clock()
        bucket_us = s.bucket_hours * US_PER_H
        start = now - timedelta(hours=hours) - timedelta(days=s.baseline_days, hours=s.bucket_hours)
        rows = self.lake.query(_SURGE_SQL, [bucket_us, start, now], start=start, end=now)
        meta = self._meta(sorted({r[0] for r in rows}))
        counts: dict[tuple[str, int], set[str]] = defaultdict(set)
        for tid, c3, b in rows:
            m = meta.get(tid)
            if c3 is None or not m or not m.get("is_mil"):
                continue
            counts[(self._parent(c3), int(b))].add(tid)
        first = self.lake.query(_FIRST_SQL, [start, now], start=start, end=now)
        history_start = first[0][0] if first and first[0][0] is not None else None
        names = {t: (m.get("name") or t) for t, m in meta.items()}
        return analysis.surges(counts, _us(now), hours, s, history_start, names)

    # ----------------------------------------------------------------- naval

    def naval(self, hours: int) -> list[dict]:
        now = self.clock()
        since = now - timedelta(hours=hours)
        ids = [r[0] for r in self.lake.query(_SEA_IDS_SQL, [since, now], start=since, end=now)]
        meta = self._meta(ids)
        keep = [t for t in ids if analysis.is_naval(meta.get(t), self._naval_rx)]
        if not keep:
            return []
        marks = ",".join("?" * len(keep))
        rows = self.lake.query(
            "SELECT track_id, epoch_us(ts), lat, lon, speed_kts, heading_deg FROM positions "
            f"WHERE kind = 'sea' AND ts >= ? AND ts <= ? AND track_id IN ({marks}) ORDER BY track_id, ts",
            [since, now, *keep], start=since, end=now)
        by: dict[str, list[tuple]] = defaultdict(list)
        for r in rows:
            by[r[0]].append(r)
        out = []
        for tid, pts in by.items():
            m = meta.get(tid) or {}
            last = pts[-1]
            fixes = [Fix(p[1], p[2], p[3], None, p[4]) for p in pts]
            out.append({
                "id": tid, "name": m.get("name"), "type_code": m.get("type_code"),
                "operator": m.get("operator"), "is_mil": bool(m.get("is_mil")),
                "match": "ais_type" if m.get("is_mil") else "name_prefix",
                "lat": round(last[2], 5), "lon": round(last[3], 5), "speed_kts": last[4],
                "heading_deg": last[5], "ts": (_EPOCH + timedelta(microseconds=last[1])).isoformat(),
                "track": downsample(fixes, self.cfg.naval.max_track_points),
            })
        out.sort(key=lambda x: x["ts"], reverse=True)
        return out

