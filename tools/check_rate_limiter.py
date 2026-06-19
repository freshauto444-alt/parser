#!/usr/bin/env python3
"""Unit test for the TokenBucket lock-during-sleep fix (rate_limiter.py).

The bug: acquire() slept INSIDE `async with self._lock`, so a token-starved
acquire (AS24 mid deep-pagination) held the per-domain lock across the whole
sleep and blocked EVERY other acquire for that domain — the next user's search
starved on the lock and hit its deadline with 0 cars. The fix releases the lock
before sleeping. Run: python tools/check_rate_limiter.py
"""
from __future__ import annotations
import asyncio, time, sys
from parsers.rate_limiter import TokenBucket


async def main() -> int:
    fails = []

    # 1) The lock must NOT be held while an acquire waits for a refill.
    b = TokenBucket(rate_per_minute=6)   # 0.1 tok/sec — a wait is forced
    b.tokens = 0.0
    b.last_refill = time.monotonic()
    task = asyncio.create_task(b.acquire())
    await asyncio.sleep(0.25)            # let it enter the refill wait
    if b._lock.locked():
        fails.append("lock is HELD during the refill sleep (the bug) — concurrent "
                     "acquires on this domain would block")
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass

    # 2) A second acquire that CAN be served proceeds while another is waiting —
    #    i.e. one waiter does not freeze the whole bucket.
    b2 = TokenBucket(rate_per_minute=600)  # 10 tok/sec, max 600
    b2.tokens = 1.0                          # exactly one token available
    b2.last_refill = time.monotonic()
    waiter = asyncio.create_task(b2.acquire())   # consumes... or waits
    await asyncio.sleep(0.05)
    t0 = time.monotonic()
    await asyncio.wait_for(b2.acquire(), timeout=2.0)  # must not hang on a held lock
    if time.monotonic() - t0 > 1.5:
        fails.append("second acquire took too long — lock/serialization regression")
    waiter.cancel()
    try:
        await waiter
    except asyncio.CancelledError:
        pass

    # 3) Still rate-bounded: with a full bucket, N acquires are instant.
    b3 = TokenBucket(rate_per_minute=60)
    t0 = time.monotonic()
    await asyncio.gather(*[b3.acquire() for _ in range(10)])
    if time.monotonic() - t0 > 0.5:
        fails.append("10 acquires from a full bucket should be ~instant")

    if fails:
        print("FAILED:\n  " + "\n  ".join(fails)); return 1
    print("rate-limiter: all 3 checks passed.")
    return 0

sys.exit(asyncio.run(main()))
