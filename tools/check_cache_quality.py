#!/usr/bin/env python3
"""Unit test for quality-aware caching (cache.get_or_scrape).

A DEGRADED scrape (a source errored / union empty → complete=False) must get a
short fresh window and be re-scraped after it; a COMPLETE scrape is served fresh
for the full hour. This is the fix for "C63/GTI stuck at 0 for an hour after a
transient AS24 404 / Bytbil empty". Run: python tools/check_cache_quality.py
"""
from __future__ import annotations
import asyncio, time, sys
import parsers.cache as cache
from parsers.config import CACHE_PARTIAL_STALE_SECONDS, CACHE_STALE_SECONDS


async def main() -> int:
    fails = []
    calls = {"n": 0}

    # Scrape returns degraded first (0 cars, complete=False), then healthy.
    async def scrape():
        calls["n"] += 1
        if calls["n"] == 1:
            return ([], False)               # degraded: source down
        return (["car1", "car2"], True)      # recovered: full result

    cache._store.clear()
    key = "k1"

    # 1) First call → scrapes, gets degraded [], cached with complete=False
    r1 = await cache.get_or_scrape(key, scrape)
    if r1 != [] or calls["n"] != 1:
        fails.append(f"first call: got {r1}, calls={calls['n']}")

    # 2) Immediate re-request within the 90s partial window → served from cache,
    #    NO re-scrape (avoid hammering), still degraded.
    r2 = await cache.get_or_scrape(key, scrape)
    if calls["n"] != 1:
        fails.append(f"within-window re-request should NOT re-scrape, calls={calls['n']}")

    # 3) Age the degraded entry past the partial window → must re-scrape and now
    #    return the recovered healthy result (this is the core fix).
    data, ts, complete = cache._read_entry(cache._store.get(key))
    cache._store[key] = (data, ts - (CACHE_PARTIAL_STALE_SECONDS + 5), complete)
    r3 = await cache.get_or_scrape(key, scrape)
    if r3 != ["car1", "car2"] or calls["n"] != 2:
        fails.append(f"expired degraded should re-scrape to healthy: got {r3}, calls={calls['n']}")

    # 4) The healthy entry is complete → served fresh even when aged within the hour.
    data, ts, complete = cache._read_entry(cache._store.get(key))
    if not complete:
        fails.append("recovered entry should be complete=True")
    cache._store[key] = (data, ts - (CACHE_PARTIAL_STALE_SECONDS + 5), complete)
    r4 = await cache.get_or_scrape(key, scrape)
    if r4 != ["car1", "car2"] or calls["n"] != 2:
        fails.append(f"complete entry within hour must NOT re-scrape: got {r4}, calls={calls['n']}")

    # 5) Legacy 2-tuple entries read as complete (back-compat).
    cache._store["legacy"] = (["x"], time.monotonic())
    if cache._read_entry(cache._store.get("legacy"))[2] is not True:
        fails.append("legacy 2-tuple should read complete=True")

    if fails:
        print("FAILED:\n  " + "\n  ".join(fails)); return 1
    print("cache-quality: all 5 checks passed.")
    return 0

sys.exit(asyncio.run(main()))
