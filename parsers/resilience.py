# parsers/resilience.py
# Circuit breaker per source: CLOSED → OPEN (skip 5 min) → HALF_OPEN → CLOSED.

import time
from typing import Optional
from loguru import logger
from .config import CIRCUIT_FAILURE_THRESHOLD, CIRCUIT_RECOVERY_SECONDS


class CircuitBreaker:
    __slots__ = ("name", "threshold", "timeout", "failures", "state", "last_fail", "total_ok", "total_err")

    def __init__(self, name: str, threshold: int = CIRCUIT_FAILURE_THRESHOLD, timeout: int = CIRCUIT_RECOVERY_SECONDS):
        self.name = name
        self.threshold = threshold
        self.timeout = timeout
        self.failures = 0
        self.state = "CLOSED"
        self.last_fail: float = 0
        self.total_ok = 0
        self.total_err = 0

    @property
    def available(self) -> bool:
        if self.state == "CLOSED":
            return True
        if self.state == "OPEN" and (time.monotonic() - self.last_fail) >= self.timeout:
            self.state = "HALF_OPEN"
            logger.info(f"[circuit:{self.name}] OPEN → HALF_OPEN")
            return True
        return self.state == "HALF_OPEN"

    async def call(self, coro):
        if not self.available:
            logger.debug(f"[circuit:{self.name}] OPEN — skip")
            return []
        try:
            result = await coro
            self.failures = 0
            if self.state != "CLOSED":
                logger.info(f"[circuit:{self.name}] → CLOSED")
            self.state = "CLOSED"
            self.total_ok += 1
            return result
        except Exception as e:
            self.failures += 1
            self.total_err += 1
            self.last_fail = time.monotonic()
            if self.state == "HALF_OPEN" or self.failures >= self.threshold:
                self.state = "OPEN"
                logger.warning(f"[circuit:{self.name}] → OPEN ({e})")
            return []

    def status(self) -> dict:
        total = self.total_ok + self.total_err
        return {
            "name": self.name,
            "state": self.state,
            "failures": self.failures,
            "total": total,
            "error_rate": round(self.total_err / max(total, 1) * 100, 1),
        }


# Global instances
_breakers: dict[str, CircuitBreaker] = {}


def get_breaker(name: str) -> CircuitBreaker:
    if name not in _breakers:
        _breakers[name] = CircuitBreaker(name)
    return _breakers[name]


def all_statuses() -> list[dict]:
    return [b.status() for b in _breakers.values()]
