"""Lint frontend/css for the intel-console theme rules.

  font    no font size below 11px (font-size and the font shorthand)
  blur    no backdrop-filter
  shadow  no drop shadows (box-shadow / text-shadow with a y offset >= 4px)
  legacy  none of the old NOCTUA cyan palette literals
  literal outside theme.css: no hex colours other than #000 / #fff and no rgba() with literal
          non-neutral rgb, except on lines tagged /* legend */ (data legends mirroring globe ramps)

Usage: python scripts/check_css.py [css_dir]   -> exit 0 when clean, 1 with one line per finding.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CSS = ROOT / "frontend" / "css"
FONT = re.compile(r"font-size:\s*(\d+(?:\.\d+)?)px|font:\s*(?:[a-z-]+\s+|\d00\s+)*(\d+(?:\.\d+)?)px")
SHADOW_DECL = re.compile(r"(?:box|text)-shadow:\s*([^;}]*)")
NUM = re.compile(r"-?\d*\.?\d+(?:px)?")
LEGACY = ("94, 227, 234", "120, 175, 205", "#5ee3ea", "#05080d", "#08101a", "#03060a", "242, 173, 61", "255, 75, 85")
HEX = re.compile(r"#[0-9a-fA-F]{3,8}\b")
RGB = re.compile(r"rgba?\(\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)")
NEUTRAL = {("0", "0", "0"), ("255", "255", "255")}


def drop_shadow_y(value: str) -> float:
    """Largest vertical offset among the shadows in a box-/text-shadow value."""
    v = value
    while re.search(r"\([^()]*\)", v):
        v = re.sub(r"\([^()]*\)", "", v)  # drop colour functions (nested parens)
    ys = []
    for part in v.split(","):
        nums = [t for t in part.split() if NUM.fullmatch(t)]
        if len(nums) >= 2:
            ys.append(abs(float(nums[1].removesuffix("px"))))
    return max(ys, default=0.0)


def lint(css_dir: Path) -> list[str]:
    out: list[str] = []
    for f in sorted(css_dir.glob("*.css")):
        theme = f.name == "theme.css"
        for n, line in enumerate(f.read_text(encoding="utf-8").splitlines(), 1):
            where = f"{f.name}:{n}"
            code = re.sub(r"/\*.*?\*/", "", line)
            for m in FONT.finditer(code):
                v = float(m.group(1) or m.group(2))
                if v < 11:
                    out.append(f"font    {where} {v:g}px < 11px")
            if "backdrop-filter" in code:
                out.append(f"blur    {where} backdrop-filter")
            for m in SHADOW_DECL.finditer(code):
                if drop_shadow_y(m.group(1)) >= 4:
                    out.append(f"shadow  {where} drop shadow y={drop_shadow_y(m.group(1)):g}px")
            for lit in LEGACY:
                if lit in code.lower():
                    out.append(f"legacy  {where} {lit}")
            if theme or "/* legend */" in line:
                continue
            for h in HEX.findall(code):
                if h.lower() not in ("#000", "#fff", "#000000", "#ffffff"):
                    out.append(f"literal {where} {h}")
            for m in RGB.finditer(code):
                if m.groups() not in NEUTRAL:
                    out.append(f"literal {where} {m.group(0)}")
    return out


def main(argv: list[str]) -> int:
    found = lint(Path(argv[0]) if argv else CSS)
    for x in found:
        print(x)
    print(f"{len(found)} findings")
    return 1 if found else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
