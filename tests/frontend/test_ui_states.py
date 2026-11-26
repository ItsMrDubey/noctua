"""ui.js stateHtml / settleLoading: every loading state settles into an error. ui.js imports
/static/... modules, so the two helpers are extracted from the source and run in node (skipped without node)."""

import json
import re
import shutil
import subprocess

import pytest

from osint.config import ROOT

NODE = shutil.which("node")
SRC = (ROOT / "frontend" / "js" / "ui.js").read_text(encoding="utf-8")

SCRIPT = """
const esc = (s) => String(s == null ? "" : s).replace(/[&<>"']/g, (c) => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"})[c]);
%s
const out = {};
out.a = stateHtml("empty", "No <b>", "x & y");
out.b = stateHtml("loading", "Loading");
let h1 = '<div class="state state-loading">L</div>';
const el1 = { get innerHTML() { return h1; }, set innerHTML(v) { h1 = v; }, querySelector: () => (h1.includes("state-loading") ? {} : null) };
settleLoading(el1, 20, "Gone");
let h2 = '<div class="state state-empty">E</div>';
const el2 = { get innerHTML() { return h2; }, set innerHTML(v) { h2 = v; }, querySelector: () => (h2.includes("state-loading") ? {} : null) };
settleLoading(el2, 20, "Gone");
settleLoading(null, 20, "x");
let h3 = '<div class="state state-loading">L</div>';
const el3 = { get innerHTML() { return h3; }, set innerHTML(v) { h3 = v; }, querySelector: () => (h3.includes("state-loading") ? {} : null) };
settleLoading(el3, 20, "Gone", () => false);
await new Promise((r) => setTimeout(r, 80));
out.settled = h1; out.kept = h2; out.superseded = h3;
console.log(JSON.stringify(out));
"""


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_state_helpers():
    m = re.search(r"^export const stateHtml.*?^}\n", SRC, re.S | re.M)
    assert m, "stateHtml/settleLoading not found in ui.js"
    code = m.group(0).replace("export ", "")
    script = SCRIPT % code
    r = subprocess.run([NODE, "--input-type=module", "-e", script], capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    out = json.loads(r.stdout.strip().splitlines()[-1])
    assert out["a"] == '<div class="state state-empty"><b>No &lt;b&gt;</b>x &amp; y</div>'
    assert out["b"] == '<div class="state state-loading"><b>Loading</b></div>'
    assert 'state-err' in out["settled"] and "Gone" in out["settled"] and "No answer within 0 s" in out["settled"]
    assert out["kept"] == '<div class="state state-empty">E</div>'
    # a superseded request's timer leaves a later load's loading state alone
    assert out["superseded"] == '<div class="state state-loading">L</div>'


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_trail_state():
    src = (ROOT / "frontend" / "js" / "panels" / "inspector.js").read_text(encoding="utf-8")
    m = re.search(r"^export function trailState.*?^}\n", src, re.S | re.M)
    assert m, "trailState not found in inspector.js"
    code = m.group(0).replace("export ", "")
    pts = "[{}, {}]"
    script = code + """
const o = [
  trailState({ inFlight: true, pts: null }),
  trailState({ inFlight: true, pts: [{}] }),
  trailState({ inFlight: true, pts: [] }),
  trailState({ inFlight: true, pts: null, error: "boom" }),
  trailState({ inFlight: false, pts: [{}] }),
  trailState({ inFlight: false, pts: [] }),
  trailState({ inFlight: false, pts: null, error: "boom" }),
  trailState({ inFlight: false, pts: null }),
  trailState({ inFlight: true, pts: %s }),
  trailState({ inFlight: false, pts: %s, error: "x" }),
];
console.log(JSON.stringify(o));
""" % (pts, pts)
    r = subprocess.run([NODE, "--input-type=module", "-e", script], capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    assert json.loads(r.stdout.strip().splitlines()[-1]) == ["loading", "empty", "empty", "error", "empty", "empty", "error", "off", "ready", "ready"]


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_trail_toggle_off_mid_request_settles():
    """Trails.showSelected/clearSelected (real source) + the inspector's trailReq bookkeeping: toggling Trail
    off while a request is in flight must end in 'off', never stay 'loading'."""
    tsrc = (ROOT / "frontend" / "js" / "trails.js").read_text(encoding="utf-8")
    isrc = (ROOT / "frontend" / "js" / "panels" / "inspector.js").read_text(encoding="utf-8")
    ts = re.search(r"^export function trailState.*?^}\n", isrc, re.S | re.M).group(0).replace("export ", "")
    cs = re.search(r"^  clearSelected\(\).*?\n", tsrc, re.M).group(0)
    ss = re.search(r"^  async showSelected.*?^  }\n", tsrc, re.S | re.M).group(0)
    script = ts + """
let release; const fetchJson = () => new Promise((r) => { release = r; });
class T { constructor() { this.sel = { removeAll() {} }; this.scene = { requestRender() {} }; this.selKey = null; this.lastSel = 0; }
  _draw() {}
%s
%s}
const t = new T(); let req = { inFlight: false, error: null }, pts = null;
t.onState = (k, f, e) => { req = { inFlight: f, error: e }; };
t.onPoints = (k, p) => { pts = p; req = { inFlight: false, error: null }; };
const C = { Color: {} }; globalThis.colorFor = () => ({});
const p = t.showSelected("aircraft", "a1", {}, true);
const during = trailState({ ...req, pts });
t.clearSelected(); pts = null; req = { inFlight: false, error: null }; // app.js: clearSelected + inspector.resetTrail
release({ points: [{}, {}] }); await p;
console.log(JSON.stringify([during, trailState({ ...req, pts })]));
""" % (cs, ss)
    r = subprocess.run([NODE, "--input-type=module", "-e", script], capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    assert json.loads(r.stdout.strip().splitlines()[-1]) == ["loading", "off"]


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_trail_periodic_refresh_does_not_flicker():
    """A throttled (non-forced) refresh of the same selected track must not re-emit onState(true);
    a forced call or a different track does."""
    tsrc = (ROOT / "frontend" / "js" / "trails.js").read_text(encoding="utf-8")
    ss = re.search(r"^  async showSelected.*?^  }\n", tsrc, re.S | re.M).group(0)
    script = """
const fetchJson = async () => ({ points: [{}, {}] });
class T { constructor() { this.sel = { removeAll() {} }; this.scene = { requestRender() {} }; this.selKey = null; this.lastSel = 0; }
  _draw() {}
%s}
const t = new T(); const ev = [];
t.onState = (k, f) => ev.push(k + ":" + f); t.onPoints = () => {};
const C = { Color: {} }; globalThis.colorFor = () => ({});
await t.showSelected("aircraft", "a1", {}, false);
t.lastSel = 0; await t.showSelected("aircraft", "a1", {}, false);
t.lastSel = 0; await t.showSelected("aircraft", "a1", {}, true);
t.lastSel = 0; await t.showSelected("aircraft", "a2", {}, false);
console.log(JSON.stringify(ev));
""" % ss
    r = subprocess.run([NODE, "--input-type=module", "-e", script], capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    assert json.loads(r.stdout.strip().splitlines()[-1]) == ["aircraft:a1:true", "aircraft:a1:true", "aircraft:a2:true"]
