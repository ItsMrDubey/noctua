"""WCAG contrast + palette rules for the intel-console theme (frontend/css/theme.css).

Reads the first `:root { ... }` block of theme.css and checks:
  text      --fg / --fg-2 >= 4.5:1 and --dim >= 3:1 on --void, --deep, --raise;
            accent text colours (--acc --red --red-2 --green --ion --violet) >= 4.5:1 on the same;
  symbology every --cat-*, --st-*, --thr-*, --sym-* >= 3:1 on --void and on the darkened globe;
  distinct  categories pairwise CIE76 dE >= 20 and each dE >= 20 from --acc (no amber category);
  order     --st-* luminance strictly increasing reported -> official, saturation non-decreasing;
            --thr-0..4 hue non-increasing (sand -> amber-orange -> red), luminance of 0 lowest.

Usage: python scripts/check_contrast.py [path/to/theme.css] [--globe #rrggbb]
Exit code 0 when every check passes, 1 otherwise.
"""

from __future__ import annotations

import colorsys
import math
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_THEME = ROOT / "frontend" / "css" / "theme.css"
# a land mid-tone of the Esri base imagery after the globe tint (brightness .55, saturation .35,
# warm hue shift); ocean is darker, so text and glyphs that pass on this pass on the ocean too
GLOBE_REF = "#24221a"
STATUS_ORDER = ["reported", "multi-source", "sensor-detected", "sensor-corroborated", "official"]
BGS = ["--void", "--deep", "--raise"]


def parse_tokens(css: str) -> dict[str, str]:
    m = re.search(r":root\s*\{(.*?)\n\}", css, re.S)
    if not m:
        raise SystemExit("no :root block found")
    body = re.sub(r"/\*.*?\*/", "", m.group(1), flags=re.S)
    toks = {k: v.strip() for k, v in re.findall(r"(--[\w-]+)\s*:\s*([^;]+);", body)}
    for _ in range(5):  # resolve var(--x) indirection
        for k, v in toks.items():
            r = re.fullmatch(r"var\((--[\w-]+)\)", v)
            if r and r.group(1) in toks:
                toks[k] = toks[r.group(1)]
    return toks


def rgb(value: str) -> tuple[float, float, float]:
    v = value.strip().lower()
    if v.startswith("#"):
        h = v[1:]
        if len(h) in (3, 4):
            h = "".join(c * 2 for c in h)
        return tuple(int(h[i:i + 2], 16) / 255 for i in (0, 2, 4))  # alpha ignored (opaque checks)
    m = re.fullmatch(r"rgba?\(\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+).*\)", v)
    if m:
        return tuple(int(x) / 255 for x in m.groups())
    raise ValueError(f"not a colour: {value!r}")


def lum(c) -> float:
    lin = [x / 12.92 if x <= 0.04045 else ((x + 0.055) / 1.055) ** 2.4 for x in c]
    return 0.2126 * lin[0] + 0.7152 * lin[1] + 0.0722 * lin[2]


def ratio(a, b) -> float:
    la, lb = sorted((lum(a), lum(b)), reverse=True)
    return (la + 0.05) / (lb + 0.05)


def lab(c):
    lin = [x / 12.92 if x <= 0.04045 else ((x + 0.055) / 1.055) ** 2.4 for x in c]
    x = (0.4124 * lin[0] + 0.3576 * lin[1] + 0.1805 * lin[2]) / 0.95047
    y = 0.2126 * lin[0] + 0.7152 * lin[1] + 0.0722 * lin[2]
    z = (0.0193 * lin[0] + 0.1192 * lin[1] + 0.9505 * lin[2]) / 1.08883
    f = [t ** (1 / 3) if t > 0.008856 else 7.787 * t + 16 / 116 for t in (x, y, z)]
    return 116 * f[1] - 16, 500 * (f[0] - f[1]), 200 * (f[1] - f[2])


def delta_e(a, b) -> float:
    return math.dist(lab(a), lab(b))


def main(argv: list[str]) -> int:
    theme, globe = DEFAULT_THEME, GLOBE_REF
    args = list(argv)
    if "--globe" in args:
        i = args.index("--globe")
        globe = args[i + 1]
        del args[i:i + 2]
    if args:
        theme = Path(args[0])
    t = parse_tokens(theme.read_text(encoding="utf-8"))
    results: list[tuple[bool, str]] = []

    def check(ok: bool, msg: str) -> None:
        results.append((ok, msg))

    for name, need in [("--fg", 4.5), ("--fg-2", 4.5), ("--dim", 3.0), ("--acc", 4.5), ("--red", 4.5),
                       ("--red-2", 4.5), ("--green", 4.5), ("--ion", 4.5), ("--violet", 4.5)]:
        for bg in BGS:
            r = ratio(rgb(t[name]), rgb(t[bg]))
            check(r >= need, f"text  {name:<8} on {bg:<7} {r:5.2f} (>= {need})")

    sym = [k for k in t if re.match(r"--(cat|st|thr|sym)-", k)]
    for name in sym:
        for label, bg in (("--void", t["--void"]), ("globe", globe)):
            r = ratio(rgb(t[name]), rgb(bg))
            check(r >= 3.0, f"sym   {name:<24} on {label:<6} {r:5.2f} (>= 3.0)")

    cats = [k for k in t if k.startswith("--cat-")]
    for i, a in enumerate(cats):
        d = delta_e(rgb(t[a]), rgb(t["--acc"]))
        check(d >= 20, f"dist  {a:<24} vs --acc {d:5.1f} (>= 20)")
        for b in cats[i + 1:]:
            d = delta_e(rgb(t[a]), rgb(t[b]))
            check(d >= 20, f"dist  {a} vs {b} {d:5.1f} (>= 20)")

    for a, b in (("--red", "--thr-2"),):  # terror incident status: confirmed (--red) vs reported (--thr-2)
        d = delta_e(rgb(t[a]), rgb(t[b]))
        check(d >= 20, f"dist  terror confirmed {a} vs reported {b} {d:5.1f} (>= 20)")

    st = [rgb(t["--st-" + s]) for s in STATUS_ORDER]
    ls = [lum(c) for c in st]
    sats = [colorsys.rgb_to_hls(*c)[2] for c in st]
    check(all(a < b for a, b in zip(ls, ls[1:])), "order --st-* luminance increasing " + " < ".join(f"{x:.3f}" for x in ls))
    check(all(a <= b + 1e-9 for a, b in zip(sats, sats[1:])), "order --st-* saturation non-decreasing " + " <= ".join(f"{x:.2f}" for x in sats))

    thr = [rgb(t[f"--thr-{i}"]) for i in range(5)]
    hues = [colorsys.rgb_to_hls(*c)[0] * 360 for c in thr]
    check(all(a >= b for a, b in zip(hues, hues[1:])), "order --thr-0..4 hue non-increasing " + " >= ".join(f"{x:.0f}" for x in hues))
    check(lum(thr[0]) == min(lum(c) for c in thr), "order --thr-0 is the dimmest step")

    bad = [m for ok, m in results if not ok]
    for ok, m in results:
        print(("PASS " if ok else "FAIL ") + m)
    print(f"{len(results) - len(bad)}/{len(results)} checks passed")
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
