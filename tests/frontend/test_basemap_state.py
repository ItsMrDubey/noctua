"""basemap.js loadBasemapState: Dim for data defaults on, and pre-v2 stored state is migrated once."""

import json
import re
import shutil
import subprocess

import pytest

from osint.config import ROOT

NODE = shutil.which("node")

SCRIPT = """
const mk = (v) => ({ getItem: () => v });
const o = [
  loadBasemapState(mk(null)),
  loadBasemapState(mk(JSON.stringify({ src: "x", dim: false }))),
  loadBasemapState(mk(JSON.stringify({ v: 1, src: "bing", dim: false }))),
  loadBasemapState(mk(JSON.stringify({ v: 2, dim: false }))),
  loadBasemapState(mk(JSON.stringify({ v: 2, src: "bing", dim: true }))),
  loadBasemapState(mk("{not json")),
  loadBasemapState({ getItem() { throw new Error("denied"); } }),
];
console.log(JSON.stringify(o));
"""


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_basemap_state_migration():
    src = (ROOT / "frontend" / "js" / "basemap.js").read_text(encoding="utf-8")
    key = re.search(r"^const KEY = .*?\n", src, re.MULTILINE).group(0)
    ver = re.search(r"^const VER = .*?\n", src, re.MULTILINE).group(0)
    fn = re.search(r"^export function loadBasemapState.*?^}\n", src, re.DOTALL | re.MULTILINE).group(0).replace("export ", "")
    r = subprocess.run([NODE, "--input-type=module", "-e", key + ver + fn + SCRIPT], check=False, capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    o = json.loads(r.stdout.strip().splitlines()[-1])
    assert o[0] == {"src": "esri", "dim": True, "v": 2}
    assert o[1] == {"src": "x", "dim": True, "v": 2}
    assert o[2] == {"src": "bing", "dim": True, "v": 2}
    assert o[3] == {"src": "esri", "dim": False, "v": 2}
    assert o[4] == {"src": "bing", "dim": True, "v": 2}
    assert o[5] == {"src": "esri", "dim": True, "v": 2}
    assert o[6] == {"src": "esri", "dim": True, "v": 2}
