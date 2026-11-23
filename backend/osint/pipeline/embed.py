"""Multilingual sentence embeddings for cross-language event clustering (spec §2.6).

``OnnxEmbedder`` runs a small multilingual sentence encoder on the local CPU with ONNX
Runtime and the model's SentencePiece tokenizer: no PyTorch, no GPU, no paid API. The
default model is ``paraphrase-multilingual-MiniLM-L12-v2`` (50+ languages, 384 dimensions,
int8-quantised ONNX export, 118 MB), trained to map translations of a sentence onto the same
vector, which is exactly "the same headline in Arabic, Russian and English". (The spec's
multilingual-e5-small is also available as ``multilingual-e5-small``; on our headlines its
cosine similarities are compressed into 0.8-0.95 for related *and* unrelated pairs and it
is less precise at every recall level, see ``config/events.yaml``.)

Both are XLM-R models: ``sentencepiece.bpe.model`` plus the fairseq id offset gives the same
token ids as the Hugging Face ``tokenizer.json`` on all 1985 calibration texts, for 39 MB of
memory instead of 250 MB.

* **Lazy, verified download**: the model files are fetched on first use from a pinned
  Hugging Face revision into ``data/models/<name>/`` and checked against their SHA-256.
* **Bounded cost**: ``threads`` intra-op threads (default 2 of the host's cores), batches of
  ``batch`` texts sorted by length (little padding), texts truncated to ``max_tokens``, no
  ONNX Runtime memory arena (about 180 MB resident in all, flat over time).
* **Cache**: vectors are stored per document id (float16) in ``data/embeddings.sqlite``, so
  builder passes, restarts and rebuilds never embed a document twice; old rows are pruned.
* **Graceful fallback**: when the runtime packages or the model are unavailable the
  embedder logs once and returns None (the clusterer then skips its semantic path, exactly
  as with ``NullEmbedder``); loading is retried after ``retry_after_s``.

Vectors are L2-normalised float32 numpy arrays (mean pooling over the attention mask).
"""

from __future__ import annotations

import hashlib
import logging
import os
import sqlite3
import threading
import time
from collections.abc import Sequence
from dataclasses import dataclass, fields
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import numpy as np

log = logging.getLogger(__name__)

_HF = "https://huggingface.co/{path}"
# XLM-R vocabulary: <s>=0 <pad>=1 </s>=2 <unk>=3, SentencePiece ids shifted by one (fairseq)
BOS, PAD, EOS, UNK = 0, 1, 2, 3
_SPM = ("sentencepiece.bpe.model",
        "cfc8146abe2a0488e9e2a0c56de7952f7c11ab059eca145a0a727afce0db2865")


@dataclass(frozen=True)
class ModelSpec:
    """A pinned ONNX sentence encoder: local file name -> (Hugging Face path including the
    revision, sha256). Needs ``model.onnx`` and an XLM-R ``sentencepiece.bpe.model``."""

    files: dict[str, tuple[str, str]]
    prefix: str = ""  # e5 models expect "query: " in front of every text
    dim: int = 384


def _hf(repo: str, revision: str, path: str) -> str:
    return f"{repo}/resolve/{revision}/{path}"


_MINILM = ("sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2",
           "e8f8c211226b894fcb81acc59f3b34ba3efd5f42")
MODELS: dict[str, ModelSpec] = {
    # apache-2.0; quint8 (AVX2) export from the sentence-transformers repo itself
    "paraphrase-multilingual-minilm-l12-v2": ModelSpec(files={
        "model.onnx": (_hf(*_MINILM, "onnx/model_quint8_avx2.onnx"),
                       "98a01d88b7de996cdea58c32ca71208c09968d143798814b2ea09d3439dc334f"),
        _SPM[0]: (_hf(*_MINILM, _SPM[0]), _SPM[1]),
    }),
    # MIT; int8 export (Xenova), the same XLM-R SentencePiece model from the intfloat repo
    "multilingual-e5-small": ModelSpec(files={
        "model.onnx": (_hf("Xenova/multilingual-e5-small",
                           "761b726dd34fb83930e26aab4e9ac3899aa1fa78", "onnx/model_quantized.onnx"),
                       "f80102d3f2a1229f387d3c81909990d8945513e347b0eab049f7de3c6f98c193"),
        _SPM[0]: (_hf("intfloat/multilingual-e5-small", "614241f622f53c4eeff9890bdc4f31cfecc418b3",
                      f"onnx/{_SPM[0]}"), _SPM[1]),
    }, prefix="query: "),
}


