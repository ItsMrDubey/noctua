import gzip

import pytest

from osint.frontend_build import BuildError, build, bundle_js
from osint.config import ROOT


def _fe(tmp_path, files: dict[str, str], html: str | None = None):
    fe = tmp_path / "fe"
    for rel, text in files.items():
        p = fe / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
    (fe / "index.html").write_text(html or (
        '<html><head>\n<link rel="stylesheet" href="/static/css/a.css">\n'
        '<link rel="stylesheet" href="/static/css/b.css">\n</head><body>\n'
        '<script type="module" src="/static/app.js"></script>\n</body></html>'), encoding="utf-8")
    return fe


BASIC = {
    "app.js": 'import { add, K as KK } from "/static/js/m.js";\nimport { twice } from "/static/js/n.js";\n'
              "window.__out = [add(1, 2), KK, twice(4)];\n",
    "js/m.js": "export const K = 7;\nexport function add(a, b) { return a + b; }\nconst hidden = 1;\n",
    "js/n.js": 'import { add } from "/static/js/m.js";\nexport function twice(x) { return add(x, x); }\n',
    "css/a.css": "a{color:red}", "css/b.css": "b{color:blue}",
}


def test_bundle_order_and_scoping(tmp_path):
    fe = _fe(tmp_path, BASIC)
    js = bundle_js(fe, fe / "app.js")
    assert js.index("js/m.js") < js.index("js/n.js") < js.index("// ---- app.js")
    assert js.count("// ---- js/m.js") == 1  # shared dependency emitted once
    assert "const { add, K: KK } = __m0;" in js and "export" not in js
    assert "return { K, add };" in js  # non-exported names stay private


def test_build_outputs(tmp_path):
    fe = _fe(tmp_path, BASIC)
    out = build(fe)
    html = (fe / "dist" / "index.html").read_text()
    assert f'/static/dist/assets/{out["js"]}' in html and "/static/app.js" not in html
    assert html.count("stylesheet") == 1 and out["css"] in html
    css = (fe / "dist" / "assets" / out["css"]).read_text()
    assert css.index("color:red") < css.index("color:blue")
    js = (fe / "dist" / "assets" / out["js"]).read_bytes()
    assert gzip.decompress((fe / "dist" / "assets" / (out["js"] + ".gz")).read_bytes()) == js
    assert build(fe)["js"] == out["js"]  # content hash is deterministic


@pytest.mark.parametrize("bad,msg", [
    ("export default 1;\n", "unsupported export"),
    ("export { a };\n", "unsupported export"),
    ('import * as x from "/static/js/m.js";\n', "unsupported import"),
    ("const m = import('/static/js/m.js');\n", "dynamic import"),
    ('import { nope } from "/static/js/m.js";\n', "does not export"),
    ('import { a } from "/static/js/missing.js";\n', "not found"),
])
def test_rejects_unsupported(tmp_path, bad, msg):
    fe = _fe(tmp_path, {**BASIC, "app.js": bad})
    with pytest.raises(BuildError, match=msg):
        bundle_js(fe, fe / "app.js")


def test_rejects_cycle(tmp_path):
    fe = _fe(tmp_path, {**BASIC,
                        "js/m.js": 'import { twice } from "/static/js/n.js";\nexport const K = 1;\n'
                                   "export function add() {}\n"})
    with pytest.raises(BuildError, match="import cycle"):
        bundle_js(fe, fe / "app.js")


def test_real_frontend_bundles():
    js = bundle_js(ROOT / "frontend", ROOT / "frontend" / "app.js")
    assert js.count("// ---- ") > 20
