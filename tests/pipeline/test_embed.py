"""OnnxEmbedder: cache, graceful fallback, verified download; one optional real-model test."""

import hashlib
import logging
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import numpy as np
import pytest
import respx

from osint.config import ROOT
from osint.pipeline import embed as E

NOW = datetime(2026, 11, 23, 12, 0, tzinfo=UTC)


def test_cache_roundtrip_and_prune(tmp_path):
    c = E.EmbeddingCache(tmp_path / "e.sqlite")
    v = E.normalise(np.arange(1, 5, dtype=np.float32))
    c.put_many("m", {"a": v}, NOW - timedelta(days=20))
    c.put_many("m", {"b": v}, NOW)
    got = c.get_many("m", ["a", "b", "c"])
    assert set(got) == {"a", "b"} and np.allclose(got["a"], v, atol=1e-3)
    assert c.get_many("other-model", ["a"]) == {}
    assert c.prune(NOW - timedelta(days=14)) == 1 and c.count() == 1
    c.close()


def _embedder(tmp_path, **kw):
    cfg = E.EmbeddingConfig(download=False, **kw)
    return E.OnnxEmbedder(cfg, tmp_path / "models", E.EmbeddingCache(tmp_path / "e.sqlite"),
                          clock=lambda: NOW)


def test_missing_model_falls_back_and_logs_once(tmp_path, caplog):
    emb = _embedder(tmp_path)
    with caplog.at_level(logging.WARNING, logger="osint.pipeline.embed"):
        assert emb.embed(["a b c d"], ["k"]) is None
        assert emb.embed(["a b c d"], ["k"]) is None  # no retry before retry_after_s
    assert sum("unavailable" in r.message for r in caplog.records) == 1
    assert not emb.ready


def test_cached_documents_are_not_embedded_again(tmp_path, monkeypatch):
    emb = _embedder(tmp_path)
    seen: list[list[str]] = []

    def encode(texts):
        seen.append(list(texts))
        return np.stack([E.normalise(np.array([len(t), 1, 0], dtype=np.float32))
                         for t in texts])

    monkeypatch.setattr(emb, "_load", lambda: True)
    monkeypatch.setattr(emb, "_encode", encode)
    a = emb.embed(["short", "a longer text"], ["d1", "d2"])
    b = emb.embed(["short", "a longer text", "new"], ["d1", "d2", "d3"])
    assert seen == [["short", "a longer text"], ["new"]]  # length-sorted, cache hits skipped
    assert np.allclose(a[1], b[1], atol=1e-3)
    assert emb.stats["embedded"] == 3 and emb.stats["cached"] == 2


def test_ensure_model_verifies_checksums(tmp_path):
    body = b"onnx-bytes"
    good = hashlib.sha256(body).hexdigest()
    spec = E.ModelSpec(files={"model.onnx": ("org/m/resolve/r1/onnx/m.onnx", good)})
    bad = E.ModelSpec(files={"model.onnx": ("org/m/resolve/r1/onnx/m.onnx", "0" * 64)})
    with respx.mock:
        respx.get("https://huggingface.co/org/m/resolve/r1/onnx/m.onnx").mock(
            return_value=httpx.Response(200, content=body))
        with pytest.raises(ValueError):
            E.ensure_model(bad, tmp_path / "bad")
        assert not any((tmp_path / "bad").iterdir())  # no partial file left behind
        d = E.ensure_model(spec, tmp_path / "ok")
    assert (d / "model.onnx").read_bytes() == body
    assert E.ensure_model(spec, tmp_path / "ok", download=False) == d  # present: no fetch
    with pytest.raises(FileNotFoundError):
        E.ensure_model(spec, tmp_path / "none", download=False)


def test_build_embedder_switches(tmp_path, monkeypatch):
    assert E.build_embedder(E.EmbeddingConfig(), tmp_path) is None  # OSINT_EMBEDDING=0
    monkeypatch.delenv("OSINT_EMBEDDING")
    assert E.build_embedder(E.EmbeddingConfig(enabled=False), tmp_path) is None
    emb = E.build_embedder(E.EmbeddingConfig(), tmp_path)
    assert isinstance(emb, E.OnnxEmbedder) and not emb.ready  # lazy: nothing loaded yet
    emb.cache.close()
    with pytest.raises(ValueError):
        E.OnnxEmbedder(E.EmbeddingConfig(model="nope"), tmp_path)


# ----------------------------------------------------------- real model (optional)

MODEL = "paraphrase-multilingual-minilm-l12-v2"
MODELS_DIR = Path(os.environ.get("OSINT_TEST_MODELS_DIR") or ROOT / "data" / "models")


@pytest.mark.slow
@pytest.mark.skipif(not (MODELS_DIR / MODEL / "model.onnx").is_file(),
                    reason="embedding model not downloaded (data/models or "
                           "OSINT_TEST_MODELS_DIR)")
def test_real_model_groups_translations_not_neighbours(tmp_path):
    emb = E.OnnxEmbedder(E.EmbeddingConfig(model=MODEL, download=False), MODELS_DIR)
    assert emb._load()
    # SentencePiece + fairseq offset == the Hugging Face tokenizer.json ids (XLM-R)
    ids, mask = emb.tokenize(["Drone attack on a Russian ship in Benghazi",
                              "سفينة روسية تنفجر في بنغازي"])
    assert ids[0].tolist() == [0, 46193, 86, 52875, 98, 10, 102374, 109923, 23, 99390, 528,
                               708, 2]
    assert ids[1].tolist() == [0, 89626, 67020, 43775, 648, 8844, 153753, 240, 2567, 94411,
                               179, 2, 1]
    assert mask[1].tolist() == [1] * 12 + [0]
    v = emb.embed([
        "Suspected drone attack caused explosion on Russian ship at Benghazi port",
        "سفينة روسية تنفجر في بنغازي بعد هجوم بطائرة مسيرة",
        "На российском военном корабле, стоявшем в Бенгази, произошли взрывы",
        "Russian missile strike on Kyiv kills three, damages power plant",
        "Clashes break out at Kyiv Pechersk Lavra",
    ])
    sim = np.array(v) @ np.array(v).T
    # the same incident in three languages vs. other events (calibration: 0.75 joins)
    assert min(sim[0, 1], sim[0, 2], sim[1, 2]) > 0.65
    assert max(sim[0, 3], sim[1, 3], sim[2, 3], sim[3, 4]) < 0.5
