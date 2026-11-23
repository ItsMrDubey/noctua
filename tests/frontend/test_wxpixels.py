"""Weather tile processing (frontend/js/wxpixels.js): IR decoding to cloud-top temperature,
the shared palette, latitude fade, RainViewer re-colouring and the 3D-cloud helpers. The module
is pure (no Cesium / DOM), so each case runs it under node; skipped without node."""

import json
import shutil
import subprocess

import pytest

from osint.config import ROOT

NODE = shutil.which("node")
MOD = (ROOT / "frontend" / "js" / "wxpixels.js").as_uri()
pytestmark = pytest.mark.skipif(NODE is None, reason="node not installed")


def run(body: str):
    src = f'import * as W from "{MOD}";\nconst out = (() => {{ {body} }})();\nconsole.log(JSON.stringify(out));\n'
    r = subprocess.run([NODE, "--input-type=module"], input=src.encode(), capture_output=True, timeout=60, check=False)
    assert r.returncode == 0, r.stderr.decode("utf-8", "replace")[-800:]
    return json.loads(r.stdout.decode().strip().splitlines()[-1])


def test_gibs_colormap_inverts_to_temperature():
    r = run("""const g = W.gibsTable(), t = (rgb) => g.has(rgb) ? Math.round(W.tOf(g.get(rgb))) : null;
      return { white: t(0xffffff), magenta: t(0x7f007f), red: t(0xff0000), cyan: t(0x00ffff),
               grey197: t(197 * 65793), grey1: t(65793), grey129: t(129 * 65793), unknown: t(0x123456) };""")
    assert r["white"] == -92 and r["magenta"] == -91
    assert r["red"] == -61 and r["cyan"] == -19
    assert r["grey197"] == -19 and r["grey1"] == 57
    # 129 is both a cold grey (-75 C) and on the warm ramp: the (far more common) warm value wins
    assert 4 <= r["grey129"] <= 9
    assert r["unknown"] is None


def test_grey_products_white_is_cold():
    r = run("""const m = W.greyTable('mtg'), s = W.greyTable('msg');
      return [W.tOf(m[0]), W.tOf(m[255]), W.tOf(s[0]), W.tOf(s[255])].map(Math.round);""")
    assert r == [30, -80, 30, -67]


def test_palette_warm_clear_cold_enhanced_and_monotonic_alpha():
    r = run("""const a = (t, s) => W.irColour(t, s)[3];
      return { warm: a(25), sea: a(10), low: a(-5), mid: a(-25), cold: a(-45), storm: a(-70),
               stWarm: a(-20, 'storms'), stCold: a(-50, 'storms'), c70: W.irColour(-70),
               alphas: [5, 0, -10, -20, -30, -40, -50].map((t) => a(t)) };""")
    assert r["warm"] == 0 and r["sea"] == 0
    assert 0 < r["low"] < r["mid"] < r["cold"] <= r["storm"] <= 255
    assert r["alphas"] == sorted(r["alphas"])
    assert r["stWarm"] == 0 and r["stCold"] > 180  # storm style: only cold tops
    red, green, blue, _ = r["c70"]
    assert red > 200 and blue > 200 and green < 120  # magenta for very cold convective tops


def test_process_ir_recolours_and_drops_undecodable_pixels():
    r = run("""const px = new Uint8ClampedArray([255, 255, 255, 255,  20, 20, 20, 255,  9, 9, 9, 0,  200, 200, 200, 255]);
      W.processIR(px, 4, 1, W.greyTable('mtg'), W.irPalette('clouds'), [10], 70);
      const cold = Array.from(px.slice(0, 4)), warm = px[7], off = px[11], mid = px[15];
      const px2 = new Uint8ClampedArray([255, 255, 255, 255]);
      W.processIR(px2, 1, 1, W.greyTable('mtg'), W.irPalette('clouds'), [69], 70);
      return { cold, warm, off, mid, polar: px2[3] };""")
    assert r["cold"][3] > 200 and r["warm"] == 0 and r["off"] == 0 and 0 < r["mid"] < 255
    assert r["polar"] < 20  # faded out next to the latitude cut


