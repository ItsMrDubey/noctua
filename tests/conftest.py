import pytest


@pytest.fixture(autouse=True)
def _no_embedding_model(monkeypatch):
    """Builders made from settings never load (or download) the sentence encoder in tests;
    tests that need embeddings pass a fake embedder explicitly."""
    monkeypatch.setenv("OSINT_EMBEDDING", "0")
