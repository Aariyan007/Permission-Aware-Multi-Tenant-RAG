import io
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from pypdf import PdfReader

from app.auth import current_principal, get_pool
from app.db import Principal, tenant_tx, vec_literal
from app.observability import REQUESTS, stage
from app.rag.chunker import chunk_text
from app.ratelimit import enforce_rate_limit

router = APIRouter(tags=["documents"])


def extract_text(filename: str, data: bytes) -> str:
    if filename.lower().endswith(".pdf"):
        return "\n\n".join((page.extract_text() or "") for page in PdfReader(io.BytesIO(data)).pages)
    return data.decode("utf-8", errors="replace")


@router.post("/documents", status_code=201)
async def ingest(
    request: Request,
    title: str = Form(...),
    visibility: Literal["tenant", "restricted"] = Form("tenant"),
    group_ids: str = Form(""),  # comma-separated group UUIDs
    text: str | None = Form(None),
    file: UploadFile | None = File(None),
    p: Principal = Depends(enforce_rate_limit),
    pool=Depends(get_pool),
):
    if p.role not in ("admin", "member"):
        raise HTTPException(403, "viewers cannot ingest")
    if file is not None:
        body = extract_text(file.filename or "", await file.read())
        source = file.filename
    elif text:
        body, source = text, None
    else:
        raise HTTPException(422, "provide `text` or `file`")
    try:
        groups = [UUID(g) for g in group_ids.split(",") if g.strip()]
    except ValueError:
        raise HTTPException(422, "group_ids must be comma-separated UUIDs") from None
    if visibility == "restricted" and not groups:
        raise HTTPException(422, "restricted documents need at least one group")

    tid = str(p.tenant_id)
    REQUESTS.labels("ingest", tid).inc()
    with stage("chunk", tid):
        chunks = chunk_text(body)
    if not chunks:
        raise HTTPException(422, "document has no text")
    with stage("embed", tid):
        vectors = await request.app.state.embedder.embed(chunks)

    restricted = visibility == "restricted"
    async with tenant_tx(pool, p) as c:
        with stage("store", tid):
            doc_id = await c.fetchval(
                "INSERT INTO documents (tenant_id, title, source, created_by, visibility) "
                "VALUES (app_tenant(), $1, $2, app_user(), $3) RETURNING id", title, source, visibility)
            for g in groups:  # RLS: members may only attach groups they belong to
                await c.execute(
                    "INSERT INTO document_acl (tenant_id, document_id, group_id) VALUES (app_tenant(), $1, $2)", doc_id, g)
            await c.executemany(
                "INSERT INTO chunks (tenant_id, document_id, owner_id, chunk_index, content, embedding, restricted, acl_group_ids) "
                "VALUES (app_tenant(), $1, app_user(), $2, $3, $4::vector, $5, $6)",
                [(doc_id, i, ch, vec_literal(v), restricted, groups) for i, (ch, v) in enumerate(zip(chunks, vectors))])
            await c.execute(
                "INSERT INTO audit_log (tenant_id, user_id, action, query) VALUES (app_tenant(), app_user(), 'ingest', $1)", title)
    return {"id": doc_id, "chunks": len(chunks)}


@router.get("/documents")
async def list_documents(p: Principal = Depends(current_principal), pool=Depends(get_pool)):
    async with tenant_tx(pool, p) as c:
        rows = await c.fetch("SELECT id, title, visibility, created_at FROM documents ORDER BY created_at DESC")
    return [dict(r) for r in rows]


@router.delete("/documents/{doc_id}", status_code=204)
async def delete_document(doc_id: UUID, p: Principal = Depends(current_principal), pool=Depends(get_pool)):
    async with tenant_tx(pool, p) as c:
        deleted = await c.fetchval("DELETE FROM documents WHERE id = $1 RETURNING id", doc_id)
    if deleted is None:
        raise HTTPException(404, "document not found")
