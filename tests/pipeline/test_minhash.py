import random

import pytest

from osint.pipeline.minhash import (
    LSHIndex,
    MinHasher,
    exact_jaccard,
    jaccard,
    normalise,
    shingles,
)

BASE = ("Russian drone strike on Kharkiv apartment block kills three, regional governor "
        "says, as air defences repel a second wave overnight")


def test_normalise_and_shingles():
    assert normalise("  Hello,  WORLD!! ") == "hello world"
    assert shingles("abc") == {"abc"}
    assert shingles("") == set()
    assert len(shingles("abcdefg", 5)) == 3


@pytest.mark.parametrize("method", ["oph", "classic"])
def test_signature_similarity_sanity(method):
    mh = MinHasher(128, method=method)
    a = mh.signature(BASE)
    assert jaccard(a, mh.signature(BASE)) == 1.0
    assert jaccard(a, mh.signature(BASE.upper() + "!")) == 1.0  # normalisation
    near = BASE.replace("three", "four")
    true_j = exact_jaccard(shingles(BASE), shingles(near))
    assert abs(jaccard(a, mh.signature(near)) - true_j) < 0.15
    other = mh.signature("Central bank raises interest rates to curb inflation in Brazil")
    assert jaccard(a, other) < 0.15
    assert len(a) == 128 and all(0 <= x < 2**32 for x in a)


def test_signature_is_deterministic_across_instances_and_scripts():
    t = "قصف روسي على خاركيف يقتل ثلاثة أشخاص"
    assert MinHasher(64).signature(t) == MinHasher(64).signature(t)
    assert MinHasher(64).signature("") == MinHasher(64).signature("")


def test_minhasher_validates():
    with pytest.raises(ValueError):
        MinHasher(8)
    with pytest.raises(ValueError):
        MinHasher(64, method="nope")
    with pytest.raises(ValueError):
        LSHIndex(100, bands=32)
    with pytest.raises(ValueError):
        jaccard((1, 2), (1,))


def test_lsh_candidate_recall_and_precision():
    rng = random.Random(7)
    mh = MinHasher(128)
    lsh = LSHIndex(128, bands=32)
    words = [f"w{i}" for i in range(2000)]
    docs = {i: " ".join(rng.choice(words) for _ in range(40)) for i in range(200)}
    for i, text in docs.items():
        lsh.insert(i, mh.signature(text))
    assert len(lsh) == 200
    hits = 0
    for i, text in docs.items():  # near-duplicates: replace 2 of 40 words
        toks = text.split()
        toks[3], toks[20] = "changed", "edited"
        if i in lsh.query(mh.signature(" ".join(toks))):
            hits += 1
    assert hits / len(docs) >= 0.95
    unrelated = mh.signature(" ".join(rng.choice(words) for _ in range(40)))
    assert len(lsh.query(unrelated)) <= 5


def test_lsh_remove_and_multiple_signatures():
    mh = MinHasher(64)
    lsh = LSHIndex(64, bands=16)
    s1, s2 = mh.signature(BASE), mh.signature("completely different text about ports")
    lsh.insert("e1", s1)
    lsh.insert("e1", s2)
    assert "e1" in lsh.query(s1) and "e1" in lsh.query(s2)
    lsh.remove("e1")
    assert "e1" not in lsh
    assert lsh.query(s1) == set() and lsh.query(s2) == set()
    lsh.remove("missing")  # no error
