# parsers/rate_limiter.py
# Token bucket rate limiter per domain.

import asyncio
import time
from .config import RATE_LIMITS


class TokenBucket:
    __slots__ = ("rate", "tokens", "max_tokens", "last_refill", "_lock")

    def __init__(self, rate_per_minute: int):
        self.rate = max(rate_per_minute, 1)
        self.max_tokens = float(self.rate)
        self.tokens = self.max_tokens
        self.last_refill = time.monotonic()
        self._lock = asyncio.Lock()

    async def acquire(self):
        # Refill + consume under the lock, but NEVER hold the lock across the sleep.
        # The old code slept INSIDE `async with self._lock`, so a single token-starved
        # acquire (e.g. AS24 mid deep-pagination) blocked EVERY other acquire for the
        # same domain — the next user's search would starve waiting on the lock and
        # hit its deadline with 0 cars ("AS24 still busy with the previous query").
        # Loop: take what's available under the lock, otherwise compute the wait,
        # release the lock, sleep, and re-check. Other tasks refill/consume meanwhile.
        while True:
            async with self._lock:
                now = time.monotonic()
                elapsed = now - self.last_refill
                self.tokens = min(self.max_tokens, self.tokens + elapsed * self.rate / 60)
                self.last_refill = now
                if self.tokens >= 1:
                    self.tokens -= 1
                    return
                wait = (1 - self.tokens) / (self.rate / 60)
            # Cap a single sleep so we re-check periodically (the deficit may be
            # served by concurrent refill sooner than a stale estimate suggests).
            await asyncio.sleep(min(wait, 2.0))


_buckets: dict[str, TokenBucket] = {d: TokenBucket(r) for d, r in RATE_LIMITS.items()}


async def acquire(domain: str):
    bucket = _buckets.get(domain)
    if bucket:
        await bucket.acquire()
