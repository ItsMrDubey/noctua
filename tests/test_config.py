from pathlib import Path

from osint.config import ROOT, Settings, load_sources


def test_settings_reads_env(monkeypatch, tmp_path):
    monkeypatch.setenv("FIRMS_MAP_KEY", "abc")
    monkeypatch.setenv("OSINT_DATA_DIR", str(tmp_path))
    s = Settings(_env_file=None)
    assert s.firms_map_key == "abc"
    assert s.data_dir == tmp_path
    assert s.sqlite_path == tmp_path / "osint.sqlite"
    assert s.lake_dir == tmp_path / "lake"


def test_settings_defaults(monkeypatch):
    monkeypatch.delenv("OSINT_DATA_DIR", raising=False)
    s = Settings(_env_file=None)
    assert s.data_dir == ROOT / "data"
    assert s.port == 8765
    assert s.reference_dir == ROOT / "reference"


def test_load_sources_normalises_empty_blocks(tmp_path: Path):
    (tmp_path / "sources.yaml").write_text("usgs:\n  interval_s: 120\nais:\n", encoding="utf-8")
    assert load_sources(tmp_path) == {"usgs": {"interval_s": 120}, "ais": {}}


def test_repo_sources_yaml_lists_collectors():
    assert set(load_sources(ROOT / "config")) == {
        "adsb", "ais", "firms", "usgs", "gdelt", "rss", "news_alterlab", "nga_msi", "acled",
        "ucdp_candidate", "telegram", "gdacs", "eonet", "reliefweb", "adsb_signals", "celestrak",
        "ioda", "swpc", "radiation"}

def test_contact_from_env_file_is_exported(tmp_path, monkeypatch):
    import os

    from osint.config import Settings
    monkeypatch.delenv("OSINT_CONTACT", raising=False)
    (tmp_path / ".env").write_text("OSINT_CONTACT=https://example.org/contact\n", encoding="utf-8")
    s = Settings(_env_file=tmp_path / ".env")
    assert s.contact == "https://example.org/contact"
    assert os.environ["OSINT_CONTACT"] == "https://example.org/contact"
