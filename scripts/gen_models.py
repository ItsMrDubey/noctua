"""Procedural low-poly glTF 2.0 (.glb) models for the NOCTUA globe (stdlib only).

Every mesh here is original geometry built from primitives coded below, so the
generated .glb files carry no third-party licensing.

Axis convention
---------------
Models are authored in a vehicle frame: +X forward (nose/bow), +Y left, +Z up,
metres, origin at the vehicle's centre (ships: waterline). Vertices are written
to glTF in the standard glTF orientation: glTF +Z = forward, +Y = up, +X = left
(vehicle (x, y, z) -> glTF (y, z, x); a cyclic permutation, so winding and
handedness are preserved).

CesiumJS (Model, default upAxis=Y, forwardAxis=Z) maps glTF (x, y, z) to its
local frame as (z, x, y): the nose ends up on local +X, up on local +Z. With
Transforms.headingPitchRollToFixedFrame, heading 0 points local +X to *east*, so
models3d.js uses HEADING_OFFSET = -PI/2: hpr.heading = toRadians(heading_deg) - PI/2
makes compass heading 0 face north and 90 face east.

Usage: python scripts/gen_models.py [outdir]   (default frontend/models)
"""
from __future__ import annotations

import json
import math
import struct
import sys
from pathlib import Path

Vec = tuple[float, float, float]

# ---------------------------------------------------------------- geometry

class Part:
    """Triangle soup for one material; triangles stored as vertex triples."""

    def __init__(self) -> None:
        self.tris: list[tuple[Vec, Vec, Vec]] = []

    def add_convex(self, faces: list[list[Vec]]) -> None:
        """Add the polygonal faces of a convex solid, oriented outward."""
        pts = [p for f in faces for p in f]
        c = tuple(sum(p[i] for p in pts) / len(pts) for i in range(3))
        for f in faces:
            fc = tuple(sum(p[i] for p in f) / len(f) for i in range(3))
            for i in range(1, len(f) - 1):
                a, b, d = f[0], f[i], f[i + 1]
                n = _cross(_sub(b, a), _sub(d, a))
                if _dot(n, n) < 1e-12:
                    continue  # degenerate
                if _dot(n, _sub(fc, c)) < 0:
                    b, d = d, b
                self.tris.append((a, b, d))


def _sub(a: Vec, b: Vec) -> Vec:
    return (a[0] - b[0], a[1] - b[1], a[2] - b[2])


def _cross(a: Vec, b: Vec) -> Vec:
    return (a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0])


def _dot(a: Vec, b: Vec) -> float:
    return a[0] * b[0] + a[1] * b[1] + a[2] * b[2]


def loft(part: Part, rings: list[list[Vec]], cap: bool = True) -> None:
    """Chain of convex segments between consecutive rings (equal vertex counts)."""
    for k in range(len(rings) - 1):
        r0, r1 = rings[k], rings[k + 1]
        n = len(r0)
        faces = [[r0[i], r0[(i + 1) % n], r1[(i + 1) % n], r1[i]] for i in range(n)]
        if cap or k == 0:
            faces.append(list(r0))
        if cap or k == len(rings) - 2:
            faces.append(list(r1))
        part.add_convex(faces)


def box(part: Part, x0: float, x1: float, y0: float, y1: float, z0: float, z1: float) -> None:
    loft(part, [[(x0, y0, z), (x1, y0, z), (x1, y1, z), (x0, y1, z)] for z in (z0, z1)])


def extrude(part: Part, poly: list[tuple[float, float]], axis: str, a0: float, a1: float) -> None:
    """Prism of a convex 2D polygon. axis 'z': poly in (x,y); 'y': poly in (x,z); 'x': poly in (y,z)."""
    def p3(u: float, v: float, w: float) -> Vec:
        if axis == "z":
            return (u, v, w)
        if axis == "y":
            return (u, w, v)
        return (w, u, v)
    loft(part, [[p3(u, v, a) for u, v in poly] for a in (a0, a1)])


def ellipse(cx: float, ry: float, rz: float, n: int = 8, zc: float = 0.0, yc: float = 0.0) -> list[Vec]:
    """Ring perpendicular to X."""
    return [(cx, yc + ry * math.cos(2 * math.pi * i / n), zc + rz * math.sin(2 * math.pi * i / n)) for i in range(n)]


