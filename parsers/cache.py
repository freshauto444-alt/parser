# parsers/cache.py
# Cache-on-Read + Singleflight + Stale-While-Revalidate.
#
# Only scrapes what users actually search for.
# 10 identical concurrent searches → 1 scrape (singleflight).
# Stale data served instantly while refreshing in background.

import asyncio
import time
from typing import Callable, Awaitable
from cachetools import TTLCache
from loguru import logger
from .config import CACHE_TTL_SECONDS, CACHE_STALE_SECONDS, CACHE_MAX_ENTRIES

# L1 in-memory cache: (data, timestamp) tuples
_store = TTLCache(maxsize=CACHE_MAX_ENTRIES, ttl=CACHE_TTL_SECONDS)

# Singleflight: one scrape per cache key, concurrent callers share result
_flights: dict[str, asyncio.Future] = {}
_lock = asyncio.Lock()


def cache_key(make: str, model: str, **filters) -> str:
    """Fuzzy cache key: rounds price to 2K (ceil), groups similar searches."""
    from math import ceil
    # vehicle_type is first part of the key so Car vs Motorcycle searches
    # don't collide even for same brand+model ("BMW" + empty model).
    vtype = (filters.get("vehicle_type") or "Car").lower()
    parts = [vtype, (make or "").lower().strip(), (model or "").lower().strip()]
    if filters.get("fuel"):
        parts.append(filters["fuel"].lower())
    if filters.get("year_from"):
        parts.append(str(filters["year_from"]))
    if filters.get("price_to"):
        parts.append("pt" + str(ceil(int(filters["price_to"]) / 2000) * 2000))
    if filters.get("price_from"):
        # Floor to 2K bucket — groups 25000 and 25999 into same cache
        parts.append("pf" + str((int(filters["price_from"]) // 2000) * 2000))
    if filters.get("year_to"):
        parts.append("yt" + str(filters["year_to"]))
    if filters.get("body_type"):
        parts.append("b" + filters["body_type"].lower())
    if filters.get("transmission"):
        parts.append(filters["transmission"].lower())
    return "|".join(parts)


async def get_or_scrape(key: str, scrape: Callable[[], Awaitable[list]]) -> list:
    """
    Smart cache access:
    - HIT fresh → return <1ms
    - HIT stale → return instantly + background refresh
    - MISS → scrape (singleflight: only 1 concurrent scrape per key)
    """
    # Check cache
    entry = _store.get(key)
    if entry is not None:
        data, ts = entry
        age = time.monotonic() - ts
        if age < CACHE_STALE_SECONDS:
            return data  # fresh
        # stale — return + async refresh
        asyncio.create_task(_refresh(key, scrape))
        return data

    # Singleflight
    async with _lock:
        # Double-check after lock
        entry = _store.get(key)
        if entry is not None:
            return entry[0]
        if key in _flights:
            fut = _flights[key]
        else:
            fut = asyncio.Future()
            _flights[key] = fut
            asyncio.create_task(_run(key, scrape, fut))

    try:
        # 35s: AS24 HTTP ~3-5s, Blocket Playwright ~8-15s, parallel total ~15-20s.
        # Buffer of ~2x typical. 120s was too long — user would give up long before.
        return await asyncio.wait_for(fut, timeout=35)
    except asyncio.TimeoutError:
        logger.warning(f"[cache] scrape timeout (35s) for {key}, returning stale-or-empty")
        async with _lock:
            _flights.pop(key, None)  # evict stuck flight
        stale = _store.get(key)
        return stale[0] if stale is not None else []


async def _run(key: str, scrape: Callable, fut: asyncio.Future):
    try:
        data = await scrape()
        _store[key] = (data, time.monotonic())
        if not fut.done():
            fut.set_result(data)
    except Exception as e:
        logger.error(f"[cache] scrape failed: {key}: {e}")
        if not fut.done():
            # Try to return stale cached data instead of empty
            stale = _store.get(key)
            if stale is not None:
                logger.info(f"[cache] returning stale data for {key}")
                fut.set_result(stale[0])
            else:
                fut.set_result([])
    finally:
        async with _lock:
            _flights.pop(key, None)


async def _refresh(key: str, scrape: Callable):
    async with _lock:
        if key in _flights:
            return  # someone else refreshing
    try:
        data = await scrape()
        if data:
            _store[key] = (data, time.monotonic())
            logger.debug(f"[cache] refreshed: {key} ({len(data)} items)")
    except Exception as e:
        logger.warning(f"[cache] refresh failed: {key}: {e}")


async def cleanup_stale_flights():
    """Remove flights whose futures are already done but weren't popped (crash recovery)."""
    async with _lock:
        done = [k for k, fut in _flights.items() if fut.done()]
        for k in done:
            _flights.pop(k, None)
        if done:
            logger.debug(f"[cache] Cleaned {len(done)} completed flights")


def stats() -> dict:
    return {"entries": len(_store), "max": _store.maxsize, "inflight": len(_flights)}
