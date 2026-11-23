"""Line geometry helpers: Douglas-Peucker simplification and a uniform grid index of line
segments for cheap "is this point within X km of a cable/pipeline" queries."""

import itertools
import math
from collections.abc import Iterable

KM_PER_DEG = 111.195  # mean, good enough for proximity screening


def simplify(coords: list[list[float]], tol_deg: float) -> list[list[float]]:
    """Douglas-Peucker on [lon, lat] pairs (planar degrees; iterative, keeps both ends)."""
    n = len(coords)
    if n < 3 or tol_deg <= 0:
        return [list(c) for c in coords]
    keep = [False] * n
    keep[0] = keep[-1] = True
    stack = [(0, n - 1)]
    tol2 = tol_deg * tol_deg
    while stack:
        i, j = stack.pop()
        ax, ay = coords[i][0], coords[i][1]
        bx, by = coords[j][0], coords[j][1]
        dx, dy = bx - ax, by - ay
        ll = dx * dx + dy * dy
        best, idx = -1.0, -1
        for k in range(i + 1, j):
            px, py = coords[k][0], coords[k][1]
            if ll == 0:
                d = (px - ax) ** 2 + (py - ay) ** 2
            else:
                t = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / ll))
                d = (px - ax - t * dx) ** 2 + (py - ay - t * dy) ** 2
            if d > best:
                best, idx = d, k
        if idx >= 0 and best > tol2:
            keep[idx] = True
            stack.append((i, idx))
            stack.append((idx, j))
    return [list(coords[k]) for k in range(n) if keep[k]]


def seg_dist_km(lat: float, lon: float, a: tuple[float, float], b: tuple[float, float]) -> float:
    """Distance from (lat, lon) to segment a-b ([lon, lat] each), local equirectangular."""
    kx = KM_PER_DEG * math.cos(math.radians(lat))
    ax, ay = (a[0] - lon) * kx, (a[1] - lat) * KM_PER_DEG
    bx, by = (b[0] - lon) * kx, (b[1] - lat) * KM_PER_DEG
    dx, dy = bx - ax, by - ay
    ll = dx * dx + dy * dy
    t = 0.0 if ll == 0 else max(0.0, min(1.0, -(ax * dx + ay * dy) / ll))
    return math.hypot(ax + t * dx, ay + t * dy)


class SegmentGrid:
    """Line segments bucketed into ``cell_deg`` cells (each segment goes into every cell its
    bounding box, grown by ``buffer_km``, touches). ``near()`` looks at one cell only."""

    def __init__(self, cell_deg: float = 0.25, buffer_km: float = 5.0) -> None:
        self.cell = cell_deg
        self.buffer_km = buffer_km
        self.cells: dict[tuple[int, int], list[int]] = {}
        self.segs: list[tuple[tuple[float, float], tuple[float, float], int]] = []
        self.owners: list[dict] = []

    def _key(self, lat: float, lon: float) -> tuple[int, int]:
        return math.floor(lat / self.cell), math.floor(lon / self.cell)

    def add_line(self, coords: Iterable, owner: dict) -> None:
        oi = len(self.owners)
        self.owners.append(owner)
        pts = [(float(c[0]), float(c[1])) for c in coords]
        for a, b in itertools.pairwise(pts):
            if abs(a[0] - b[0]) > 180:  # antimeridian hop: not a real segment
                continue
            si = len(self.segs)
            self.segs.append((a, b, oi))
            mid_lat = (a[1] + b[1]) / 2
            blat = self.buffer_km / KM_PER_DEG
            blon = self.buffer_km / (KM_PER_DEG * max(0.05, math.cos(math.radians(mid_lat))))
            r0, c0 = self._key(min(a[1], b[1]) - blat, min(a[0], b[0]) - blon)
            r1, c1 = self._key(max(a[1], b[1]) + blat, max(a[0], b[0]) + blon)
            if (r1 - r0 + 1) * (c1 - c0 + 1) > 4000:  # degenerate giant segment: skip
                continue
            for r in range(r0, r1 + 1):
                for c in range(c0, c1 + 1):
                    self.cells.setdefault((r, c), []).append(si)

    def near(self, lat: float, lon: float, km: float | None = None) -> tuple[dict, float] | None:
        """Closest indexed line within ``km`` (default and max: buffer_km) -> (owner, km)."""
        lim = min(km if km is not None else self.buffer_km, self.buffer_km)
        best: tuple[dict, float] | None = None
        for si in self.cells.get(self._key(lat, lon), ()):
            a, b, oi = self.segs[si]
            d = seg_dist_km(lat, lon, a, b)
            if d <= lim and (best is None or d < best[1]):
                best = (self.owners[oi], d)
        return best

    def __len__(self) -> int:
        return len(self.segs)


class PointGrid:
    """Points bucketed by ``cell_deg``; ``near()`` checks the 3x3 neighbourhood."""

    def __init__(self, cell_deg: float = 0.5) -> None:
        self.cell = cell_deg
        self.cells: dict[tuple[int, int], list[tuple[float, float, dict]]] = {}

    def add(self, lat: float, lon: float, item: dict) -> None:
        k = (math.floor(lat / self.cell), math.floor(lon / self.cell))
        self.cells.setdefault(k, []).append((lat, lon, item))

    def near(self, lat: float, lon: float, km: float) -> tuple[dict, float] | None:
        r, c = math.floor(lat / self.cell), math.floor(lon / self.cell)
        best = None
        kx = KM_PER_DEG * math.cos(math.radians(lat))
        for dr in (-1, 0, 1):
            for dc in (-1, 0, 1):
                for plat, plon, it in self.cells.get((r + dr, c + dc), ()):
                    d = math.hypot((plon - lon) * kx, (plat - lat) * KM_PER_DEG)
                    if d <= km and (best is None or d < best[1]):
                        best = (it, d)
        return best