def fuselage(part: Part, stations: list[tuple[float, float, float, float]], n: int = 8) -> None:
    """stations: (x, ry, rz, zc) from tail to nose."""
    loft(part, [ellipse(x, max(ry, 0.02), max(rz, 0.02), n, zc) for x, ry, rz, zc in stations], cap=False)


def cylinder(part: Part, cx: float, cy: float, z0: float, z1: float, r: float, n: int = 12) -> None:
    ring = [(cx + r * math.cos(2 * math.pi * i / n), cy + r * math.sin(2 * math.pi * i / n)) for i in range(n)]
    loft(part, [[(x, y, z) for x, y in ring] for z in (z0, z1)])


def shift(part: Part, start: int, dy: float = 0.0, dz: float = 0.0, dx: float = 0.0) -> None:
    part.tris[start:] = [tuple((p[0] + dx, p[1] + dy, p[2] + dz) for p in t) for t in part.tris[start:]]


def mirror_y(part: Part, start: int) -> None:
    """Append a Y-mirrored copy of tris[start:] (winding flipped to stay outward)."""
    part.tris.extend(tuple((p[0], -p[1], p[2]) for p in (t[0], t[2], t[1])) for t in part.tris[start:])


def wing(part: Part, root_x: tuple[float, float], tip_x: tuple[float, float], y0: float, span: float,
         z: float, t_root: float, t_tip: float, dihedral: float = 0.0, both: bool = True) -> None:
    """Tapered/swept wing panel from y0 to y0+span; root_x/tip_x = (trailing, leading) x."""
    s = len(part.tris)
    zt = z + span * math.tan(math.radians(dihedral))
    root = [(root_x[0], y0, z - t_root / 2), (root_x[1], y0, z - t_root / 4), (root_x[1], y0, z + t_root / 4), (root_x[0], y0, z + t_root / 2)]
    tip = [(tip_x[0], y0 + span, zt - t_tip / 2), (tip_x[1], y0 + span, zt - t_tip / 4), (tip_x[1], y0 + span, zt + t_tip / 4), (tip_x[0], y0 + span, zt + t_tip / 2)]
    loft(part, [root, tip])
    if both:
        mirror_y(part, s)


def fin(part: Part, root_x: tuple[float, float], tip_x: tuple[float, float], z0: float, height: float,
        y: float, t: float, cant: float = 0.0) -> None:
    """Vertical (or canted) fin; root_x/tip_x = (trailing, leading)."""
    dy = height * math.sin(math.radians(cant))
    dz = height * math.cos(math.radians(cant))
    root = [(root_x[0], y - t / 2, z0), (root_x[1], y - t / 2, z0), (root_x[1], y + t / 2, z0), (root_x[0], y + t / 2, z0)]
    tip = [(tip_x[0], y + dy - t / 4, z0 + dz), (tip_x[1], y + dy - t / 4, z0 + dz), (tip_x[1], y + dy + t / 4, z0 + dz), (tip_x[0], y + dy + t / 4, z0 + dz)]
    loft(part, [root, tip])


def nacelle(part: Part, x0: float, x1: float, y: float, z: float, r: float, n: int = 8) -> None:
    s = len(part.tris)
    fuselage(part, [(x0, r * 0.7, r * 0.7, 0), (x0 + 0.2 * (x1 - x0), r, r, 0), (x1, r * 0.95, r * 0.95, 0)], n)
    loft(part, [ellipse(x1, r * 0.95, r * 0.95, n), ellipse(x1 + 0.001, r * 0.5, r * 0.5, n)])
    loft(part, [ellipse(x0 - 0.001, r * 0.4, r * 0.4, n), ellipse(x0, r * 0.7, r * 0.7, n)])
    shift(part, s, dy=y, dz=z)


def hull(part: Part, length: float, beam: float, freeboard: float, draft: float, bow: float = 0.18,
         stern_square: float = 0.85, n_side: int = 6) -> None:
    """Ship hull: deck-plan polygon (pointed bow, square-ish stern) lofted from keel to deck."""
    L2, B2 = length / 2, beam / 2
    bx = L2 - bow * length
    side = []  # port half-breadth from stern to bow tip (y>0); starboard mirrored
    for i in range(n_side + 1):
        side.append((-L2 + (bx + L2) * i / n_side, B2))
    for i in range(1, 7):  # parabolic bow: y = B2 (1 - t^2) keeps the deck plan convex
        t = i / 7
        side.append((bx + (L2 - bx) * t, B2 * (1 - t * t)))
    pts = [(-L2, -B2 * stern_square)] + [(x, -y) for x, y in side] + [(L2, 0.0)] + [(x, y) for x, y in reversed(side)] + [(-L2, B2 * stern_square)]
    # bow taper: points between bx and L2 handled by convex hull of segment; keep deck convex
    deck = [(x, y, freeboard) for x, y in pts]
    water = [(x * 0.99, y, 0.0) for x, y in pts]
    keel = [(x * 0.93, y * 0.55, -draft) for x, y in pts]
    loft(part, [keel, water, deck])


