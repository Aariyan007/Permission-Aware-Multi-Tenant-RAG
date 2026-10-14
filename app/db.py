"""Database access. Every request-scoped query goes through `tenant_tx`, which pins the
caller's identity into the transaction so Postgres RLS can filter rows."""
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from uuid import UUID

import asyncpg


@dataclass(frozen=True)
class Principal:
    user_id: UUID
    tenant_id: UUID
    role: str  # advisory only: the database re-checks the real role on every statement


async def create_pool(dsn: str) -> asyncpg.Pool:
    return await asyncpg.create_pool(dsn, min_size=1, max_size=10)


@asynccontextmanager
async def tenant_tx(pool: asyncpg.Pool, p: Principal) -> AsyncIterator[asyncpg.Connection]:
    """One transaction with app.tenant_id / app.user_id set. `true` = transaction-local
    (like SET LOCAL), so values vanish at COMMIT/ROLLBACK and can't leak to the next
    request that reuses this pooled connection."""
    async with pool.acquire() as conn, conn.transaction():
        await conn.fetchval(
            "SELECT set_config('app.tenant_id', $1, true), set_config('app.user_id', $2, true)",
            str(p.tenant_id),
            str(p.user_id),
        )
        # pgvector: HNSW + RLS can under-return; iterative scan keeps scanning until k visible rows.
        await conn.execute("SET LOCAL hnsw.iterative_scan = 'strict_order'")
        yield conn


def vec_literal(v: list[float]) -> str:
    return "[" + ",".join(f"{x:.6f}" for x in v) + "]"
