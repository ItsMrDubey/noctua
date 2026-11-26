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
await new Promise((r) => setTimeout(r, 80));
out.settled = h1; out.kept = h2;
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