# ---------------------------------------------------------------- glb writer

def _pad(b: bytes, fill: bytes) -> bytes:
    return b + fill * ((4 - len(b) % 4) % 4)


def build_glb(name: str, parts: list[tuple[Part, tuple[float, float, float], float, float]]) -> tuple[bytes, int]:
    """parts: (Part, rgb, metallic, roughness). Returns (glb bytes, triangle count).

    One mesh, one primitive per material; non-indexed positions/normals are
    expanded per face (flat shading) and indexed with a simple 0..n-1 list.
    """
    gltf: dict = {
        "asset": {"version": "2.0", "generator": "noctua gen_models.py"},
        "scene": 0, "scenes": [{"nodes": [0]}],
        "nodes": [{"mesh": 0, "name": name}],
        "meshes": [{"name": name, "primitives": []}],
        "materials": [], "accessors": [], "bufferViews": [], "buffers": [],
    }
    blob = b""
    total = 0

    def view(data: bytes, target: int) -> int:
        nonlocal blob
        blob = _pad(blob, b"\x00")
        gltf["bufferViews"].append({"buffer": 0, "byteOffset": len(blob), "byteLength": len(data), "target": target})
        blob += data
        return len(gltf["bufferViews"]) - 1

    for part, rgb, metal, rough in parts:
        if not part.tris:
            continue
        pos: list[float] = []
        nrm: list[float] = []
        for a, b, c in part.tris:
            n = _cross(_sub(b, a), _sub(c, a))
            ln = math.sqrt(_dot(n, n)) or 1.0
            ng = (n[1] / ln, n[2] / ln, n[0] / ln)
            for p in (a, b, c):
                pos += [p[1], p[2], p[0]]  # vehicle (x,y,z) -> glTF (y,z,x)
                nrm += ng
        nv = len(pos) // 3
        idx = list(range(nv))
        big = nv > 65535
        ib = struct.pack("<%d%s" % (nv, "I" if big else "H"), *idx)
        pv = view(struct.pack("<%df" % len(pos), *pos), 34962)
        # min/max must match the float32 values actually stored
        f32 = [struct.unpack("<f", struct.pack("<f", v))[0] for v in pos]
        mn = [min(f32[i::3]) for i in range(3)]
        mx = [max(f32[i::3]) for i in range(3)]
        acc = gltf["accessors"]
        acc.append({"bufferView": pv, "componentType": 5126, "count": nv, "type": "VEC3", "min": mn, "max": mx})
        nv_ = view(struct.pack("<%df" % len(nrm), *nrm), 34962)
        acc.append({"bufferView": nv_, "componentType": 5126, "count": nv, "type": "VEC3"})
        iv = view(ib, 34963)
        acc.append({"bufferView": iv, "componentType": 5125 if big else 5123, "count": nv, "type": "SCALAR", "min": [0], "max": [nv - 1]})
        gltf["materials"].append({
            "name": "m%d" % len(gltf["materials"]),
            "pbrMetallicRoughness": {"baseColorFactor": [rgb[0], rgb[1], rgb[2], 1.0], "metallicFactor": metal, "roughnessFactor": rough},
            "doubleSided": True,
        })
        gltf["meshes"][0]["primitives"].append({
            "attributes": {"POSITION": len(acc) - 3, "NORMAL": len(acc) - 2}, "indices": len(acc) - 1,
            "material": len(gltf["materials"]) - 1, "mode": 4,
        })
        total += len(part.tris)
    blob = _pad(blob, b"\x00")
    gltf["buffers"].append({"byteLength": len(blob)})
    js = _pad(json.dumps(gltf, separators=(",", ":")).encode(), b" ")
    length = 12 + 8 + len(js) + 8 + len(blob)
    out = struct.pack("<4sII", b"glTF", 2, length)
    out += struct.pack("<I4s", len(js), b"JSON") + js
    out += struct.pack("<I4s", len(blob), b"BIN\x00") + blob
    return out, total