@dataclass
class EmbeddingConfig:
    """``embedding:`` block of config/events.yaml."""

    enabled: bool = True
    model: str = "paraphrase-multilingual-minilm-l12-v2"
    threads: int = 2
    batch: int = 32
    max_tokens: int = 128
    download: bool = True
    cache_days: float = 14.0
    retry_after_s: float = 3600.0

    @classmethod
    def from_dict(cls, d: dict[str, Any] | None) -> EmbeddingConfig:
        known = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in (d or {}).items() if k in known})


# ---------------------------------------------------------------------- vectors


def pack(vec: np.ndarray) -> bytes:
    return np.asarray(vec, dtype=np.float16).tobytes()


def unpack(blob: bytes) -> np.ndarray:
    return np.frombuffer(blob, dtype=np.float16).astype(np.float32)


def normalise(vec: np.ndarray) -> np.ndarray:
    n = float(np.linalg.norm(vec))
    return vec / n if n else vec


# ------------------------------------------------------------------------ cache


class EmbeddingCache:
    """doc id -> vector (float16), one table per file, keyed by (doc id, model)."""

    SCHEMA = """
    CREATE TABLE IF NOT EXISTS embeddings (
      doc_id TEXT NOT NULL, model TEXT NOT NULL, vec BLOB NOT NULL, created_at TEXT NOT NULL,
      PRIMARY KEY (doc_id, model));
    CREATE INDEX IF NOT EXISTS embeddings_created ON embeddings(created_at);
    """

    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self._lock = threading.Lock()
        self._db = sqlite3.connect(path, timeout=30.0, check_same_thread=False)
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA synchronous=NORMAL")
        self._db.executescript(self.SCHEMA)

    def get_many(self, model: str, keys: Sequence[str]) -> dict[str, np.ndarray]:
        out: dict[str, np.ndarray] = {}
        uniq = list(dict.fromkeys(keys))
        for i in range(0, len(uniq), 900):
            chunk = uniq[i:i + 900]
            with self._lock:
                rows = self._db.execute(
                    f"SELECT doc_id, vec FROM embeddings WHERE model = ? AND doc_id IN "
                    f"({','.join('?' * len(chunk))})", (model, *chunk)).fetchall()
            out.update({r[0]: unpack(r[1]) for r in rows})
        return out

    def put_many(self, model: str, items: dict[str, np.ndarray], now: datetime) -> None:
        if not items:
            return
        ts = now.astimezone(UTC).isoformat(timespec="seconds")
        with self._lock, self._db:
            self._db.executemany(
                "INSERT OR REPLACE INTO embeddings VALUES (?,?,?,?)",
                [(k, model, pack(v), ts) for k, v in items.items()])

    def prune(self, older_than: datetime) -> int:
        ts = older_than.astimezone(UTC).isoformat(timespec="seconds")
        with self._lock, self._db:
            return self._db.execute("DELETE FROM embeddings WHERE created_at < ?",
                                    (ts,)).rowcount

    def count(self) -> int:
        with self._lock:
            return int(self._db.execute("SELECT COUNT(*) FROM embeddings").fetchone()[0])

    def close(self) -> None:
        with self._lock:
            self._db.close()


# ------------------------------------------------------------------------ model


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        while chunk := f.read(1 << 20):
            h.update(chunk)
    return h.hexdigest()


