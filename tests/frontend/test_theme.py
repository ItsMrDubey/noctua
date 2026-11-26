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