# ---------------------------------------------------------------- palette

MIL = (0.46, 0.49, 0.52)
MIL_DARK = (0.30, 0.32, 0.35)
CIV = (0.86, 0.87, 0.89)
CIV_ACC = (0.52, 0.56, 0.62)
GLASS = (0.10, 0.12, 0.15)
HULL = (0.36, 0.38, 0.41)
DECK = (0.22, 0.23, 0.25)
SUPER = (0.78, 0.79, 0.80)
RED_HULL = (0.40, 0.18, 0.16)
CONT = [(0.55, 0.30, 0.24), (0.32, 0.42, 0.52), (0.56, 0.52, 0.42)]


# ---------------------------------------------------------------- aircraft

def airliner_body(b: Part, a: Part, L: float, R: float, span: float, engines: int, t_tail: bool = False,
                  high_wing: bool = False, sweep: float = 30.0) -> None:
    """Shared tube-and-wing builder. Origin at mid-fuselage."""
    h = L / 2
    fuselage(b, [(-h, 0.15 * R, 0.25 * R, 0.55 * R), (-h + 0.18 * L, 0.85 * R, 0.9 * R, 0.2 * R),
                 (-h + 0.3 * L, R, R, 0), (h - 0.2 * L, R, R, 0), (h - 0.09 * L, 0.88 * R, 0.85 * R, -0.05 * R),
                 (h - 0.025 * L, 0.5 * R, 0.45 * R, -0.12 * R), (h, 0.08 * R, 0.08 * R, -0.15 * R)], n=10)
    rc = 0.22 * L  # root chord
    xw = 0.06 * L if not high_wing else 0.05 * L
    zw = -0.55 * R if not high_wing else 0.8 * R
    semispan = span / 2 - R * 0.6
    tsw = semispan * math.tan(math.radians(sweep))
    wing(b, (xw - rc / 2, xw + rc / 2), (xw - rc / 2 - tsw + 0.65 * rc, xw + rc / 2 - tsw), R * 0.6, semispan,
         zw, 0.12 * rc, 0.05 * rc, dihedral=5 if not high_wing else -2)
    # engines under the wing
    er = 0.38 * R if engines == 2 else 0.3 * R
    stations = [0.35] if engines == 2 else [0.28, 0.55]
    for f in stations:
        y = R * 0.6 + f * semispan
        xl = xw + rc / 2 - f * tsw
        el = 2.6 * er * 2
        for s in (1, -1):
            nacelle(a, xl - el * 0.75, xl + el * 0.3, s * y, zw - er * 1.25 + (y * math.tan(math.radians(5 if not high_wing else -2))), er)
    # tail
    vh = 0.19 * L if not t_tail else 0.17 * L
    vc = 0.14 * L
    fin(b, (-h + 0.005 * L, -h + vc), (-h + 0.05 * L - 0.02 * L, -h + 0.07 * L + vc * 0.35), 0.45 * R, vh, 0, 0.12 * vc)
    hs = 0.17 * span
    hc = 0.1 * L
    if t_tail:
        zh, xh = 0.45 * R + vh, -h + 0.06 * L
    else:
        zh, xh = 0.35 * R, -h + 0.06 * L
    wing(b, (xh - hc / 2, xh + hc / 2), (xh - hc / 2 - 0.25 * hs + 0.5 * hc, xh + hc / 2 - 0.25 * hs), 0.0 if t_tail else 0.3 * R,
         hs, zh, 0.1 * hc, 0.05 * hc, dihedral=4 if not t_tail else -3)
    # cockpit glazing strip
    box(a, h - 0.07 * L, h - 0.045 * L, -0.45 * R, 0.45 * R, 0.15 * R, 0.45 * R)


def m_airliner() -> list:
    b, a = Part(), Part()
    airliner_body(b, a, 44.5, 2.0, 41.0, engines=2)
    return [(b, CIV, 0.1, 0.6), (a, CIV_ACC, 0.2, 0.5)]


