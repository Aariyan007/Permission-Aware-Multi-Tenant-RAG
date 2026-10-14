import hashlib
import math
import re
from typing import Protocol

import httpx

from app.config import Settings


class Embedder(Protocol):
    async def embed(self, texts: list[str]) -> list[list[float]]: ...


class HashEmbedder:
    """Deterministic bag-of-words hashing embedder. No network, no model download; similar
    texts share words so they land close in cosine space. Used for tests and offline demos."""

    def __init__(self, dim: int = 256) -> None:
        self.dim = dim

    async def embed(self, texts: list[str]) -> list[list[float]]:
        return [self._one(t) for t in texts]

    def _one(self, text: str) -> list[float]:
        v = [0.0] * self.dim
        for tok in re.findall(r"[a-z0-9_]+", text.lower()):
            h = int.from_bytes(hashlib.blake2b(tok.encode(), digest_size=8).digest(), "big")
            v[h % self.dim] += 1.0 if (h >> 63) & 1 else -1.0
        norm = math.sqrt(sum(x * x for x in v)) or 1.0
        return [x / norm for x in v]


class VoyageEmbedder:
    def __init__(self, api_key: str, model: str, dim: int) -> None:
        self.api_key, self.model, self.dim = api_key, model, dim

    async def embed(self, texts: list[str]) -> list[list[float]]:
        out: list[list[float]] = []
        async with httpx.AsyncClient(timeout=30) as client:
            for i in range(0, len(texts), 64):
                r = await client.post(
                    "https://api.voyageai.com/v1/embeddings",
                    headers={"Authorization": f"Bearer {self.api_key}"},
                    json={"input": texts[i : i + 64], "model": self.model, "output_dimension": self.dim},
                )
                r.raise_for_status()
                out += [d["embedding"] for d in r.json()["data"]]
        return out


def build_embedder(s: Settings) -> Embedder:
    if s.voyage_api_key:
        return VoyageEmbedder(s.voyage_api_key, s.voyage_model, s.embed_dim)
    return HashEmbedder(s.embed_dim)
