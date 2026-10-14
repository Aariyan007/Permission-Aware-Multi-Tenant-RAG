
from fastapi import APIRouter, Depends, Header, HTTPException, Request
from pydantic import BaseModel, Field

from app.auth import hash_password, issue_token, verify_password
from app.config import Settings, get_settings

router = APIRouter(tags=["auth"])


class TenantIn(BaseModel):
    name: str = Field(min_length=1)
    admin_email: str
    admin_password: str = Field(min_length=8)


class LoginIn(BaseModel):
    email: str
    password: str


@router.post("/tenants", status_code=201)
async def create_tenant(body: TenantIn, request: Request, x_bootstrap_key: str = Header(""),
                        settings: Settings = Depends(get_settings)):
    """Sign-up for a new organization. Guarded by a bootstrap key; runs through a
    SECURITY DEFINER function because no tenant context exists yet."""
    if x_bootstrap_key != settings.bootstrap_key:
        raise HTTPException(403, "bad bootstrap key")
    row = await request.app.state.pool.fetchrow(
        "SELECT * FROM create_tenant($1, $2, $3)", body.name, body.admin_email, hash_password(body.admin_password))
    return {"tenant_id": row["tenant_id"], "admin_user_id": row["user_id"]}


@router.post("/auth/login")
async def login(body: LoginIn, request: Request, settings: Settings = Depends(get_settings)):
    row = await request.app.state.pool.fetchrow("SELECT * FROM auth_lookup($1)", body.email)
    ok = verify_password(body.password, row["password_hash"]) if row else False
    if not ok:  # same error for unknown email and wrong password
        raise HTTPException(401, "invalid credentials")
    token = issue_token(settings, row["user_id"], row["tenant_id"], row["role"])
    return {"access_token": token, "token_type": "bearer"}