def ensure_model(spec: ModelSpec, model_dir: Path, *, download: bool = True,
                 timeout: float = 600.0) -> Path:
    """The model's directory with every file present and verified; downloads missing files
    from the pinned revision (``download``). Raises on a missing file or a bad checksum."""
    model_dir.mkdir(parents=True, exist_ok=True)
    for name, (remote, sha) in spec.files.items():
        dest = model_dir / name
        if dest.is_file():
            continue
        if not download:
            raise FileNotFoundError(f"{dest} missing (download disabled)")
        import httpx  # local: only needed on the first run

        url = _HF.format(path=remote)
        tmp = dest.with_name(f".{name}.{os.getpid()}.part")
        log.info("embedding model: downloading %s", url)
        h = hashlib.sha256()
        try:
            with httpx.stream("GET", url, follow_redirects=True, timeout=timeout) as r:
                r.raise_for_status()
                with tmp.open("wb") as f:
                    for chunk in r.iter_bytes(1 << 20):
                        f.write(chunk)
                        h.update(chunk)
            if h.hexdigest() != sha:
                raise ValueError(f"{url}: sha256 {h.hexdigest()} != pinned {sha}")
            os.replace(tmp, dest)
        finally:
            tmp.unlink(missing_ok=True)
    return model_dir