def m_tanker() -> list:
    b, a = Part(), Part()
    airliner_body(b, a, 47.0, 2.1, 44.0, engines=4, sweep=33)
    # refuelling boom under the tail, angled down
    h = 47.0 / 2
    s = len(a.tris)
    fuselage(a, [(0.0, 0.25, 0.25, 0), (9.0, 0.35, 0.35, 0)], n=6)
    a.tris[s:] = [tuple((-h + 4 - p[0] * math.cos(0.5) , p[1], -1.2 - p[0] * math.sin(0.5) + p[2]) for p in t) for t in a.tris[s:]]
    wing(a, (-h - 0.5, -h + 0.6), (-h - 0.3, -h + 0.4), 0.2, 1.6, -3.0, 0.2, 0.1)
    return [(b, MIL, 0.1, 0.7), (a, MIL_DARK, 0.2, 0.6)]


def m_awacs() -> list:
    b, a = Part(), Part()
    airliner_body(b, a, 46.6, 2.0, 44.4, engines=4, sweep=35)
    # rotodome on two struts
    zc = 2.0 + 3.2
    box(a, -7.0, -5.5, -0.25, 0.25, 1.5, zc - 0.5)
    box(a, -11.5, -10.0, -0.25, 0.25, 1.4, zc - 0.5)
    cylinder(a, -8.5, 0.0, zc - 0.9, zc + 0.9, 4.6, n=20)
    return [(b, MIL, 0.1, 0.7), (a, MIL_DARK, 0.2, 0.6)]


def m_transport() -> list:
    b, a = Part(), Part()
    airliner_body(b, a, 53.0, 3.4, 51.8, engines=4, t_tail=True, high_wing=True, sweep=25)
    # landing-gear sponsons
    box(b, -6.0, 6.0, -4.1, 4.1, -3.2, -1.6)
    return [(b, MIL, 0.1, 0.75), (a, MIL_DARK, 0.2, 0.6)]


def m_fighter() -> list:
    b, a = Part(), Part()
    L = 19.0
    h = L / 2
    fuselage(b, [(-h, 0.9, 0.7, 0.1), (-h + 3, 1.4, 0.95, 0.1), (0, 1.35, 1.0, 0.15), (h - 4.5, 0.75, 0.75, 0.1),
                 (h - 1.5, 0.35, 0.35, 0.0), (h, 0.05, 0.05, 0.0)], n=8)
    # intakes
    box(b, -2.0, 2.5, 1.0, 1.9, -0.7, 0.4)
    box(b, -2.0, 2.5, -1.9, -1.0, -0.7, 0.4)
    # swept/cropped-delta main wing
    wing(b, (-h + 2.0, 1.5), (-h + 3.4, -h + 5.2), 1.3, 5.6, 0.0, 0.35, 0.1)
    # leading-edge extensions
    wing(b, (-1.0, 4.5), (0.5, 1.6), 1.0, 0.7, 0.05, 0.25, 0.1)
    # stabilators
    wing(b, (-h - 0.6, -h + 2.2), (-h - 0.4, -h + 0.6), 1.0, 2.9, 0.0, 0.2, 0.08)
    # twin canted tails
    for y, c in ((1.3, 18), (-1.3, -18)):
        fin(b, (-h + 0.2, -h + 4.0), (-h + 0.6, -h + 1.8), 0.8, 3.2, y, 0.18, cant=c)
    # canopy
    loft(a, [ellipse(h - 6.5, 0.15, 0.1, 6, 0.7), ellipse(h - 5.0, 0.55, 0.45, 6, 0.85), ellipse(h - 3.2, 0.35, 0.25, 6, 0.6)], cap=False)
    # nozzles
    for y in (0.55, -0.55):
        s = len(a.tris)
        loft(a, [ellipse(-h - 0.6, 0.45, 0.45, 8), ellipse(-h + 0.3, 0.55, 0.55, 8)])
        shift(a, s, dy=y, dz=0.05)
    return [(b, MIL, 0.15, 0.7), (a, GLASS, 0.3, 0.3)]


