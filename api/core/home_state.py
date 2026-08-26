"""Small process-local guards for home-feed resilience.

Redis remains the shared cache; this layer prevents duplicate work inside one
API worker and retains the last successful response when Redis or the upstream
catalog is temporarily unavailable.
"""
from __future__ import annotations

import asyncio
from typing import Any

HOME_INFLIGHT: dict[str, asyncio.Task[Any]] = {}
HOME_STALE: dict[str, dict[str, Any]] = {}


def invalidate_home(uid: str) -> None:
    """Drop local feed snapshots for a user after a preference event."""
    prefix = f"home:{uid}:"
    for key in tuple(HOME_STALE):
        if key.startswith(prefix):
            HOME_STALE.pop(key, None)


async def coalesce(key: str, factory):
    task = HOME_INFLIGHT.get(key)
    if task is None or task.done():
        task = asyncio.create_task(factory())
        HOME_INFLIGHT[key] = task
    try:
        return await task
    finally:
        if task.done() and HOME_INFLIGHT.get(key) is task:
            HOME_INFLIGHT.pop(key, None)
