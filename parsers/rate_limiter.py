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
        async with self._lock:
            now = time.monotonic()
            elapsed = now - self.last_refill
            self.tokens = min(self.max_tokens, self.tokens + elapsed * self.rate / 60)
            self.last_refill = now
            if self.tokens >= 1:
                self.tokens -= 1
            else:
                # Wait for 1 token to refill, then consume it
                wait = (1 - self.tokens) / (self.rate / 60)
                await asyncio.sleep(wait)
                # After sleep, we now have ~1 token. Consume it.
                self.tokens = 0  # consumed the token we waited for


_buckets: dict[str, TokenBucket] = {d: TokenBucket(r) for d, r in RATE_LIMITS.items()}


async def acquire(domain: str):
    bucket = _buckets.get(domain)
    if bucket:
        await bucket.acquire()