def m_helo() -> list:
    b, a = Part(), Part()
    # cabin, boom, rotor disc, tail rotor; ~19.8 m rotor-tip to tail (UH-60 class)
    fuselage(b, [(-3.2, 0.9, 0.9, 0.2), (-1.5, 1.2, 1.3, 0.1), (2.0, 1.15, 1.25, 0.0), (3.6, 0.9, 0.9, -0.2), (4.6, 0.3, 0.4, -0.4)], n=10)
    fuselage(b, [(-9.6, 0.15, 0.25, 1.0), (-3.0, 0.45, 0.5, 0.6)], n=8)
    fin(b, (-10.2, -8.8), (-10.4, -9.6), 0.8, 2.2, 0.0, 0.18)
    wing(b, (-9.4, -8.4), (-9.3, -8.5), 0.0, 1.9, 0.9, 0.12, 0.08)
    box(b, -1.0, 1.4, -0.7, 0.7, 1.3, 2.0)  # engine deck
    cylinder(a, 0.2, 0.0, 2.0, 2.45, 0.25, n=8)  # mast
    cylinder(a, 0.2, 0.0, 2.45, 2.55, 8.2, n=24)  # rotor disc
    s = len(a.tris)
    cylinder(a, 0.0, 0.0, -0.05, 0.05, 1.6, n=12)  # tail rotor disc in XZ plane
    a.tris[s:] = [tuple((p[0] - 10.0, 0.45 + p[2], 3.0 + p[1]) for p in t) for t in a.tris[s:]]
    box(b, -2.0, 2.2, 1.25, 1.45, -1.6, -1.45)  # skids/gear
    box(b, -2.0, 2.2, -1.45, -1.25, -1.6, -1.45)
    # rotor disc is a light translucent-looking dark grey
    return [(b, MIL, 0.1, 0.75), (a, MIL_DARK, 0.1, 0.8)]


def m_uav() -> list:
    b, a = Part(), Part()
    # MALE-class: 11 m fuselage, 20 m straight wing, V-tail, pusher; length_m reports 14 m incl. prop
    fuselage(b, [(-5.5, 0.25, 0.3, 0.1), (-3.5, 0.5, 0.55, 0.0), (3.0, 0.55, 0.6, 0.0), (4.6, 0.55, 0.65, 0.15), (5.5, 0.15, 0.2, 0.1)], n=8)
    wing(b, (-1.4, 0.4), (-0.9, 0.0), 0.4, 9.6, 0.25, 0.22, 0.08, dihedral=2)
    for c in (40, -40):
        fin(b, (-5.6, -4.4), (-5.9, -5.2), 0.2, 1.9, 0.0, 0.1, cant=c)
    fin(b, (-5.4, -4.6), (-5.5, -5.0), 0.0, -0.9, 0.0, 0.1)
    s = len(a.tris)
    cylinder(a, 0.0, 0.0, -0.04, 0.04, 1.4, n=10)  # prop disc in YZ plane
    a.tris[s:] = [tuple((-5.9 + p[2], p[0], p[1] + 0.1) for p in t) for t in a.tris[s:]]
    loft(a, [ellipse(3.6, 0.2, 0.15, 6, -0.55), ellipse(4.4, 0.35, 0.3, 6, -0.6), ellipse(5.0, 0.2, 0.15, 6, -0.5)], cap=False)  # sensor ball
    return [(b, (0.62, 0.64, 0.66), 0.1, 0.7), (a, MIL_DARK, 0.2, 0.6)]


def m_generic() -> list:
    b, a = Part(), Part()
    # business-jet class: 20 m, aft-mounted engines, T-tail
    L, R = 20.0, 1.0
    h = L / 2
    fuselage(b, [(-h, 0.1, 0.2, 0.45), (-h + 4, 0.8, 0.85, 0.15), (-h + 6, R, R, 0), (h - 4.5, R, R, 0), (h - 1.8, 0.7, 0.6, -0.1), (h, 0.06, 0.06, -0.2)], n=10)
    wing(b, (-1.8, 1.6), (-3.6, -2.0), 0.7, 8.0, -0.5, 0.35, 0.12, dihedral=4)
    fin(b, (-h + 0.2, -h + 3.4), (-h + 1.2, -h + 2.6), 0.5, 3.2, 0.0, 0.2)
    wing(b, (-h + 0.9, -h + 2.4), (-h + 1.5, -h + 2.3), 0.0, 3.4, 3.7, 0.15, 0.08)
    for s in (1, -1):
        nacelle(a, -h + 3.2, -h + 6.2, s * 1.55, 0.55, 0.5)
    box(a, h - 2.4, h - 1.6, -0.5, 0.5, 0.1, 0.45)
    return [(b, CIV, 0.1, 0.6), (a, CIV_ACC, 0.2, 0.5)]


# ---------------------------------------------------------------- ships

