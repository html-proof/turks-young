import asyncpg


async def create_pool(dsn: str) -> asyncpg.Pool:
    pool = await asyncpg.create_pool(
        dsn,
        min_size=2,
        max_size=10,
        command_timeout=10,
        ssl="require",
    )
    return pool
