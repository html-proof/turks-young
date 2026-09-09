from pathlib import Path
import logging
import asyncpg

logger = logging.getLogger(__name__)


async def create_pool(dsn: str) -> asyncpg.Pool:
    pool = await asyncpg.create_pool(
        dsn,
        min_size=2,
        max_size=10,
        command_timeout=20,
        ssl="require",
        statement_cache_size=0,
    )
    try:
        schema = Path(__file__).with_name("schema.sql").read_text(encoding="utf-8")
        async with pool.acquire() as connection:
            await connection.execute(schema)
    except Exception as exc:
        logger.warning("Database schema auto-apply notice: %s", exc)
    return pool