def m_carrier() -> list:
    h, d, a = Part(), Part(), Part()
    L, B = 330.0, 40.0
    hull(h, 317.0, B, 17.0, 11.0, bow=0.12)
    # flight deck: wide flared slab with angled-deck sponson (port side)
    extrude(d, [(-165, -20), (150, -20), (165, -8), (165, 12), (40, 38), (-120, 38), (-165, 18)], "z", 17.0, 19.5)
    # island on starboard, aft of midships
    box(a, -50, -20, -18.5, -11.5, 19.5, 34.0)
    box(a, -44, -28, -17.5, -12.5, 34.0, 42.0)
    cylinder(a, -36, -15, 42.0, 52.0, 1.2, n=8)
    box(a, -42, -30, -15.6, -14.4, 46.0, 47.0)
    # deck markings: centre line strip slightly raised
    box(a, -150, 120, -1.2, 1.2, 19.5, 19.56)
    s = len(a.tris)
    box(a, -110, 80, -0.8, 0.8, 19.5, 19.57)
    a.tris[s:] = [tuple((p[0] * math.cos(0.16) - p[1] * math.sin(0.16) - 10, p[0] * math.sin(0.16) + p[1] * math.cos(0.16) + 14, p[2]) for p in t) for t in a.tris[s:]]
    return [(h, HULL, 0.2, 0.8), (d, DECK, 0.1, 0.9), (a, SUPER, 0.1, 0.7)]


def m_destroyer() -> list:
    h, s_ = Part(), Part()
    L, B = 155.0, 20.0
    hull(h, L, B, 9.0, 6.5, bow=0.22)
    # superstructure blocks (tapered), mast, funnel, gun
    loft(s_, [[(x, y, z) for x, y in ((-10, -8), (32, -8), (32, 8), (-10, 8))] for z in (9.0,)] +
         [[(x, y, z) for x, y in ((-8, -6.5), (28, -6.5), (28, 6.5), (-8, 6.5))] for z in (17.0,)])
    loft(s_, [[(x, y, 17.0) for x, y in ((14, -5), (28, -5), (28, 5), (14, 5))], [(x, y, 22.0) for x, y in ((15, -3.5), (25, -3.5), (25, 3.5), (15, 3.5))]])
    loft(s_, [[(x, y, 9.0) for x, y in ((-40, -6.5), (-14, -6.5), (-14, 6.5), (-40, 6.5))], [(x, y, 15.0) for x, y in ((-38, -5), (-15, -5), (-15, 5), (-38, 5))]])
    # mast: tapered lattice approximated as frustum + yard
    loft(s_, [[(x, y, 22.0) for x, y in ((16.5, -1.6), (19.5, -1.6), (19.5, 1.6), (16.5, 1.6))], [(x, y, 38.0) for x, y in ((17.6, -0.4), (18.4, -0.4), (18.4, 0.4), (17.6, 0.4))]])
    box(s_, 17.5, 18.5, -4.0, 4.0, 31.0, 31.6)
    box(s_, -6, 2, -2.2, 2.2, 17.0, 23.0)  # funnel
    cylinder(s_, 52.0, 0.0, 9.0, 10.8, 2.4, n=10)  # gun mount
    box(s_, 53.5, 61.0, -0.25, 0.25, 9.9, 10.3)  # barrel
    box(s_, -75, -55, -9.5, 9.5, 9.0, 9.2)  # helideck (aft)
    return [(h, HULL, 0.2, 0.8), (s_, SUPER, 0.1, 0.7)]


def m_cargo() -> list:
    h, s_, c0 = Part(), Part(), Part()
    c1, c2 = Part(), Part()
    L, B = 300.0, 48.0
    hull(h, L, B, 22.0, 14.0, bow=0.12, stern_square=0.9)
    # accommodation block aft + funnel
    box(s_, -138, -122, -20, 20, 22.0, 44.0)
    box(s_, -136, -126, -23, 23, 42.0, 44.0)  # bridge wings
    box(s_, -150, -140, -3, 3, 22.0, 40.0)
    # container stacks: bays of rows/tiers, colour-cycled
    parts = [c0, c1, c2]
    k = 0
    x = -118.0
    while x < 110:
        tiers = 4 + (k % 3 == 1)
        for j, y0 in enumerate((-21.0, -7.0, 7.0)):
            box(parts[(k + j) % 3], x, x + 12.2, y0, y0 + 13.6, 22.0, 22.0 + 2.6 * tiers)
        x += 13.0
        k += 1
    return [(h, RED_HULL, 0.2, 0.85), (s_, SUPER, 0.1, 0.7)] + [(p, CONT[i], 0.1, 0.85) for i, p in enumerate(parts)]


