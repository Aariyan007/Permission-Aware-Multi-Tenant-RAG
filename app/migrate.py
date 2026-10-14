"""Apply migrations/*.sql in order as the DB owner. Usage: python -m app.migrate"""
import asyncio
from pathlib import Path

import asyncpg

from app.config import get_settings

MIGRATIONS = Path(__file__).resolve().parent.parent / "migrations"


async def run(admin_dsn: str, app_password: str) -> None:
    conn = await asyncpg.connect(admin_dsn)
    try:
        # The restricted role the API uses. NOBYPASSRLS is what makes RLS binding.
        exists = await conn.fetchval("SELECT 1 FROM pg_roles WHERE rolname = 'rag_app'")
        pw = await conn.fetchval("SELECT quote_literal($1::text)", app_password)
        verb = "ALTER" if exists else "CREATE"
        await conn.execute(f"{verb} ROLE rag_app LOGIN NOSUPERUSER NOBYPASSRLS PASSWORD {pw}")
        await conn.execute(
            "CREATE TABLE IF NOT EXISTS schema_migrations (name text PRIMARY KEY, applied_at timestamptz DEFAULT now())")
        done = {r["name"] for r in await conn.fetch("SELECT name FROM schema_migrations")}
        for f in sorted(MIGRATIONS.glob("*.sql")):
            if f.name in done:
                continue
            async with conn.transaction():
                await conn.execute(f.read_text())
                await conn.execute("INSERT INTO schema_migrations (name) VALUES ($1)", f.name)
            print(f"applied {f.name}")
    finally:
        await conn.close()


if __name__ == "__main__":
    s = get_settings()
    asyncio.run(run(s.admin_database_url, s.app_db_password))
