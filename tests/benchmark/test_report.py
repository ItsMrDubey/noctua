"""Markdown investor brief: examples and rendering, plus the CLI entry point."""

from datetime import timedelta

import pytest

from osint.benchmark import report
from osint.config import ROOT
from tests.benchmark.fixtures import NOW, build, service


@pytest.fixture
def env(tmp_path):
    built = build(tmp_path)
    yield tmp_path, built["lake"]
    built["lake"].close()


def test_examples_and_render(env):
    tmp, lake = env
    svc = service(tmp, lake)
    ex = report.find_examples(svc.src, NOW - timedelta(hours=24), NOW)
    assert ex["duplicated"]["gdelt_rows"] == 3 and ex["duplicated"]["gdelt_urls"] == 3
    assert ex["centroid"]["gdelt_event_id"] == "g7" and ex["centroid"]["num_mentions"] == 66
    assert ex["sensor"]["kinds"] == ["quake"]
    assert ex["mismatch"]["reasons"] == ["country: article UA"]
    assert ex["latency"]["lead_min"] == 15.0
    text = report.render(svc.compute("24h"), ex)
    assert text.startswith("# GDELT 2.0 benchmark — 24h window")
    assert "| Records per real-world incident | 1.25 | 2.00 | lower |" in text
    assert "## Caveats" in text and "## Illustrative examples" in text
    assert "> in Lviv (where)" in text
    assert "GDELT emitted **3 rows**" in text
    svc.store.close()


def test_render_without_examples(env):
    tmp, _lake = env
    svc = service(tmp, None)
    text = report.render(svc.compute("24h"), dict.fromkeys(report.TITLES))
    assert text.count("_No example in this window._") == 5
    svc.store.close()


def test_cli_writes_report(env, monkeypatch, tmp_path_factory):
    tmp, lake = env
    lake.close()
    monkeypatch.setenv("OSINT_DATA_DIR", str(tmp))
    monkeypatch.setenv("OSINT_CONFIG_DIR", str(ROOT / "config"))
    out_dir = tmp_path_factory.mktemp("digests")
    assert report.main(["--window", "7d", "--out", str(out_dir / "benchmark-<date>.md")]) == 0
    files = list(out_dir.glob("benchmark-*.md"))
    assert len(files) == 1 and "<date>" not in files[0].name
    assert "7d window" in files[0].read_text(encoding="utf-8")
    with pytest.raises(SystemExit):
        report.main(["--window", "1y", "--out", str(out_dir / "x.md")])
