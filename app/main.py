from contextlib import asynccontextmanager

import asyncpg
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app import observability
from app.api import admin, auth, documents, query
from app.config import Settings, get_settings
from app.db import create_pool
from app.rag.embedder import Embedder, build_embedder
from app.rag.generator import Generator, build_generator
from app.ratelimit import TenantRateLimiter


def create_app(settings: Settings | None = None, embedder: Embedder | None = None,
               generator: Generator | None = None) -> FastAPI:
    settings = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.pool = await create_pool(settings.database_url)
        yield
        await app.state.pool.close()

    app = FastAPI(title="Permission-Aware Multi-Tenant RAG", lifespan=lifespan)
    app.state.settings = settings
    app.state.embedder = embedder or build_embedder(settings)
    app.state.generator = generator or build_generator(settings)
    app.state.limiter = TenantRateLimiter()
    app.dependency_overrides[get_settings] = lambda: settings
    observability.install(app, settings)

    for r in (auth.router, admin.router, documents.router, query.router):
        app.include_router(r)

    @app.get("/health")
    def health():
        return {"status": "ok"}

    # Translate database-level rejections into proper HTTP errors.
    @app.exception_handler(asyncpg.InsufficientPrivilegeError)
    async def _rls(_: Request, __: Exception):
        return JSONResponse({"detail": "forbidden by row-level security"}, status_code=403)

    @app.exception_handler(asyncpg.UniqueViolationError)
    async def _dup(_: Request, __: Exception):
        return JSONResponse({"detail": "already exists"}, status_code=409)

    @app.exception_handler(asyncpg.ForeignKeyViolationError)
    async def _fk(_: Request, __: Exception):
        return JSONResponse({"detail": "referenced record not found in your tenant"}, status_code=422)

    return app


app = create_app()