class OnnxEmbedder:
    """``Embedder`` (pipeline.cluster) backed by an ONNX sentence encoder; see module doc."""

    def __init__(self, cfg: EmbeddingConfig, models_dir: Path,
                 cache: EmbeddingCache | None = None, *,
                 clock=lambda: datetime.now(UTC)) -> None:
        if cfg.model not in MODELS:
            raise ValueError(f"unknown embedding model {cfg.model!r}; known: {sorted(MODELS)}")
        self.cfg = cfg
        self.spec = MODELS[cfg.model]
        self.name = cfg.model
        self.model_dir = models_dir / cfg.model
        self.cache = cache
        self.clock = clock
        self._lock = threading.Lock()
        self._session: Any = None
        self._tokenizer: Any = None
        self._inputs: set[str] = set()
        self._failed_at: float | None = None
        self._warned = False
        self._last_prune: datetime | None = None
        self.stats = {"embedded": 0, "cached": 0, "seconds": 0.0}

    # ------------------------------------------------------------------ loading

    @property
    def ready(self) -> bool:
        return self._session is not None

    def _load(self) -> bool:
        if self._session is not None:
            return True
        if self._failed_at is not None and \
                time.monotonic() - self._failed_at < self.cfg.retry_after_s:
            return False
        try:
            import onnxruntime as ort
            import sentencepiece

            path = ensure_model(self.spec, self.model_dir, download=self.cfg.download)
            so = ort.SessionOptions()
            so.intra_op_num_threads = max(1, int(self.cfg.threads))
            so.inter_op_num_threads = 1
            so.execution_mode = ort.ExecutionMode.ORT_SEQUENTIAL
            so.enable_cpu_mem_arena = False  # resident memory stays flat (+30% CPU time)
            t0 = time.perf_counter()
            session = ort.InferenceSession(str(path / "model.onnx"), so,
                                           providers=["CPUExecutionProvider"])
            tok = sentencepiece.SentencePieceProcessor(model_file=str(path / _SPM[0]))
        except Exception as e:  # noqa: BLE001 — missing package, offline, bad checksum
            self._failed_at = time.monotonic()
            if not self._warned:
                log.warning("embedding model %s unavailable (%s: %s); cross-language "
                            "clustering disabled, retry in %.0f s", self.name,
                            type(e).__name__, e, self.cfg.retry_after_s)
                self._warned = True
            return False
        self._session, self._tokenizer = session, tok
        self._inputs = {i.name for i in session.get_inputs()}
        self._failed_at = None
        log.info("embedding model %s loaded in %.1f s (%d threads)", self.name,
                 time.perf_counter() - t0, so.intra_op_num_threads)
        return True

    # --------------------------------------------------------------- embedding

    def tokenize(self, texts: list[str]) -> tuple[np.ndarray, np.ndarray]:
        """XLM-R input ids and attention mask: <s> pieces </s>, truncated to
        ``max_tokens``, padded to the longest text of the batch."""
        n = max(2, int(self.cfg.max_tokens)) - 2
        rows = [[BOS, *[i + 1 if i else UNK for i in pieces[:n]], EOS]
                for pieces in self._tokenizer.encode([self.spec.prefix + t for t in texts])]
        width = max(len(r) for r in rows)
        ids = np.full((len(rows), width), PAD, dtype=np.int64)
        mask = np.zeros((len(rows), width), dtype=np.int64)
        for k, r in enumerate(rows):
            ids[k, :len(r)] = r
            mask[k, :len(r)] = 1
        return ids, mask

    def _encode(self, texts: list[str]) -> np.ndarray:
        ids, mask = self.tokenize(texts)
        feed = {"input_ids": ids, "attention_mask": mask}
        if "token_type_ids" in self._inputs:
            feed["token_type_ids"] = np.zeros_like(ids)
        hidden = self._session.run(None, feed)[0]
        m = mask[..., None].astype(np.float32)
        vecs = (hidden * m).sum(axis=1) / np.maximum(m.sum(axis=1), 1e-9)
        norms = np.linalg.norm(vecs, axis=1, keepdims=True)
        return (vecs / np.maximum(norms, 1e-9)).astype(np.float32)

    def _embed_texts(self, texts: list[str]) -> list[np.ndarray]:
        # length-sorted batches: padding to the longest text of a batch costs CPU
        order = sorted(range(len(texts)), key=lambda i: len(texts[i]))
        out: list[np.ndarray | None] = [None] * len(texts)
        bs = max(1, int(self.cfg.batch))
        for s in range(0, len(order), bs):
            idx = order[s:s + bs]
            for i, v in zip(idx, self._encode([texts[i] for i in idx]), strict=True):
                out[i] = v
        return out  # type: ignore[return-value]

    def embed(self, texts: list[str], keys: list[str] | None = None
              ) -> list[np.ndarray] | None:
        """One normalised vector per text, or None when the model is unavailable. ``keys``
        (document ids, same order) use and fill the cache."""
        if not texts:
            return []
        with self._lock:
            t0 = time.perf_counter()
            cached: dict[str, np.ndarray] = {}
            if keys is not None and self.cache is not None:
                cached = self.cache.get_many(self.name, keys)
            todo = [i for i in range(len(texts))
                    if keys is None or keys[i] not in cached]
            if todo and not self._load():
                return None
            fresh = self._embed_texts([texts[i] for i in todo]) if todo else []
            out: list[np.ndarray] = [cached[keys[i]] if keys is not None and keys[i] in cached
                                     else None for i in range(len(texts))]  # type: ignore[misc]
            for i, v in zip(todo, fresh, strict=True):
                out[i] = v
            if keys is not None and self.cache is not None and todo:
                now = self.clock()
                self.cache.put_many(self.name, {keys[i]: out[i] for i in todo}, now)
                if self._last_prune is None or now - self._last_prune > timedelta(hours=6):
                    self._last_prune = now
                    self.cache.prune(now - timedelta(days=self.cfg.cache_days))
            self.stats["embedded"] += len(todo)
            self.stats["cached"] += len(texts) - len(todo)
            self.stats["seconds"] += time.perf_counter() - t0
            return out


def build_embedder(cfg: EmbeddingConfig, data_dir: Path) -> Any:
    """The configured embedder for a builder: ``OnnxEmbedder`` (models in
    ``data_dir/models``, cache ``data_dir/embeddings.sqlite``) or None when disabled
    (config, or the environment variable ``OSINT_EMBEDDING=0``)."""
    if not cfg.enabled or os.environ.get("OSINT_EMBEDDING", "").strip().lower() in (
            "0", "off", "false", "no"):
        return None
    return OnnxEmbedder(cfg, data_dir / "models", EmbeddingCache(data_dir / "embeddings.sqlite"))

