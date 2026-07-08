import asyncpg
from pathlib import Path

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
    schema = (Path(__file__).parent / "migrations" / "schema.sql").read_text()
    async with pool.acquire() as conn:
        await conn.execute(schema)