def m_tanker_ship() -> list:
    h, d, s_ = Part(), Part(), Part()
    L, B = 250.0, 44.0
    hull(h, L, B, 18.0, 15.0, bow=0.1, stern_square=0.92)
    # deck piping trunk + manifold
    box(d, -100, 110, -1.5, 1.5, 18.0, 19.5)
    box(d, -5, 5, -20, 20, 18.0, 19.0)
    for x in (-80, -40, 40, 80):
        box(d, x - 3, x + 3, -16, 16, 18.0, 18.6)
    # aft superstructure + funnel
    box(s_, -118, -100, -17, 17, 18.0, 38.0)
    box(s_, -112, -104, -21, 21, 36.0, 38.0)
    box(s_, -123, -116, -3, 3, 18.0, 41.0)
    box(s_, 112, 118, -10, 10, 18.0, 21.0)  # forecastle
    return [(h, RED_HULL, 0.2, 0.85), (d, DECK, 0.1, 0.9), (s_, SUPER, 0.1, 0.7)]


def m_passenger() -> list:
    h, s_ = Part(), Part()
    L, B = 200.0, 28.0
    hull(h, L, B, 12.0, 7.0, bow=0.2)
    for i, (x0, x1, w) in enumerate(((-90, 70, 12.5), (-85, 62, 12.0), (-80, 55, 11.0), (-60, 40, 9.0))):
        box(s_, x0, x1, -w, w, 12.0 + i * 5.0, 17.0 + i * 5.0)
    box(s_, -50, -36, -3.5, 3.5, 32.0, 38.0)
    return [(h, (0.88, 0.89, 0.90), 0.1, 0.6), (s_, (0.80, 0.82, 0.84), 0.1, 0.6)]


def m_small() -> list:
    h, s_ = Part(), Part()
    hull(h, 30.0, 7.5, 2.5, 2.2, bow=0.25)
    box(s_, -6, 4, -2.8, 2.8, 2.5, 5.8)
    box(s_, 0, 3.6, -2.4, 2.4, 5.8, 7.4)
    loft(s_, [[(x, y, 7.4) for x, y in ((1.2, -0.3), (1.8, -0.3), (1.8, 0.3), (1.2, 0.3))], [(x, y, 12.0) for x, y in ((1.4, -0.1), (1.6, -0.1), (1.6, 0.1), (1.4, 0.1))]])
    return [(h, HULL, 0.2, 0.8), (s_, SUPER, 0.1, 0.7)]


MODELS = {
    "aircraft_fighter": (m_fighter, 19.0),
    "aircraft_transport": (m_transport, 53.0),
    "aircraft_airliner": (m_airliner, 44.5),
    "aircraft_tanker": (m_tanker, 50.0),
    "aircraft_awacs": (m_awacs, 46.6),
    "aircraft_helo": (m_helo, 19.6),
    "aircraft_uav": (m_uav, 11.8),
    "aircraft_generic": (m_generic, 20.0),
    "ship_carrier": (m_carrier, 330.0),
    "ship_destroyer": (m_destroyer, 155.0),
    "ship_cargo": (m_cargo, 300.0),
    "ship_tanker": (m_tanker_ship, 250.0),
    "ship_passenger": (m_passenger, 200.0),
    "ship_small": (m_small, 30.0),
}


def generate(outdir: Path) -> dict:
    outdir.mkdir(parents=True, exist_ok=True)
    manifest = {
        "axis": "glTF Y-up, nose/bow along glTF +Z (vehicle +X fwd, +Y left, +Z up). In Cesium (forwardAxis=Z) "
                "the nose maps to local +X; use heading = toRadians(heading_deg) + HEADING_OFFSET (-PI/2).",
        "units": "metres, real-world scale; origin at vehicle centre (ships: waterline)",
        "models": {},
    }
    for name, (fn, length) in MODELS.items():
        data, tris = build_glb(name, fn())
        (outdir / (name + ".glb")).write_bytes(data)
        manifest["models"][name] = {"file": name + ".glb", "length_m": length, "triangles": tris, "bytes": len(data)}
    (outdir / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    return manifest


if __name__ == "__main__":
    root = Path(__file__).resolve().parents[1]
    out = Path(sys.argv[1]) if len(sys.argv) > 1 else root / "frontend" / "models"
    m = generate(out)
    for k, v in m["models"].items():
        print(f"{k:22s} {v['triangles']:5d} tris {v['bytes']:7d} B  {v['length_m']} m")
