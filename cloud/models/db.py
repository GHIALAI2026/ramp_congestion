"""Database and Redis connection management."""

from __future__ import annotations

import asyncpg
import redis.asyncio as aioredis

from cloud.config import settings

_db_pool: asyncpg.Pool | None = None
_redis: aioredis.Redis | None = None


async def init_db() -> asyncpg.Pool:
    global _db_pool
    _db_pool = await asyncpg.create_pool(
        settings.database_url_raw,
        min_size=2,
        max_size=10,
    )
    return _db_pool


async def get_db() -> asyncpg.Pool:
    assert _db_pool is not None, "Database not initialized"
    return _db_pool


async def close_db() -> None:
    global _db_pool
    if _db_pool:
        await _db_pool.close()
        _db_pool = None


async def init_redis() -> aioredis.Redis:
    global _redis
    _redis = aioredis.from_url(settings.redis_url, decode_responses=True)
    return _redis


async def get_redis() -> aioredis.Redis:
    assert _redis is not None, "Redis not initialized"
    return _redis


async def close_redis() -> None:
    global _redis
    if _redis:
        await _redis.close()
        _redis = None
