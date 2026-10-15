from typing import Protocol

import httpx

from app.config import Settings


class Reranker(Protocol):
    async def rerank(self, question: str, passages: list[str], top_k: int) -> list[int]:
        """Return indexes into `passages`, best first, at most top_k long."""
        ...


class NoopReranker:
    """No model available: keep the fused order. Keeps the stack runnable without API keys."""

    async def rerank(self, question: str, passages: list[str], top_k: int) -> list[int]:
        return list(range(min(top_k, len(passages))))


class VoyageReranker:
    """Cross-encoder reranker: reads question and passage *together*, so it judges relevance far
    better than comparing two separate embeddings. Too slow for thousands of chunks, so it only
    re-orders the shortlist produced by the fast retrievers."""

    def __init__(self, api_key: str, model: str) -> None:
        self.api_key, self.model = api_key, model

    async def rerank(self, question: str, passages: list[str], top_k: int) -> list[int]:
        if not passages:
            return []
        async with httpx.AsyncClient(timeout=30) as client:
            r = await client.post(
                "https://api.voyageai.com/v1/rerank",
                headers={"Authorization": f"Bearer {self.api_key}"},
                json={"query": question, "documents": passages, "model": self.model, "top_k": top_k},
            )
            r.raise_for_status()
        return [d["index"] for d in r.json()["data"]]


def build_reranker(s: Settings) -> Reranker:
    if s.voyage_api_key and s.rerank_enabled:
        return VoyageReranker(s.voyage_api_key, s.rerank_model)
    return NoopReranker()
