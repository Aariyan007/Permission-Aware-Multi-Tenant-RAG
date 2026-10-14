from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field

from app.auth import get_pool
from app.db import Principal, tenant_tx, vec_literal
from app.observability import CHUNKS_RETRIEVED, LLM_TOKENS, REQUESTS, SEARCH_MODE, stage
from app.ratelimit import enforce_rate_limit

router = APIRouter(tags=["query"])

# Note what is NOT here: any WHERE on tenant or ACL. Row-Level Security adds it.
SEARCH_SQL = """
WITH top AS (
    SELECT id, document_id, content, embedding <=> $1::vector AS dist
    FROM chunks
    ORDER BY embedding <=> $1::vector
    LIMIT $2
)
SELECT t.id, t.document_id, d.title, t.content, t.dist
FROM top t JOIN documents d ON d.id = t.document_id
ORDER BY t.dist
"""


async def search(c, vec: str, k: int, exact_max: int) -> list:
    """Pick the retrieval strategy by tenant size.

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
    rows = await c.fetch(SEARCH_SQL, vec, k)
    if len(rows) < k:
        SEARCH_MODE.labels("exact_fallback").inc()
        return await _exact(c, vec, k)
    SEARCH_MODE.labels("ann").inc()
    return rows


async def _exact(c, vec: str, k: int) -> list:
    await c.execute("SET LOCAL enable_indexscan = off")  # HNSW is an index scan: planner now scans exactly
    try:
        return await c.fetch(SEARCH_SQL, vec, k)
    finally:
        await c.execute("RESET enable_indexscan")


class QueryIn(BaseModel):
    question: str = Field(min_length=1, max_length=2000)
    top_k: int = Field(5, ge=1, le=20)


@router.post("/query")
async def query(body: QueryIn, request: Request, p: Principal = Depends(enforce_rate_limit), pool=Depends(get_pool)):
    tid = str(p.tenant_id)
    REQUESTS.labels("query", tid).inc()
    with stage("embed", tid):
        [qvec] = await request.app.state.embedder.embed([body.question])
    async with tenant_tx(pool, p) as c:
        with stage("retrieve", tid) as span:
            rows = await search(c, vec_literal(qvec), body.top_k, request.app.state.settings.exact_search_max_chunks)
            span.set_attribute("chunks.count", len(rows))
        CHUNKS_RETRIEVED.observe(len(rows))
        await c.execute(
            "INSERT INTO audit_log (tenant_id, user_id, action, query, chunk_ids) "
            "VALUES (app_tenant(), app_user(), 'query', $1, $2)", body.question, [r["id"] for r in rows])
    with stage("generate", tid):
        answer = await request.app.state.generator.generate(body.question, [r["content"] for r in rows])
    LLM_TOKENS.labels(tid, "input").inc(answer.input_tokens)
    LLM_TOKENS.labels(tid, "output").inc(answer.output_tokens)
    return {
        "answer": answer.text,
        "sources": [
            {"n": i, "chunk_id": r["id"], "document_id": r["document_id"], "title": r["title"],
             "snippet": r["content"][:300], "distance": round(float(r["dist"]), 4)}
            for i, r in enumerate(rows, 1)
        ],
    }
