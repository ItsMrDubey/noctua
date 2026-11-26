"""Intel-console theme: WCAG contrast + palette rules (scripts/check_contrast.py), CSS lint
(scripts/check_css.py) and js/symbology.js reading its colours from the theme tokens."""

import importlib.util
import json
import re
import shutil
import subprocess

import pytest

from osint.config import ROOT

SCRIPTS = ROOT / "scripts"
THEME = ROOT / "frontend" / "css" / "theme.css"
SYM = (ROOT / "frontend" / "js" / "symbology.js").as_uri()
NODE = shutil.which("node")


def _load(name):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_contrast_and_palette_rules_pass(capsys):
    assert _load("check_contrast").main([str(THEME)]) == 0, capsys.readouterr().out


def test_css_lint_clean():
    found = _load("check_css").lint(ROOT / "frontend" / "css")
    assert found == [], "\n".join(found)


def _node(prelude: str, body: str):
    src = prelude + f'\nconst S = await import("{SYM}");\nconst out = (() => {{ {body} }})();\nconsole.log(JSON.stringify(out));\n'
    r = subprocess.run([NODE, "--input-type=module"], input=src.encode(), capture_output=True, timeout=60, check=False)
    assert r.returncode == 0, r.stderr.decode("utf-8", "replace")[-800:]
    return json.loads(r.stdout.decode().strip().splitlines()[-1])


def _norm(v: str) -> str:
    return re.sub(r"\s+", "", v).lower()


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_symbology_fallbacks_equal_theme_tokens():
    tokens = _load("check_contrast").parse_tokens(THEME.read_text(encoding="utf-8"))
    out = _node("", "return { fb: S.PALETTE_FALLBACK, cat: S.catColor('economic'), unknown: S.catColor('nope'),"
                    " arr: S.EVENT_CATS.economic[1], thr: S.threatColor('events', { category: 'economic' }),"
                    " st: S.statusColor('bogus'), hi: S.threatLevelColor(9), lo: S.threatLevelColor(-3), air: S.COLORS.air };")
    fb = out["fb"]
    for k, v in fb.items():
        assert k in tokens, f"{k} is not declared in theme.css"
        assert _norm(v) == _norm(tokens[k]), f"{k}: symbology.js {v} != theme.css {tokens[k]}"
    sym = {k for k in tokens if re.match(r"--(cat|st|thr|sym)-", k)}
    assert sym <= set(fb), f"missing from PALETTE_FALLBACK: {sorted(sym - set(fb))}"
    assert out["cat"] == out["arr"] == out["thr"] == fb["--cat-economic"]
    assert out["unknown"] == fb["--cat-other"]
    assert out["st"] == fb["--st-reported"]
    assert (out["hi"], out["lo"]) == (fb["--thr-4"], fb["--thr-0"])
    assert out["air"] == fb["--sym-air"]


MOCK_DOM = """
globalThis.document = { documentElement: {} };
globalThis.getComputedStyle = () => ({ getPropertyValue: (k) => ({ "--cat-economic": "  #123456 ", "--line-2": " rgba(1, 2, 3, .4)" })[k] || "" });
globalThis.Cesium = { Color: { fromCssColorString: (s) => ({ s, withAlpha(a) { return { s: this.s, a }; } }) } };
"""


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_symbology_reads_theme_tokens_and_caches_cesium_colours():
    out = _node(MOCK_DOM, "const a = S.cesiumColor('--cat-economic', 0.5); return { cat: S.catColor('economic'),"
                          " arr: S.EVENT_CATS.economic[1], line: S.cssColor('--line-2'), air: S.COLORS.air, ces: a,"
                          " same: a === S.cesiumColor('--cat-economic', 0.5), plain: S.cesiumColor('--cat-economic') };")
    assert out["cat"] == out["arr"] == "#123456"
    assert out["line"] == "rgba(1, 2, 3, .4)"
    assert out["air"] == "#8fe8f5"  # not set by the mock: falls back
    assert out["ces"] == {"s": "#123456", "a": 0.5} and out["same"] is True
    assert out["plain"] == {"s": "#123456"}
