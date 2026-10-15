from typing import Literal

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field

from app.auth import get_pool
from app.db import Principal, tenant_tx, vec_literal
from app.observability import CHUNKS_RETRIEVED, LLM_TOKENS, REQUESTS, SEARCH_MODE, stage
from app.rag.hybrid import reciprocal_rank_fusion
from app.ratelimit import enforce_rate_limit

router = APIRouter(tags=["query"])

# Note what is NOT in either query: any WHERE on tenant or ACL. Row-Level Security adds it to both.
VECTOR_SQL = """
WITH top AS (
    SELECT id, document_id, content, embedding <=> $1::vector AS dist
    FROM chunks
    ORDER BY embedding <=> $1::vector
    LIMIT $2
)
SELECT t.id, t.document_id, d.title, t.content, 1 - t.dist AS score
FROM top t JOIN documents d ON d.id = t.document_id
ORDER BY t.dist
"""

# plainto_tsquery ANDs every word ("salari" & "band"); swapping & for | means a chunk matching
# *some* of the words still qualifies, and ts_rank_cd ranks the ones matching more words higher.
KEYWORD_SQL = """
WITH q AS (
    SELECT replace(plainto_tsquery('english', $1)::text, ' & ', ' | ')::tsquery AS query
), top AS (
    SELECT c.id, c.document_id, c.content, ts_rank_cd(c.tsv, q.query) AS score
    FROM chunks c, q
    WHERE c.tsv @@ q.query
    ORDER BY score DESC, c.id
    LIMIT $2
)
SELECT t.id, t.document_id, d.title, t.content, t.score
FROM top t JOIN documents d ON d.id = t.document_id
ORDER BY t.score DESC, t.id
"""

Mode = Literal["vector", "keyword", "hybrid"]


async def vector_search(c, vec: str, k: int, exact_max: int) -> list:
    """Pick the vector strategy by tenant size.

    HNSW is a graph walk whose candidates are filtered by RLS afterwards. A small tenant sharing
    the index with a big one can lose recall (the walk never reaches its few chunks), silently.
    So tenants up to `exact_max` chunks are searched exactly (milliseconds at that size); only
    larger tenants use ANN, and if ANN under-returns we rerun exactly as a safety net.
    The explicit tenant predicate in the size probe is a planner hint; RLS still enforces it."""
    probe = await c.fetchval(
        "SELECT count(*) FROM (SELECT 1 FROM chunks WHERE tenant_id = app_tenant() LIMIT $1) s", exact_max + 1)
    if probe <= exact_max:
        SEARCH_MODE.labels("exact_small_tenant").inc()
        return await _exact(c, vec, k)
    rows = await c.fetch(VECTOR_SQL, vec, k)
    if len(rows) < k:
        SEARCH_MODE.labels("exact_fallback").inc()
        return await _exact(c, vec, k)
    SEARCH_MODE.labels("ann").inc()
    return rows


async def _exact(c, vec: str, k: int) -> list:
    await c.execute("SET LOCAL enable_indexscan = off")  # HNSW is an index scan: planner now scans exactly
    try:
        return await c.fetch(VECTOR_SQL, vec, k)
    finally:
        await c.execute("RESET enable_indexscan")


async def keyword_search(c, question: str, k: int) -> list:
    return await c.fetch(KEYWORD_SQL, question, k)


async def find_candidates(c, mode: Mode, question: str, vec: str, top_k: int, settings) -> list[dict]:
    """vector / keyword return their own top_k ("before"). hybrid asks both for a wider shortlist
    and fuses the two rankings by position ("after"); the reranker then trims it to top_k."""
    if mode == "vector":
        return [dict(r) for r in await vector_search(c, vec, top_k, settings.exact_search_max_chunks)]
    if mode == "keyword":
        return [dict(r) for r in await keyword_search(c, question, top_k)]
    n = max(settings.search_candidates, top_k)
    vec_rows = await vector_search(c, vec, n, settings.exact_search_max_chunks)
    kw_rows = await keyword_search(c, question, n)
    by_id = {r["id"]: dict(r) for r in [*kw_rows, *vec_rows]}
    fused = reciprocal_rank_fusion([[r["id"] for r in vec_rows], [r["id"] for r in kw_rows]])
    return [{**by_id[cid], "score": score} for cid, score in fused]


class QueryIn(BaseModel):
    question: str = Field(min_length=1, max_length=2000)
    top_k: int = Field(5, ge=1, le=20)
    mode: Mode = "hybrid"


@router.post("/query")
async def query(body: QueryIn, request: Request, p: Principal = Depends(enforce_rate_limit), pool=Depends(get_pool)):
    state, tid = request.app.state, str(p.tenant_id)
    REQUESTS.labels("query", tid).inc()
    with stage("embed", tid):
        [qvec] = await state.embedder.embed([body.question])
    async with tenant_tx(pool, p) as c:
        with stage("retrieve", tid) as span:
            rows = await find_candidates(c, body.mode, body.question, vec_literal(qvec), body.top_k, state.settings)
            span.set_attribute("search.mode", body.mode)
            span.set_attribute("chunks.candidates", len(rows))

    # Rerank outside the transaction: a network call must not hold a pooled DB connection open.
    if body.mode == "hybrid":
        with stage("rerank", tid):
            order = await state.reranker.rerank(body.question, [r["content"] for r in rows], body.top_k)
            rows = [rows[i] for i in order]
    rows = rows[: body.top_k]
    CHUNKS_RETRIEVED.observe(len(rows))

    async with tenant_tx(pool, p) as c:
        await c.execute(
            "INSERT INTO audit_log (tenant_id, user_id, action, query, chunk_ids) "
            "VALUES (app_tenant(), app_user(), 'query', $1, $2)", body.question, [r["id"] for r in rows])
    with stage("generate", tid):
        answer = await state.generator.generate(body.question, [r["content"] for r in rows])
    LLM_TOKENS.labels(tid, "input").inc(answer.input_tokens)
    LLM_TOKENS.labels(tid, "output").inc(answer.output_tokens)
    return {
        "answer": answer.text,
        "mode": body.mode,
        "sources": [
            {"n": i, "chunk_id": r["id"], "document_id": r["document_id"], "title": r["title"],
             "snippet": r["content"][:300], "score": round(float(r["score"]), 4)}
            for i, r in enumerate(rows, 1)
        ],
    }