def test_mercator_rows_and_fade():
    r = run("""const top = W.mercRowLats(0, 85.05112878 * Math.PI / 180, 256), up = W.mercRowLats(0, 85.05112878 * Math.PI / 180, 256, true);
      return { first: top[0], last: top[255], upFirst: up[0], f: [W.latFade(0), W.latFade(62), W.latFade(66), W.latFade(70), W.latFade(-75)] };""")
    assert 84.9 < r["first"] < 85.06 and 0 < r["last"] < 0.5 and abs(r["upFirst"] - r["last"]) < 1e-6
    assert r["f"][0] == 1 and r["f"][1] == 1 and 0 < r["f"][2] < 1 and r["f"][3] == 0 and r["f"][4] == 0


def test_radar_decoder_round_trips_the_rainviewer_table():
    r = run("""const ref = W.ubTable(), dec = W.radarDecoder();
      // an exact table colour decodes to an entry drawn in that same colour (identical colours, e.g.
      // white 65-74 dBZ, are interchangeable)
      const col = new Map(ref.map((e) => [e.dbz + ':' + e.snow, e.rgba.join()]));
      let bad = 0; for (const e of ref) { const d = dec(...e.rgba); if (col.get(d.dbz + ':' + d.snow) !== e.rgba.join()) bad++; }
      const px = new Uint8ClampedArray([0x63, 0x61, 0x59, 0x14,  0xff, 0xaa, 0x00, 0xff,  0, 0, 0, 0]);
      W.processRadar(px, dec);
      return { n: ref.length, bad, drizzle: px[3], heavy: Array.from(px.slice(4, 8)), empty: px[11] };""")
    assert r["n"] > 150
    assert r["bad"] == 0
    assert r["drizzle"] == 0  # -10 dBZ clutter is not drawn
    assert r["heavy"][3] > 200 and r["heavy"][0] > 200 and r["empty"] == 0


def test_radar_palette_and_mask_hatch():
    r = run("""const a = [0, 5, 10, 20, 35, 50, 65].map((d) => W.radarColour(d)[3]);
      const snow = W.radarColour(20, true);
      const m = new Uint8ClampedArray(8 * 8 * 4); for (let i = 0; i < 32; i++) m[i * 4 + 3] = 255; // top half: no radar
      W.processMask(m, 8, 8);
      let hatched = 0, clear = 0; for (let i = 0; i < 64; i++) { if (i < 32 && m[i * 4 + 3] > 0) hatched++; if (i >= 32 && m[i * 4 + 3] === 0) clear++; }
      const v = new Uint8ClampedArray(8 * 4); for (let i = 0; i < 8; i++) v[i * 4 + 3] = 255;
      W.processMask(v, 8, 1, false);
      const veil = new Set(); for (let i = 0; i < 8; i++) veil.add(v[i * 4 + 3]);
      return { a, snow, hatched, clear, veil: [...veil] };""")
    assert r["a"][0] == 0 and r["a"][1] == 0 and r["a"] == sorted(r["a"])
    assert r["snow"][2] > r["snow"][0]  # snow drawn in cool periwinkle
    assert r["hatched"] == 32 and r["clear"] == 32
    assert len(r["veil"]) == 1 and 0 < r["veil"][0] < 60  # zoomed out: uniform faint veil, no lines


def test_cloud_specs_deterministic_capped_and_sunlit():
    r = run("""const items = [{ lat: 50, lon: 10, low: 90, mid: 40, high: 0 }, { lat: 50.25, lon: 10, low: 5, mid: 0, high: 100 }];
      const a = W.cloudSpecs(items, 0.25), b = W.cloudSpecs(items, 0.25), capped = W.cloudSpecs(items, 0.25, 5);
      const noon = W.sunElevation(0, 0, new Date(Date.UTC(2026, 2, 20, 12, 7))), midnight = W.sunElevation(0, 0, new Date(Date.UTC(2026, 2, 20, 0, 7)));
      return { same: JSON.stringify(a) === JSON.stringify(b), n: a.length, capped: capped.length,
               levels: [...new Set(a.map((c) => c.level))].sort(), maxW: Math.max(...a.map((c) => c.w)),
               noon, midnight, light: [W.cloudLight(30), W.cloudLight(0), W.cloudLight(-30)],
               fade: [W.cloudFade(1e4, 5e4), W.cloudFade(1e6, 5e4)] };""")
    assert r["same"] and r["capped"] == 5 and r["n"] > 5
    assert r["levels"] == ["high", "low", "mid"] and r["maxW"] <= 9000
    assert r["noon"] > 80 and r["midnight"] < -80
    assert r["light"][0] == 1 and r["light"][2] < 0.2 < r["light"][1] < 1
    assert r["fade"] == [1, 0]
