from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from app.auth import get_pool, hash_password, require_admin
from app.db import Principal, tenant_tx

router = APIRouter(tags=["admin"], dependencies=[Depends(require_admin)])


class UserIn(BaseModel):
    email: str
    password: str = Field(min_length=8)
    role: Literal["admin", "member", "viewer"] = "member"


class GroupIn(BaseModel):
    name: str = Field(min_length=1)


class AccessIn(BaseModel):
    visibility: Literal["tenant", "restricted"]
    group_ids: list[UUID] = []


@router.post("/users", status_code=201)
async def create_user(body: UserIn, p: Principal = Depends(require_admin), pool=Depends(get_pool)):
    async with tenant_tx(pool, p) as c:
        uid = await c.fetchval(
            "INSERT INTO users (tenant_id, email, password_hash, role) VALUES (app_tenant(), $1, $2, $3) RETURNING id",
            body.email, hash_password(body.password), body.role)
    return {"id": uid}


@router.get("/users")
async def list_users(p: Principal = Depends(require_admin), pool=Depends(get_pool)):
    async with tenant_tx(pool, p) as c:
        return [dict(r) for r in await c.fetch("SELECT id, email, role FROM users ORDER BY email")]


@router.post("/groups", status_code=201)
async def create_group(body: GroupIn, p: Principal = Depends(require_admin), pool=Depends(get_pool)):
    async with tenant_tx(pool, p) as c:
        gid = await c.fetchval("INSERT INTO groups (tenant_id, name) VALUES (app_tenant(), $1) RETURNING id", body.name)
    return {"id": gid}


@router.get("/groups")
async def list_groups(p: Principal = Depends(require_admin), pool=Depends(get_pool)):
    async with tenant_tx(pool, p) as c:
        return [dict(r) for r in await c.fetch("SELECT id, name FROM groups ORDER BY name")]


@router.put("/groups/{group_id}/members/{user_id}", status_code=204)
async def add_member(group_id: UUID, user_id: UUID, p: Principal = Depends(require_admin), pool=Depends(get_pool)):
    async with tenant_tx(pool, p) as c:
        await c.execute(
            "INSERT INTO user_groups (tenant_id, user_id, group_id) VALUES (app_tenant(), $1, $2) ON CONFLICT DO NOTHING",
            user_id, group_id)


@router.delete("/groups/{group_id}/members/{user_id}", status_code=204)
async def remove_member(group_id: UUID, user_id: UUID, p: Principal = Depends(require_admin), pool=Depends(get_pool)):
    """Revocation is immediate: the next query recomputes the caller's groups."""
    async with tenant_tx(pool, p) as c:
        await c.execute("DELETE FROM user_groups WHERE user_id = $1 AND group_id = $2", user_id, group_id)


@router.put("/documents/{doc_id}/access", status_code=204)
async def set_access(doc_id: UUID, body: AccessIn, p: Principal = Depends(require_admin), pool=Depends(get_pool)):
    """Replace a document's visibility and ACL. Triggers re-sync every chunk of the document."""
    if body.visibility == "restricted" and not body.group_ids:
        raise HTTPException(422, "restricted documents need at least one group (or only admins see them)")
    async with tenant_tx(pool, p) as c:
        updated = await c.fetchval("UPDATE documents SET visibility = $2 WHERE id = $1 RETURNING id", doc_id, body.visibility)
        if updated is None:
            raise HTTPException(404, "document not found")
        await c.execute("DELETE FROM document_acl WHERE document_id = $1", doc_id)
        for gid in body.group_ids:
            await c.execute(
                "INSERT INTO document_acl (tenant_id, document_id, group_id) VALUES (app_tenant(), $1, $2)", doc_id, gid)


@router.get("/audit")
async def audit(limit: int = 100, p: Principal = Depends(require_admin), pool=Depends(get_pool)):
    async with tenant_tx(pool, p) as c:
        rows = await c.fetch(
            "SELECT id, user_id, action, query, chunk_ids, created_at FROM audit_log ORDER BY id DESC LIMIT $1",
            min(limit, 500))
    return [dict(r) for r in rows]
