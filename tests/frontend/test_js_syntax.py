"""Every frontend module parses as an ES module. A syntax error in any one of them stops
app.js (and the built bundle) from loading, leaving a black globe with no server-side
error. `node --check <file>` parses .js as CommonJS and misses some module errors, so the
source is checked as a module via stdin."""

import shutil
import subprocess

import pytest

from osint.config import ROOT

NODE = shutil.which("node")
MODULES = sorted(p for p in (ROOT / "frontend").rglob("*.js")
                 if not {"vendor", "dist", "node_modules"} & set(p.parts))


@pytest.mark.skipif(NODE is None, reason="node not installed")
@pytest.mark.parametrize("path", MODULES, ids=lambda p: p.relative_to(ROOT).as_posix())
def test_module_parses(path):
    r = subprocess.run([NODE, "--input-type=module", "--check"], input=path.read_bytes(), check=False,
                       capture_output=True, timeout=60)
    assert r.returncode == 0, r.stderr.decode("utf-8", "replace")[-800:]
