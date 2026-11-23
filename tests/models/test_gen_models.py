"""Structural validation of the procedurally generated .glb models."""
import importlib.util
import json
import struct
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
_spec = importlib.util.spec_from_file_location("gen_models", ROOT / "scripts" / "gen_models.py")
gen = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(gen)

COMP = {5126: ("f", 4), 5123: ("H", 2), 5125: ("I", 4)}
NCOMP = {"SCALAR": 1, "VEC3": 3}


def parse_glb(data: bytes):
    magic, version, length = struct.unpack_from("<4sII", data, 0)
    assert magic == b"glTF" and version == 2 and length == len(data)
    jlen, jtype = struct.unpack_from("<I4s", data, 12)
    assert jtype == b"JSON" and jlen % 4 == 0
    doc = json.loads(data[20:20 + jlen])
    off = 20 + jlen
    blen, btype = struct.unpack_from("<I4s", data, off)
    assert btype == b"BIN\x00" and blen % 4 == 0 and off + 8 + blen == len(data)
    return doc, data[off + 8:off + 8 + blen]


def read_acc(doc, binc, i):
    a = doc["accessors"][i]
    bv = doc["bufferViews"][a["bufferView"]]
    fmt, size = COMP[a["componentType"]]
    n = NCOMP[a["type"]]
    assert bv["byteOffset"] % 4 == 0
    assert a["count"] * n * size == bv["byteLength"]
    assert bv["byteOffset"] + bv["byteLength"] <= len(binc)
    vals = struct.unpack_from(f"<{a['count'] * n}{fmt}", binc, bv["byteOffset"])
    return a, [vals[k:k + n] for k in range(0, len(vals), n)]


@pytest.fixture(scope="module")
def outdir(tmp_path_factory):
    d = tmp_path_factory.mktemp("models")
    gen.generate(d)
    return d


@pytest.mark.parametrize("name", list(gen.MODELS))
def test_glb_valid(outdir, name):
    doc, binc = parse_glb((outdir / (name + ".glb")).read_bytes())
    assert doc["asset"]["version"] == "2.0"
    assert doc["buffers"][0]["byteLength"] == len(binc)
    assert len(doc["meshes"]) == 1
    tris = 0
    prims = doc["meshes"][0]["primitives"]
    assert 1 <= len(prims) <= 5
    for p in prims:
        pa, pos = read_acc(doc, binc, p["attributes"]["POSITION"])
        _, nrm = read_acc(doc, binc, p["attributes"]["NORMAL"])
        ia, idx = read_acc(doc, binc, p["indices"])
        assert len(nrm) == len(pos)
        assert all(0 <= v[0] < len(pos) for v in idx)
        assert ia["count"] % 3 == 0
        for k in range(3):
            assert pa["min"][k] == min(v[k] for v in pos)
            assert pa["max"][k] == max(v[k] for v in pos)
        for n in nrm:
            assert abs(sum(c * c for c in n) - 1) < 1e-3
        m = doc["materials"][p["material"]]
        assert len(m["pbrMetallicRoughness"]["baseColorFactor"]) == 4
        tris += ia["count"] // 3
    assert 120 <= tris <= 1500, tris


@pytest.mark.parametrize("name", list(gen.MODELS))
def test_scale_and_forward_axis(outdir, name):
    """Longest extent is along glTF +Z (forward) for fixed-wing/ships and matches length_m."""
    doc, _binc = parse_glb((outdir / (name + ".glb")).read_bytes())
    mins = [min(doc["accessors"][p["attributes"]["POSITION"]]["min"][k] for p in doc["meshes"][0]["primitives"]) for k in range(3)]
    maxs = [max(doc["accessors"][p["attributes"]["POSITION"]]["max"][k] for p in doc["meshes"][0]["primitives"]) for k in range(3)]
    length = maxs[2] - mins[2]
    want = gen.MODELS[name][1]
    assert abs(length - want) / want < 0.12, (length, want)
    if name.startswith("ship_"):
        assert length > maxs[0] - mins[0]  # bow-to-stern longer than beam
        assert maxs[1] > 0 > mins[1]  # waterline origin (glTF Y up)


def test_manifest(outdir):
    m = json.loads((outdir / "manifest.json").read_text())
    assert set(m["models"]) == set(gen.MODELS)
    for v in m["models"].values():
        assert (outdir / v["file"]).exists()


def test_nose_points_forward():
    """Fighter nose tip (thinnest station) is at max glTF Z."""
    data, _ = gen.build_glb("t", gen.m_fighter())
    doc, binc = parse_glb(data)
    _, pos = read_acc(doc, binc, doc["meshes"][0]["primitives"][0]["attributes"]["POSITION"])
    zmax = max(p[2] for p in pos)
    tip = [p for p in pos if p[2] > zmax - 0.01]
    assert all(abs(p[0]) < 0.2 for p in tip)  # centred laterally


def test_committed_models_current():
    """frontend/models matches the generator output (regenerate with scripts/gen_models.py)."""
    d = ROOT / "frontend" / "models"
    for name, (fn, _) in gen.MODELS.items():
        data, _ = gen.build_glb(name, fn())
        assert (d / (name + ".glb")).read_bytes() == data, name
