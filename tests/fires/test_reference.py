import io
import zipfile

import httpx
import pytest
import respx

from osint.fires import reference

FLARE_URL = "https://eog.test/flares.xlsx"
VOLCANO_URL = "https://gvp.test/wfs"
NS = 'xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"'


def _inline(ref, text):
    return f'<c r="{ref}" t="inlineStr"><is><t>{text}</t></is></c>'


def _num(ref, v):
    return f'<c r="{ref}" t="n"><v>{v}</v></c>'


def xlsx() -> bytes:
    """Two flare sheets (inline strings / shared strings) and a country summary sheet."""
    s1 = (f"<worksheet {NS}><sheetData>"
          f'<row r="1">{_inline("A1", "Country")}{_inline("D1", "Latitude")}'
          f'{_inline("E1", "Longitude")}{_inline("K1", "Type")}</row>'
          f'<row r="2">{_inline("A2", "Iraq")}{_num("D2", "30.5")}{_num("E2", "47.75")}'
          f'{_inline("K2", "upstream")}</row>'
          f'<row r="3">{_inline("A3", "Bad")}{_num("D3", "95")}{_num("E3", "1")}</row>'
          "</sheetData></worksheet>")
    s2 = (f"<worksheet {NS}><sheetData>"
          '<row r="1"><c r="A1" t="s"><v>0</v></c><c r="B1" t="s"><v>1</v></c>'
          '<c r="C1" t="s"><v>2</v></c></row>'
          '<row r="2"><c r="A2"><v>26.1</v></c><c r="B2"><v>50.2</v></c>'
          '<c r="C2" t="s"><v>3</v></c></row></sheetData></worksheet>')
    s3 = (f"<worksheet {NS}><sheetData><row r=\"1\">{_inline('A1', 'Country')}"
          f"{_inline('B1', 'BCM')}</row><row r=\"2\">{_inline('A2', 'Iraq')}{_num('B2', 17)}"
          "</row></sheetData></worksheet>")
    shared = (f"<sst {NS}><si><t>Latitude</t></si><si><t>Longitude</t></si>"
              "<si><t>Type</t></si><si><t>refinery</t></si></sst>")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("xl/worksheets/sheet1.xml", s1)
        z.writestr("xl/worksheets/sheet2.xml", s2)
        z.writestr("xl/worksheets/sheet10.xml", s3)
        z.writestr("xl/sharedStrings.xml", shared)
    return buf.getvalue()


GVP = {"type": "FeatureCollection", "features": [
    {"geometry": {"type": "Point", "coordinates": [15.0, 37.75]},
     "properties": {"Volcano_Name": "Etna", "Last_Eruption_Year": 2025, "Country": "Italy"}},
    {"geometry": {"type": "Point", "coordinates": [6.85, 50.17]},
     "properties": {"Volcano_Name": "West Eifel", "Last_Eruption_Year": -8300}},
    {"geometry": {"type": "Point", "coordinates": [1.0, 1.0]},
     "properties": {"Volcano_Name": "Unknown", "Last_Eruption_Year": None}},
    {"geometry": None, "properties": {"Volcano_Name": "No geometry"}},
]}


def test_parse_flare_xlsx():
    assert reference.parse_flare_xlsx(xlsx()) == [
        [30.5, 47.75, "upstream"], [26.1, 50.2, "refinery"]]


def test_parse_volcanoes_and_filter(tmp_path):
    items = reference.parse_volcanoes(GVP)
    assert [v["name"] for v in items] == ["Etna", "West Eifel", "Unknown"]
    reference._write_json(tmp_path / reference.VOLCANO_FILE, items)
    assert [v["name"] for v in reference.load_volcanoes(tmp_path, 1800)] == ["Etna"]
    assert len(reference.load_volcanoes(tmp_path)) == 3


async def test_fetch_writes_caches(tmp_path):
    with respx.mock:
        respx.get(FLARE_URL).mock(return_value=httpx.Response(200, content=xlsx()))
        respx.get(VOLCANO_URL).mock(return_value=httpx.Response(200, json=GVP))
        async with httpx.AsyncClient() as http:
            assert await reference.fetch_flares(http, FLARE_URL, tmp_path) == 2
            assert await reference.fetch_volcanoes(http, VOLCANO_URL, tmp_path) == 3
    assert reference.load_flares(tmp_path)[0] == [30.5, 47.75, "upstream"]


async def test_fetch_errors_keep_previous_cache(tmp_path):
    reference._write_json(tmp_path / reference.FLARE_FILE, [[1.0, 2.0, "upstream"]])
    with respx.mock:
        respx.get(FLARE_URL).mock(side_effect=[httpx.Response(503),
                                               httpx.Response(200, content=b"<html>login</html>")])
        async with httpx.AsyncClient() as http:
            with pytest.raises(RuntimeError, match="HTTP 503"):
                await reference.fetch_flares(http, FLARE_URL, tmp_path)
            with pytest.raises(RuntimeError, match="not an xlsx"):
                await reference.fetch_flares(http, FLARE_URL, tmp_path)
    assert reference.load_flares(tmp_path) == [[1.0, 2.0, "upstream"]]


def test_load_cities(tmp_path):
    rows = ["1\tKyiv\tKyiv\t\t50.45\t30.52\tP\tPPLC\tUA\t\t\t\t\t\t2900000\t",
            "2\tTinyville\tTinyville\t\t50.0\t30.0\tP\tPPL\tUA\t\t\t\t\t\t900\t",
            "broken line"]
    with zipfile.ZipFile(tmp_path / "cities15000.zip", "w") as z:
        z.writestr("cities15000.txt", "\n".join(rows))
    assert reference.load_cities(tmp_path) == [(50.45, 30.52, "Kyiv", 2900000)]
    assert reference.load_cities(tmp_path / "missing") == []
    assert reference.urban_radius_km(15000) == 2.0
    assert 14 < reference.urban_radius_km(2_900_000) < 16
