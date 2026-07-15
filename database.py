import logging
from pathlib import Path

import asyncpg

logger = logging.getLogger(__name__)

_pool: asyncpg.Pool | None = None


async def create_pool(dsn: str) -> asyncpg.Pool:
    global _pool
    _pool = await asyncpg.create_pool(dsn, min_size=2, max_size=10)
    await _apply_schema(_pool)
    return _pool


async def get_pool() -> asyncpg.Pool:
    if _pool is None:
        raise RuntimeError("DB pool not initialised")
    return _pool


async def _apply_schema(pool: asyncpg.Pool) -> None:
    """Apply schema.sql first, then every other migrations/*.sql in filename
    order. All migration files are idempotent, so this is safe on every boot
    and brings a fresh database fully up to date."""
    migrations_dir = Path(__file__).parent / "migrations"
    files = [migrations_dir / "schema.sql"] + sorted(
        p for p in migrations_dir.glob("*.sql") if p.name != "schema.sql"
    )
    async with pool.acquire() as conn:
        for path in files:
            try:
                await conn.execute(path.read_text())
                logger.info("Applied migration: %s", path.name)
            except Exception as exc:
                logger.error("Migration %s failed (startup continues): %s", path.name, exc)
