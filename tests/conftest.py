"""Test world: a real Postgres (pgvector), fresh DB per session, three tenants with planted canaries."""
import os
from dataclasses import dataclass, field

import asyncpg
import httpx
import pytest_asyncio
from asgi_lifespan import LifespanManager

from app.config import Settings
from app.main import create_app
from app.migrate import run as run_migrations

PG = os.environ.get("TEST_PG", "postgresql://postgres:postgres@localhost:5432")
DB = "rag_test"
APP_PW = "rag_app_pw"
BOOT = "test-bootstrap"


@dataclass
class UserCtx:
    email: str
    password: str
    tenant: str  # "a" | "b" | "c"
    role: str
    groups: set[str]
    id: str = ""
    token: str = ""

    @property
    def headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.token}"}


@dataclass
class World:
    client: httpx.AsyncClient
    settings: Settings
    users: dict[str, UserCtx] = field(default_factory=dict)  # key "a:admin"
    group_ids: dict[str, str] = field(default_factory=dict)  # key "a:eng"
    tenant_ids: dict[str, str] = field(default_factory=dict)
    docs: dict[str, str] = field(default_factory=dict)  # key "a:eng" -> document id
    chunk_owner: dict[str, str] = field(default_factory=dict)  # chunk id -> "a:eng"

    def user(self, tenant: str, name: str) -> UserCtx:
        return self.users[f"{tenant}:{name}"]


def canary(tenant: str, kind: str) -> str:
    return f"canary{kind}{tenant}"  # e.g. canaryenga


def can_see(u: UserCtx, tenant: str, kind: str) -> bool:
    if u.tenant != tenant:
        return False
    return kind == "pub" or u.role == "admin" or kind in u.groups


@pytest_asyncio.fixture(scope="session")
async def world():
    admin = await asyncpg.connect(f"{PG}/postgres")
    await admin.execute(f"DROP DATABASE IF EXISTS {DB} WITH (FORCE)")
    await admin.execute(f"CREATE DATABASE {DB}")
    await admin.close()
    await run_migrations(f"{PG}/{DB}", APP_PW)

    settings = Settings(
        database_url=f"postgresql://rag_app:{APP_PW}@{PG.split('@')[1]}/{DB}",
        admin_database_url=f"{PG}/{DB}", bootstrap_key=BOOT, jwt_secret="test-secret-that-is-at-least-32-bytes-long",
        rate_limit_per_minute=100_000, anthropic_api_key=None, voyage_api_key=None)
    app = create_app(settings)
    async with LifespanManager(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
            w = World(client, settings)
            await _seed(w)
            yield w


async def login(w: World, u: UserCtx) -> None:
    r = await w.client.post("/auth/login", json={"email": u.email, "password": u.password})
    assert r.status_code == 200, r.text
    u.token = r.json()["access_token"]


async def _seed(w: World) -> None:
    c = w.client
    for t in "abc":
        r = await c.post("/tenants", headers={"X-Bootstrap-Key": BOOT},
                         json={"name": f"Tenant {t}", "admin_email": f"admin@{t}.test", "admin_password": "password-123"})
        assert r.status_code == 201, r.text
        w.tenant_ids[t] = r.json()["tenant_id"]
        admin = UserCtx(f"admin@{t}.test", "password-123", t, "admin", set(), id=r.json()["admin_user_id"])
        w.users[f"{t}:admin"] = admin
        await login(w, admin)

        for g in ("eng", "hr"):
            r = await c.post("/groups", headers=admin.headers, json={"name": g})
            assert r.status_code == 201, r.text
            w.group_ids[f"{t}:{g}"] = r.json()["id"]

        for name, role, groups in (("alice", "member", {"eng"}), ("bob", "member", {"hr"}),
                                   ("carol", "viewer", set()), ("dave", "member", set())):
            u = UserCtx(f"{name}@{t}.test", "password-123", t, role, groups)
            r = await c.post("/users", headers=admin.headers, json={"email": u.email, "password": u.password, "role": role})
            assert r.status_code == 201, r.text
            u.id = r.json()["id"]
            for g in groups:
                rr = await c.put(f"/groups/{w.group_ids[f'{t}:{g}']}/members/{u.id}", headers=admin.headers)
                assert rr.status_code == 204, rr.text
            w.users[f"{t}:{name}"] = u
            await login(w, u)

        for kind, vis, gids in (("pub", "tenant", ""), ("eng", "restricted", w.group_ids[f"{t}:eng"]),
                                ("hr", "restricted", w.group_ids[f"{t}:hr"])):
            text = f"The {kind} policy of tenant {t}. Secret marker {canary(t, kind)} is written in this paragraph."
            r = await c.post("/documents", headers=admin.headers,
                             data={"title": f"{kind}-{t}", "text": text, "visibility": vis, "group_ids": gids})
            assert r.status_code == 201, r.text
            w.docs[f"{t}:{kind}"] = r.json()["id"]

    # Map every chunk id back to which "tenant:kind" doc it belongs to (for the oracle).
    conn = await asyncpg.connect(w.settings.admin_database_url)
    for row in await conn.fetch("SELECT id, document_id FROM chunks"):
        for key, doc_id in w.docs.items():
            if str(row["document_id"]) == doc_id:
                w.chunk_owner[str(row["id"])] = key
    await conn.close()


async def ask(w: World, u: UserCtx, question: str, top_k: int = 20) -> dict:
    r = await w.client.post("/query", headers=u.headers, json={"question": question, "top_k": top_k})
    assert r.status_code == 200, r.text
    return r.json()
