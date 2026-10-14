import hashlib
import hmac
import os
import time
from uuid import UUID

import jwt
from fastapi import Depends, HTTPException, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.config import Settings, get_settings
from app.db import Principal

_bearer = HTTPBearer(auto_error=False)
_ITERATIONS = 200_000


def hash_password(password: str) -> str:
    salt = os.urandom(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, _ITERATIONS)
    return f"{salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    salt_hex, digest_hex = stored.split("$")
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt_hex), _ITERATIONS)
    return hmac.compare_digest(digest.hex(), digest_hex)


def issue_token(settings: Settings, user_id: UUID, tenant_id: UUID, role: str) -> str:
    now = int(time.time())
    claims = {"sub": str(user_id), "tid": str(tenant_id), "role": role, "iat": now,
              "exp": now + settings.jwt_ttl_seconds}
    return jwt.encode(claims, settings.jwt_secret, algorithm="HS256")


def current_principal(
    creds: HTTPAuthorizationCredentials | None = Depends(_bearer),
    settings: Settings = Depends(get_settings),
) -> Principal:
    if creds is None:
        raise HTTPException(401, "missing bearer token")
    try:
        c = jwt.decode(creds.credentials, settings.jwt_secret, algorithms=["HS256"])
        return Principal(UUID(c["sub"]), UUID(c["tid"]), c["role"])
    except (jwt.PyJWTError, KeyError, ValueError):
        raise HTTPException(401, "invalid token") from None


def require_admin(p: Principal = Depends(current_principal)) -> Principal:
    if p.role != "admin":
        raise HTTPException(403, "admin only")
    return p


def require_writer(p: Principal = Depends(current_principal)) -> Principal:
    if p.role not in ("admin", "member"):
        raise HTTPException(403, "viewers cannot write")
    return p


def get_pool(request: Request):
    return request.app.state.pool
