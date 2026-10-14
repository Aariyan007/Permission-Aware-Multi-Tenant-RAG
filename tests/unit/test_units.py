import jwt
import pytest

from app.auth import hash_password, issue_token, verify_password
from app.config import Settings
from app.rag.chunker import chunk_text
from app.rag.embedder import HashEmbedder
from app.rag.generator import build_prompt


def test_chunker_packs_paragraphs_and_respects_limit():
    text = "\n\n".join(" ".join(f"w{i}_{j}" for j in range(40)) for i in range(10))
    chunks = chunk_text(text, max_words=100, overlap=10)
    assert len(chunks) > 1 and all(len(c.split()) <= 100 for c in chunks)


def test_chunker_splits_giant_paragraph_with_overlap():
    words = [f"w{i}" for i in range(500)]
    chunks = chunk_text(" ".join(words), max_words=100, overlap=20)
    assert chunks[0].split()[-20:] == chunks[1].split()[:20]
    assert set(" ".join(chunks).split()) == set(words)


def test_chunker_empty_and_bad_args():
    assert chunk_text("  \n\n ") == []
    with pytest.raises(ValueError):
        chunk_text("x", max_words=10, overlap=10)


async def test_hash_embedder_is_deterministic_and_normalized():
    e = HashEmbedder(256)
    [a], [b] = await e.embed(["hello world"]), await e.embed(["hello world"])
    assert a == b and abs(sum(x * x for x in a) - 1) < 1e-6


async def test_similar_text_is_closer_than_unrelated():
    e = HashEmbedder(256)
    q, near, far = await e.embed(["refund policy for orders", "our refund policy covers orders", "quarterly sales numbers"])
    dot = lambda x, y: sum(i * j for i, j in zip(x, y))
    assert dot(q, near) > dot(q, far)


def test_password_hash_roundtrip():
    h = hash_password("correct horse")
    assert verify_password("correct horse", h) and not verify_password("wrong", h)
    assert h != hash_password("correct horse")  # salted


def test_jwt_carries_tenant_and_user():
    from uuid import uuid4
    s, u, t = Settings(jwt_secret="x" * 32), uuid4(), uuid4()
    c = jwt.decode(issue_token(s, u, t, "member"), "x" * 32, algorithms=["HS256"])
    assert c["sub"] == str(u) and c["tid"] == str(t) and c["role"] == "member"


def test_prompt_numbers_sources_and_marks_them_as_data():
    p = build_prompt("q?", ["alpha", "beta"])
    assert "[1] alpha" in p and "[2] beta" in p and p.endswith("Question: q?")
